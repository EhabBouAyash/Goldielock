"""CLI pipeline for the deterministic XAUUSD supply/demand strategy."""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from fetch_intraday import fetch_intraday_bars
from fetch_market_data import fetch_dxy_reliable, fetch_xauusd_spot_snapshot
from prop_account_tracker import refresh_prop_account_state

from .engine import StrategyConfig, analyze_market
from .macro_sources import collect_macro_context
from .prompt import SYSTEM_PROMPT


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_BACKLOG = ROOT / "trade_backlog.txt"
LATEST_JSON = PACKAGE_DIR / "latest_analysis.json"
LATEST_TEXT = PACKAGE_DIR / "latest_signal.txt"
ARCHIVE_JSONL = PACKAGE_DIR / "analysis_archive.jsonl"
INTRADAY_CACHE = ROOT / "day_trading_intraday_snapshot.json"
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
MACRO_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "macro_bias": {
            "type": "string",
            "enum": [
                "BULLISH_GOLD",
                "BEARISH_GOLD",
                "NEUTRAL",
                "TWO_WAY",
                "UNKNOWN",
            ],
        },
        "risk_gate": {
            "type": "string",
            "enum": ["ALLOW", "CAUTION", "BLOCK"],
        },
        "confidence": {
            "type": "string",
            "enum": ["HIGH", "MEDIUM", "LOW"],
        },
        "drivers": {"type": "array", "items": {"type": "string"}},
        "event_risk": {"type": "array", "items": {"type": "string"}},
        "evidence_basis": {
            "type": "string",
            "enum": [
                "IMMINENT_EVENT",
                "TIER1_OFFICIAL",
                "CROSS_ASSET_CONSENSUS",
                "HEADLINES_ONLY",
                "INSUFFICIENT",
            ],
        },
        "reason": {"type": "string"},
    },
    "required": [
        "macro_bias",
        "risk_gate",
        "confidence",
        "evidence_basis",
        "reason",
    ],
}


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, "", "n/a"):
            return None
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _extract_interval_records(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    intervals = payload.get("intervals") or {}
    records = {
        timeframe: list((intervals.get(timeframe) or {}).get("candles") or [])
        for timeframe in ("5m", "15m", "1h", "4h")
    }
    if not records["4h"] and records["1h"]:
        records["4h"] = _aggregate_one_hour_to_four_hour(records["1h"])
    return records


def _aggregate_one_hour_to_four_hour(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build completed UTC-aligned 4H candles when the provider lacks a 4H interval."""
    buckets: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        raw = record.get("ts")
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = int(parsed.astimezone(timezone.utc).timestamp())
        except (TypeError, ValueError):
            continue
        bucket = timestamp // (4 * 60 * 60)
        buckets.setdefault(bucket, []).append(record)

    aggregated: list[dict[str, Any]] = []
    for bucket in sorted(buckets):
        group = sorted(buckets[bucket], key=lambda item: str(item.get("ts")))
        if len(group) < 4:
            continue
        volumes = [_safe_float(item.get("volume")) for item in group]
        aggregated.append(
            {
                "ts": group[0].get("ts"),
                "open": _safe_float(group[0].get("open")),
                "high": max(float(item["high"]) for item in group),
                "low": min(float(item["low"]) for item in group),
                "close": _safe_float(group[-1].get("close")),
                "volume": (
                    sum(value for value in volumes if value is not None)
                    if any(value is not None for value in volumes)
                    else None
                ),
            }
        )
    return aggregated


def _has_required_candles(payload: dict[str, Any]) -> bool:
    records = _extract_interval_records(payload)
    return all(records.get(timeframe) for timeframe in ("5m", "15m", "1h", "4h"))


def _load_intraday_cache() -> dict[str, Any]:
    if not INTRADAY_CACHE.exists():
        return {"intervals": {}, "data_source": "unavailable"}
    try:
        payload = json.loads(INTRADAY_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"intervals": {}, "data_source": "unavailable"}
    intraday = payload.get("intraday") if isinstance(payload, dict) else None
    if not isinstance(intraday, dict):
        return {"intervals": {}, "data_source": "unavailable"}
    intraday = deepcopy(intraday)
    intraday["data_source"] = f"cache:{INTRADAY_CACHE.name}"
    return intraday


def _fetch_intraday_with_cache() -> dict[str, Any]:
    live = fetch_intraday_bars(retries=3)
    if _has_required_candles(live):
        live["data_source"] = "live"
        return live
    cached = _load_intraday_cache()
    if _has_required_candles(cached):
        cached["fallback_reason"] = "Live intraday fetch returned incomplete intervals."
        return cached
    live["data_source"] = "live_failed_no_cache"
    return live


def _latest_candle_time(payload: dict[str, Any], timeframe: str = "15m") -> datetime | None:
    records = _extract_interval_records(payload).get(timeframe) or []
    if not records:
        return None
    raw = records[-1].get("ts")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _apply_freshness_gate(
    analysis: dict[str, Any],
    intraday: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    result = deepcopy(analysis)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    latest = _latest_candle_time(intraday)
    age_minutes = (now - latest).total_seconds() / 60.0 if latest else None
    result["data_freshness"] = {
        "source": intraday.get("data_source", "input"),
        "latest_15m_candle": latest.isoformat() if latest else None,
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "max_age_minutes": 45,
    }
    if result.get("status") == "DATA_ERROR":
        return result
    if latest is None or age_minutes is None or age_minutes > 45 or age_minutes < -5:
        result["status"] = "DATA_STALE"
        result["signal"] = "NO_TRADE"
        result["reason"] = (
            "The latest completed 15M candle is missing or stale; cached structure "
            "may be reviewed but cannot authorize an order."
        )
    return result


def _to_spot(value: float | None, basis: float | None) -> float | None:
    if value is None or basis is None:
        return None
    return round(value - basis, 2)


def _convert_zone_to_spot(zone: dict[str, Any], basis: float) -> dict[str, Any]:
    converted = deepcopy(zone)
    for key in ("low", "high", "proximal", "distal"):
        converted[key] = _to_spot(_safe_float(zone.get(key)), basis)
    converted["price_basis"] = "XAUUSD_SPOT"
    return converted


def _live_proxy_price(payload: dict[str, Any]) -> float | None:
    """Latest GC=F trade, including the forming bar, to pair with live spot."""
    for timeframe in ("5m", "15m"):
        records = _extract_interval_records(payload).get(timeframe) or []
        if records:
            return _safe_float(records[-1].get("close"))
    return None


def _apply_spot_basis(
    analysis: dict[str, Any],
    spot_snapshot: dict[str, Any],
    proxy_price: float | None = None,
) -> dict[str, Any]:
    result = deepcopy(analysis)
    spot = _safe_float(spot_snapshot.get("price"))
    # The basis must pair two simultaneous quotes. The last completed 15M close
    # can be up to 30 minutes older than the spot quote.
    proxy = (
        proxy_price
        if proxy_price is not None
        else _safe_float(analysis.get("current_chart_price"))
    )
    basis = round(proxy - spot, 2) if proxy is not None and spot is not None else None
    result["market_price"] = {
        "xauusd_spot": spot,
        "spot_source": spot_snapshot.get("source"),
        "chart_proxy": "GC=F",
        "chart_proxy_close": proxy,
        "basis_proxy_minus_spot": basis,
        "formula": "spot_level = proxy_level - basis",
        "fallback": bool(spot_snapshot.get("fallback")),
    }

    if basis is None or bool(spot_snapshot.get("fallback")):
        if result.get("status") == "ACTIONABLE":
            result["status"] = "RISK_BLOCKED"
            result["signal"] = "NO_TRADE"
            result["reason"] = (
                "Cannot convert chart levels to an independently verified XAUUSD "
                "spot quote; the spot feed is missing or using the same GC=F proxy."
            )
        return result

    result["zones_spot"] = [
        _convert_zone_to_spot(zone, basis) for zone in analysis.get("zones", [])
    ]
    if analysis.get("selected_zone"):
        result["selected_zone_spot"] = _convert_zone_to_spot(
            analysis["selected_zone"], basis
        )
    if analysis.get("candidate_zones"):
        result["candidate_zones_spot"] = [
            _convert_zone_to_spot(zone, basis)
            for zone in analysis["candidate_zones"]
        ]
    if analysis.get("status") == "ACTIONABLE":
        result["execution"] = {
            "price_basis": "XAUUSD_SPOT",
            "order_type": analysis["order_type"],
            "entry": _to_spot(_safe_float(analysis["entry"]), basis),
            "stop_loss": _to_spot(_safe_float(analysis["stop_loss"]), basis),
            "target_1": _to_spot(_safe_float(analysis["target_1"]), basis),
            "reward_risk": analysis["reward_risk"],
            "reward_risk_net": analysis.get("reward_risk_net"),
            "valid_until": analysis.get("valid_until"),
        }
    return result


def _apply_account_gate(
    analysis: dict[str, Any],
    backlog_path: Path,
    config: StrategyConfig,
) -> dict[str, Any]:
    result = deepcopy(analysis)
    account = refresh_prop_account_state(
        backlog_path,
        today=datetime.now(ZoneInfo("Asia/Beirut")).date().isoformat(),
        write_back=False,
    )
    snapshot = account["snapshot_for_agent"]
    result["account_snapshot"] = snapshot
    if result.get("status") != "ACTIONABLE":
        return result

    execution = result.get("execution") or {}
    entry = _safe_float(execution.get("entry"))
    stop = _safe_float(execution.get("stop_loss"))
    equity = _safe_float(snapshot.get("current_equity_usd"))
    max_risk = _safe_float(snapshot.get("recommended_max_risk_usd"))
    blocked = (
        entry is None
        or stop is None
        or equity is None
        or max_risk is None
        or bool(snapshot.get("daily_breached_soft"))
        or bool(snapshot.get("max_loss_breached_soft"))
    )
    if blocked:
        result["status"] = "RISK_BLOCKED"
        result["signal"] = "NO_TRADE"
        result["reason"] = (
            "Prop-account gate blocked execution: verified entry/stop/equity/risk "
            "room is missing or a loss limit is breached."
        )
        return result

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        result["status"] = "RISK_BLOCKED"
        result["signal"] = "NO_TRADE"
        result["reason"] = "Stop distance is non-positive."
        return result
    risk_usd = min(equity * config.risk_fraction, max_risk)
    daily_room = _safe_float(snapshot.get("daily_loss_room_soft_usd")) or 0.0
    max_loss_room = _safe_float(snapshot.get("max_loss_room_soft_usd")) or 0.0
    risk_usd = min(risk_usd, daily_room * 0.5, max_loss_room * 0.25)
    if risk_usd <= 0:
        result["status"] = "RISK_BLOCKED"
        result["signal"] = "NO_TRADE"
        result["reason"] = "No positive risk budget remains."
        return result

    ounces = risk_usd / stop_distance
    notional = ounces * entry
    effective_leverage = notional / equity
    recommended_preset = int(snapshot.get("recommended_tv_leverage") or 1)
    presets = [1, 10, 50, 100]
    preset = next(
        (
            value
            for value in presets
            if value >= effective_leverage and value <= recommended_preset
        ),
        None,
    )
    if preset is None:
        result["status"] = "RISK_BLOCKED"
        result["signal"] = "NO_TRADE"
        result["reason"] = "Required leverage exceeds the account snapshot cap."
        return result

    execution["risk_usd"] = round(risk_usd, 2)
    execution["risk_fraction"] = round(risk_usd / equity, 4)
    execution["xau_ounces"] = round(ounces, 4)
    execution["notional_usd"] = round(notional, 2)
    execution["effective_leverage"] = round(effective_leverage, 2)
    execution["tradingview_leverage"] = preset
    execution["margin_usd"] = round(notional / preset, 2)
    execution["loss_at_stop_usd"] = round(ounces * stop_distance, 2)
    target = _safe_float(execution.get("target_1"))
    execution["profit_at_target_1_usd"] = (
        round(ounces * abs(target - entry), 2) if target is not None else None
    )
    result["execution"] = execution
    return result


def _collect_macro_context() -> dict[str, Any]:
    return collect_macro_context(fetch_dxy_reliable())


def _zone_freshness_bucket(analysis: dict[str, Any]) -> str:
    zone = analysis.get("selected_zone") or {}
    mitigation = _safe_float(zone.get("mitigation_pct"))
    if mitigation is None:
        return "NO_SELECTED_ZONE"
    if mitigation == 0:
        return "FRESH"
    if mitigation < 40:
        return "LIGHTLY_MITIGATED"
    if mitigation < 70:
        return "HEAVILY_MITIGATED"
    return "STALE"


def _macro_request_payload(
    analysis: dict[str, Any],
    macro_context: dict[str, Any],
) -> dict[str, Any]:
    """Sanitize technical context: no prices, indicators, levels, size, or OHLC."""
    regime = analysis.get("regime") or {}
    zone = analysis.get("selected_zone") or {}
    confirmation = analysis.get("confirmation") or {}
    return {
        "categorical_technical_context": {
            "technical_status": analysis.get("status"),
            "technical_direction": analysis.get("signal"),
            "regime": regime.get("regime", "UNKNOWN"),
            "movement_state": regime.get("movement_state", "UNKNOWN"),
            "higher_timeframe_bias": regime.get("four_hour_bias", "MIXED"),
            "zone_type": zone.get("type", "NONE"),
            "zone_formation": zone.get("formation", "NONE"),
            "zone_freshness": _zone_freshness_bucket(analysis),
            "structure_event": zone.get("structure_event", "NONE"),
            "confirmation_state": (
                "COMPLETE" if confirmation else "INCOMPLETE"
            ),
        },
        "macro_context": macro_context,
    }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _gemini_visible_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        prompt_feedback = data.get("promptFeedback") or data.get("error") or data
        raise ValueError(f"Gemini returned no candidates: {prompt_feedback}")
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    chunks = [
        str(part.get("text") or "")
        for part in parts
        if part.get("text") and not part.get("thought")
    ]
    text = "\n".join(chunk for chunk in chunks if chunk).strip()
    if not text:
        raise ValueError("Gemini returned empty visible text")
    return text


def _macro_evaluation(payload: dict[str, Any]) -> dict[str, Any]:
    load_dotenv(ROOT / ".env", override=True)
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {
            "macro_bias": "UNKNOWN",
            "risk_gate": "BLOCK",
            "confidence": "LOW",
            "drivers": [],
            "event_risk": [],
            "evidence_basis": "INSUFFICIENT",
            "system_error": True,
            "reason": "GEMINI_API_KEY is missing; requested macro gate unavailable.",
            "model": GEMINI_MODEL,
        }
    try:
        response = requests.post(
            GEMINI_URL,
            headers={
                "x-goog-api-key": api_key,
                "content-type": "application/json",
            },
            json={
                "system_instruction": {
                    "parts": [{"text": SYSTEM_PROMPT}],
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": json.dumps(payload, indent=2, default=str),
                            }
                        ],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseSchema": MACRO_RESPONSE_SCHEMA,
                    "maxOutputTokens": 4096,
                    "thinkingConfig": {"thinkingLevel": "low"},
                },
            },
            timeout=180,
        )
        response.raise_for_status()
        data = response.json()
        parsed = _extract_json_object(_gemini_visible_text(data))
        if not parsed:
            raise ValueError("Gemini did not return a JSON object")
        parsed["model"] = GEMINI_MODEL
        usage = data.get("usageMetadata") or {}
        parsed["token_usage"] = {
            "input_tokens": usage.get("promptTokenCount"),
            "output_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
        }
        return parsed
    except Exception as exc:
        return {
            "macro_bias": "UNKNOWN",
            "risk_gate": "BLOCK",
            "confidence": "LOW",
            "drivers": [],
            "event_risk": [],
            "evidence_basis": "INSUFFICIENT",
            "system_error": True,
            "reason": f"Macro evaluation failed: {exc}",
            "model": GEMINI_MODEL,
        }


def _apply_macro_gate(
    analysis: dict[str, Any],
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    result = deepcopy(analysis)
    result["macro_evaluation"] = evaluation
    if result.get("status") != "ACTIONABLE":
        return result
    direction = result.get("signal")
    bias = str(evaluation.get("macro_bias", "UNKNOWN")).upper()
    gate = str(evaluation.get("risk_gate", "BLOCK")).upper()
    valid_biases = {
        "BULLISH_GOLD",
        "BEARISH_GOLD",
        "NEUTRAL",
        "TWO_WAY",
        "UNKNOWN",
    }
    if bias not in valid_biases:
        bias = "UNKNOWN"
    if gate not in {"ALLOW", "CAUTION", "BLOCK"}:
        gate = "BLOCK"
    evidence_basis = str(
        evaluation.get("evidence_basis", "INSUFFICIENT")
    ).upper()
    trusted_block_bases = {
        "IMMINENT_EVENT",
        "TIER1_OFFICIAL",
        "CROSS_ASSET_CONSENSUS",
    }
    if evidence_basis not in trusted_block_bases | {
        "HEADLINES_ONLY",
        "INSUFFICIENT",
    }:
        evidence_basis = "INSUFFICIENT"
    if (
        gate == "BLOCK"
        and evidence_basis not in trusted_block_bases
        and not evaluation.get("system_error")
    ):
        gate = "CAUTION"
    result["macro_evaluation"]["macro_bias"] = bias
    result["macro_evaluation"]["risk_gate"] = gate
    result["macro_evaluation"]["evidence_basis"] = evidence_basis
    opposing = (
        direction == "LONG" and bias == "BEARISH_GOLD"
    ) or (
        direction == "SHORT" and bias == "BULLISH_GOLD"
    )
    confidence = str(evaluation.get("confidence", "LOW")).upper()
    trusted_opposition = (
        opposing
        and confidence == "HIGH"
        and evidence_basis in trusted_block_bases
    )
    if gate == "BLOCK" or trusted_opposition:
        result["status"] = "MACRO_BLOCKED"
        result["signal"] = "NO_TRADE"
        result["reason"] = (
            "Gemini macro-only gate blocked the deterministic setup."
            if gate == "BLOCK"
            else "Macro bias opposes the deterministic technical direction."
        )
        result.pop("execution", None)
    return result


def _format_signal(analysis: dict[str, Any]) -> str:
    regime_data = analysis.get("regime") or {}
    regime = regime_data.get("regime", "UNKNOWN")
    movement_state = regime_data.get("movement_state", "UNKNOWN")
    lines = [
        "SUPPLY / DEMAND XAUUSD PIPELINE",
        f"Generated: {analysis.get('generated_at')}",
        f"Status: {analysis.get('status')}",
        f"Signal: {analysis.get('signal')}",
        f"Regime: {regime} | Movement state: {movement_state}",
        f"Reason: {analysis.get('reason', 'All deterministic gates passed.')}",
    ]
    zone = analysis.get("selected_zone_spot")
    if zone:
        lines.extend(
            [
                (
                    f"Zone: {zone['type']} {zone['formation']} "
                    f"${zone['low']:.2f}-${zone['high']:.2f} "
                    f"({zone['timeframe']}, score={zone['score']}, "
                    f"mitigation={zone['mitigation_pct']:.1f}%, "
                    f"freshness={zone['freshness_pct']:.1f}%, "
                    f"structure={zone['structure_event']})"
                )
            ]
        )
    execution = analysis.get("execution")
    if analysis.get("status") == "ACTIONABLE" and execution:
        lines.extend(
            [
                f"Order: {execution['order_type']} ${execution['entry']:.2f}",
                f"Stop: ${execution['stop_loss']:.2f}",
                (
                    f"Target 1: ${execution['target_1']:.2f} "
                    f"({execution['reward_risk']}R gross, "
                    f"{execution.get('reward_risk_net')}R after costs)"
                ),
                f"Cancel if not triggered by: {execution.get('valid_until')}",
                (
                    f"Size: {execution['xau_ounces']:.4f} oz | "
                    f"Risk: ${execution['risk_usd']:.2f} | "
                    f"TV leverage: {execution['tradingview_leverage']}:1"
                ),
            ]
        )
    confirmation = analysis.get("confirmation") or {}
    if confirmation:
        pattern = (confirmation.get("candlestick_pattern") or {}).get("name")
        crossover = confirmation.get("ema_crossover") or {}
        lines.append(
            f"Confirmation: {pattern} + 9/21 EMA {crossover.get('direction')} crossover "
            f"on {confirmation.get('execution_timeframe')}"
        )
    macro = analysis.get("macro_evaluation")
    if macro:
        lines.extend(
            [
                (
                    f"Macro gate: {macro.get('risk_gate')} | "
                    f"Bias: {macro.get('macro_bias')} | "
                    f"Confidence: {macro.get('confidence')} | "
                    f"Model: {macro.get('model', 'gemini-3.8-flash')}"
                ),
                f"Macro reason: {macro.get('reason')}",
            ]
        )
    return "\n".join(lines) + "\n"


def run_pipeline(
    *,
    input_path: Path | None = None,
    with_macro: bool = False,
    backlog_path: Path = DEFAULT_BACKLOG,
    config: StrategyConfig | None = None,
) -> dict[str, Any]:
    config = config or StrategyConfig()
    if input_path:
        intraday = json.loads(input_path.read_text(encoding="utf-8"))
        if isinstance(intraday.get("intraday"), dict):
            intraday = intraday["intraday"]
        intraday["data_source"] = f"input:{input_path.name}"
    else:
        intraday = _fetch_intraday_with_cache()
    spot = fetch_xauusd_spot_snapshot()
    raw = analyze_market(_extract_interval_records(intraday), config=config)
    raw = _apply_freshness_gate(raw, intraday)
    analysis = _apply_spot_basis(raw, spot, _live_proxy_price(intraday))
    analysis = _apply_account_gate(analysis, backlog_path, config)
    analysis["generated_at"] = datetime.now(timezone.utc).isoformat()
    analysis["pipeline"] = "supply_demand_strat.v2"
    if with_macro:
        macro_context = _collect_macro_context()
        macro_payload = _macro_request_payload(analysis, macro_context)
        analysis = _apply_macro_gate(
            analysis,
            _macro_evaluation(macro_payload),
        )
    return analysis


def persist(analysis: dict[str, Any]) -> None:
    payload = json.dumps(analysis, indent=2, default=str)
    LATEST_JSON.write_text(payload + "\n", encoding="utf-8")
    LATEST_TEXT.write_text(_format_signal(analysis), encoding="utf-8")
    with ARCHIVE_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(analysis, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the XAUUSD supply/demand strategy pipeline."
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Optional intraday JSON fixture; otherwise fetch live bars.",
    )
    parser.add_argument(
        "--macro",
        "--gemini",
        "--opus",
        dest="with_macro",
        action="store_true",
        help=(
            "Ask Gemini 3.8 Flash for a sanitized macro-only veto. "
            "--opus is kept as an alias and does not call Anthropic."
        ),
    )
    parser.add_argument(
        "--backlog",
        type=Path,
        default=DEFAULT_BACKLOG,
        help="Trade backlog used for the prop-account risk gate.",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Print only; do not update latest/archive files.",
    )
    args = parser.parse_args()
    analysis = run_pipeline(
        input_path=args.input,
        with_macro=args.with_macro,
        backlog_path=args.backlog,
    )
    if not args.no_persist:
        persist(analysis)
    print(_format_signal(analysis), end="")
    return 0 if analysis.get("status") != "DATA_ERROR" else 2


if __name__ == "__main__":
    raise SystemExit(main())
