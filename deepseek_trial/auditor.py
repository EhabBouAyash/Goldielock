"""DeepSeek decision auditor. Copies the engine ticket; never computes levels."""
from __future__ import annotations

import json

import requests

from deepseek_trial.config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_TIMEOUT_SEC,
    MODEL_NAME,
    PACKAGE_DIR,
    deepseek_api_key,
    env_status,
)
from deepseek_trial.ticket import Ticket, parse_ticket, tickets_match, validate_ticket

TOKEN_LOG = PACKAGE_DIR / "books" / "token_usage.jsonl"

SYSTEM = (
    "You are the DeepSeek-supervised auditor for a paper-only trading trial, "
    "spec version 2.0. The trusted engine already computed a complete ticket. "
    "Return one JSON object with exactly those ticket fields. Copy numbers and "
    "enums from the engine ticket. Do not change price levels, volume, side, "
    "or action. Do not add a market-regime opinion, news veto, or short if the "
    "engine ticket is BUY. Do not refuse a VALID_SIGNAL OPEN. Protective exits "
    "are not your job."
)


def _log_tokens(usage: dict, strategy_id: str, event_id: str) -> None:
    TOKEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "model": MODEL_NAME,
        "strategy_id": strategy_id,
        "event_id": event_id,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    with TOKEN_LOG.open("a") as handle:
        handle.write(json.dumps(row) + "\n")


def audit_ticket(engine: Ticket, tick: float) -> tuple[Ticket | None, str, dict]:
    """Ask deepseek-v4-pro to copy `engine`. Returns (model_ticket, reason, meta)."""
    status = env_status()
    meta = {"model": MODEL_NAME, "env": status}
    if not status["key_present"]:
        return None, "TIMEOUT", {**meta, "error": "DEEPSEEK_API_KEY missing"}

    body = {
        "model": MODEL_NAME,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "Copy this engine ticket as JSON with the same keys:\n"
                    + json.dumps(engine.to_json_dict())
                ),
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {deepseek_api_key()}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            DEEPSEEK_BASE_URL,
            headers=headers,
            json=body,
            timeout=DEEPSEEK_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        return None, "TIMEOUT", {**meta, "error": str(exc)}

    usage = payload.get("usage") or {}
    _log_tokens(usage, engine.strategy_id, engine.event_id)
    try:
        content = payload["choices"][0]["message"]["content"]
        raw = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None, "INVALID_OUTPUT", {**meta, "usage": usage}

    ticket, err = parse_ticket(raw)
    if ticket is None:
        return None, err or "INVALID_OUTPUT", {**meta, "usage": usage}
    if validate_ticket(ticket):
        return None, "INVALID_OUTPUT", {**meta, "usage": usage}
    if not tickets_match(engine, ticket, tick):
        return ticket, "STATE_MISMATCH", {**meta, "usage": usage}
    return ticket, "", {**meta, "usage": usage}
