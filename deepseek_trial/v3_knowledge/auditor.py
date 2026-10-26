"""DeepSeek TRADE/SKIP from strategy knowledge. Code still sizes and caps fills."""
from __future__ import annotations

import json

import requests

from deepseek_trial.config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_TIMEOUT_SEC,
    PACKAGE_DIR,
    V3_MODEL_NAME,
    deepseek_api_key,
    env_status,
)
from deepseek_trial.v3_knowledge.prompts import STRATEGY_A, STRATEGY_B, STRATEGY_C

TOKEN_LOG = PACKAGE_DIR / "v3_knowledge" / "books" / "token_usage.jsonl"
PROMPT_VERSION = "GOLD-OCT26-v3-knowledge-1"
CARDS = {"A": STRATEGY_A, "B": STRATEGY_B, "C": STRATEGY_C}

SYSTEM = (
    "You are a paper-trading assistant for one gold strategy. Use only your knowledge of "
    "THAT strategy plus the supplied bars. Do not invent news, institutions, or a win-rate. "
    "Software will size the trade, clamp the stop (0.80-2.50 ATR), and enforce a maximum of "
    "5 filled trades per day. You cannot change lot size or risk dollars.\n"
    "If the bars show a valid setup for this strategy right now, return TRADE. "
    "Otherwise SKIP. Prefer SKIP when mixed or unclear.\n"
    "Return one JSON object only:\n"
    '{"decision":"TRADE or SKIP","side":"BUY or SELL or NONE",'
    '"stop_atr":1.2,"target_r":2.5,"reason":"maximum 45 words"}\n'
    "stop_atr is a positive number (software clamps 0.80-2.50). "
    "target_r is reward-to-risk (software clamps 1.50-2.50). "
    "side must be BUY or SELL when decision is TRADE."
)


def _extract_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def propose(strategy_id: str, snapshot: dict) -> tuple[dict | None, dict]:
    """Returns (proposal or None, meta). Late/bad JSON is SKIP."""
    meta = {
        "model": V3_MODEL_NAME,
        "prompt": PROMPT_VERSION,
        "strategy": strategy_id,
        "thinking": True,
        "env": env_status(),
    }
    if not deepseek_api_key():
        return None, {**meta, "error": "NO_API_KEY"}
    body = {
        "model": V3_MODEL_NAME,
        "temperature": 0,
        "thinking": {"type": "enabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM + "\n\nSTRATEGY:\n" + CARDS[strategy_id]},
            {"role": "user", "content": json.dumps(snapshot, default=str)[:10000]},
        ],
    }
    try:
        resp = requests.post(
            DEEPSEEK_BASE_URL,
            headers={"Authorization": f"Bearer {deepseek_api_key()}", "Content-Type": "application/json"},
            json=body,
            timeout=DEEPSEEK_TIMEOUT_SEC,
        )
        if resp.status_code == 400:
            return _propose_without_thinking(strategy_id, snapshot, meta)
        resp.raise_for_status()
        payload = resp.json()
        msg = payload["choices"][0]["message"]
        content = msg.get("content") or ""
        raw = _extract_json(content)
        usage = payload.get("usage") or {}
        TOKEN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TOKEN_LOG.open("a") as handle:
            handle.write(json.dumps({
                "model": V3_MODEL_NAME, "strategy": strategy_id,
                "thinking": True, **usage,
            }) + "\n")
        meta["usage"] = usage
        meta["model"] = V3_MODEL_NAME
    except (requests.RequestException, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return None, {**meta, "error": str(exc)}

    return _parse(raw, meta)


def _propose_without_thinking(strategy_id: str, snapshot: dict, meta: dict) -> tuple[dict | None, dict]:
    """Retry flash if `thinking.enabled` is rejected; still the flash model."""
    body = {
        "model": V3_MODEL_NAME,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM + "\n\nSTRATEGY:\n" + CARDS[strategy_id]},
            {"role": "user", "content": json.dumps(snapshot, default=str)[:10000]},
        ],
    }
    try:
        resp = requests.post(
            DEEPSEEK_BASE_URL,
            headers={"Authorization": f"Bearer {deepseek_api_key()}", "Content-Type": "application/json"},
            json=body,
            timeout=DEEPSEEK_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        payload = resp.json()
        content = (payload["choices"][0]["message"].get("content") or "")
        raw = _extract_json(content)
        usage = payload.get("usage") or {}
        TOKEN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TOKEN_LOG.open("a") as handle:
            handle.write(json.dumps({
                "model": V3_MODEL_NAME, "strategy": strategy_id,
                "thinking": "unspecified", **usage,
            }) + "\n")
        meta["usage"] = usage
        return _parse(raw, meta)
    except (requests.RequestException, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return None, {**meta, "error": str(exc)}


def _parse(raw: dict, meta: dict) -> tuple[dict | None, dict]:
    decision = str(raw.get("decision", "SKIP")).upper()
    if decision != "TRADE":
        return None, {**meta, "decision": "SKIP", "raw": raw}
    side = str(raw.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        return None, {**meta, "decision": "SKIP", "error": "SIDE", "raw": raw}
    try:
        stop_atr = float(raw.get("stop_atr", 1.2))
        target_r = float(raw.get("target_r", 2.5))
    except (TypeError, ValueError):
        return None, {**meta, "decision": "SKIP", "error": "NUMBERS", "raw": raw}
    stop_atr = min(2.50, max(0.80, stop_atr))
    target_r = min(2.50, max(1.50, target_r))
    return {"side": side, "stop_atr": stop_atr, "target_r": target_r, "raw": raw}, {**meta, "decision": "TRADE"}
