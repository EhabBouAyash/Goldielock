"""
DAY TRADING GOLD INSIGHTS (TOP-DOWN SCORING SYSTEM)
====================================================

Intraday trading script for spot gold (XAUUSD) using a TOP-DOWN scoring hierarchy:

1. MACRO SIGNALS (highest weight) - News, Fed, DXY, real yields, liquidity events
2. 4H CHART (primary bias confirmation) - Trend direction, key levels
3. 1H CHART (session/swing bridge) - Confirms whether bias is transmitting
4. 15M CHART (structure or execution) - Pattern formation, cleaner intraday entries
5. 5M CHART (optional execution) - Precise scalp trigger when cleaner than 15m

Optimized for FundedNext Stellar 1-Step CFD ($25K) and TradingView paper execution.

Usage:
    python3 day_trading_insights.py --option trading
    python3 day_trading_insights.py --option default
"""

import argparse
import contextlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import date, datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo
 

import requests
from dotenv import load_dotenv

try:
    from langgraph.graph import END, StateGraph
    HAS_LANGGRAPH = True
except ImportError:
    local_site_packages = (
        Path(__file__).resolve().parent
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if local_site_packages.exists():
        sys.path.insert(0, str(local_site_packages))
    try:
        from langgraph.graph import END, StateGraph
        HAS_LANGGRAPH = True
    except ImportError:
        END = "__end__"
        StateGraph = None
        HAS_LANGGRAPH = False

# Import data fetching functions from generate_insights.py. That module still
# performs some legacy Yahoo lookups at import time, so keep those diagnostics
# out of this agent's terminal output.
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from generate_insights import (
        fetch_gold_price,
        fetch_dxy,
        fetch_macro,
        fetch_treasury_snapshot,
        fetch_fed_balance_sheet_snapshot,
        parse_gold_signal,
        update_signal_log,
        append_model_trade_log,
        _ensure_manual_trade_backlog_file,
        _read_recent_text,
        MODEL_TRADE_LOG_TXT_PATH,
        MANUAL_TRADE_BACKLOG_TXT_PATH,
        SIGNAL_LOG_PKL_PATH,
        SIGNAL_LOG_CSV_PATH,
        GNEWS_API_KEY,
    )

# Import additional news (with fallback if not available)
try:
    from additional_news import fetch_all as fetch_additional_metals_news
    from additional_news import format_for_claude as format_additional_news_for_prompt
    HAS_ADDITIONAL_NEWS = True
except ImportError:
    HAS_ADDITIONAL_NEWS = False

# Import intraday data fetcher (5m/15m/1h/4h bars)
from fetch_intraday import fetch_intraday_bars

# Import token tracker
from token_tracker import log_token_usage, calculate_cost

from prop_account_tracker import refresh_prop_account_state
from day_trading_prompt_rules import (
    DISRUPTION_SCOUT_SYSTEM_PROMPT,
    DAY_TRADING_SYSTEM_PROMPT,
)

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Day trading specific paths
DAY_TRADING_INSIGHTS_PATH = Path(__file__).resolve().parent / "day_trading_insights.txt"
DAY_TRADING_LIVE_DATA_PATH = Path(__file__).resolve().parent / "day_trading_live_data.txt"
DISRUPTION_EVENTS_PATH = Path(__file__).resolve().parent / "gold_disruption_events.json"
AGENT_STATE_PATH = Path(__file__).resolve().parent / "gold_agent_state.json"
LOOPBACK_EVALUATIONS_PATH = Path(__file__).resolve().parent / "gold_loopback_evaluations.jsonl"
DAY_TRADING_INTRADAY_SNAPSHOT_PATH = Path(__file__).resolve().parent / "day_trading_intraday_snapshot.json"
DAY_TRADING_INTRADAY_ARCHIVE_PATH = Path(__file__).resolve().parent / "day_trading_intraday_archive.jsonl"

SPOT_GOLD_SYMBOL = "XAUUSD"
SPOT_GOLD_YF_SYMBOL = "XAUUSD=X"

# FundedNext Stellar 1-Step CFD — $25,000 account (help.fundednext.com, Mar 2026)
PROP_ACCOUNT_SIZE = 25_000
PROP_DAILY_LOSS_SOFT_LIMIT_PCT = 0.025  # $625 buffer below hard 3% daily cap
PROP_DAILY_LOSS_HARD_LIMIT_PCT = 0.03   # $750 — FundedNext hard daily loss
PROP_TOTAL_DRAWDOWN_SOFT_LIMIT_PCT = 0.05  # $1,250 buffer below hard 6% max loss
PROP_TOTAL_DRAWDOWN_HARD_LIMIT_PCT = 0.06  # $1,500 — account floor $23,500
PROP_PROFIT_TARGET_PCT = 0.10  # $2,500 challenge profit target
PROP_MIN_TRADING_DAYS = 2
PROP_MIN_TARGET1_PROFIT_USD = 200  # minimum $ win on aligned Target 1 hits
PROP_DAILY_ROOM_USE_PCT = 0.95  # size each trade to ~95% of remaining daily loss room
PROP_PREFERRED_TV_LEVERAGE = 100  # default aligned execution preset when room allows
MT5_ACCOUNT_LEVERAGE = 100  # FundedNext / MT5 standard account leverage
MT5_OZ_PER_STANDARD_LOT = 100  # XAUUSD: 1 standard lot = 100 oz on typical MT5 brokers

# TradingView paper accounts only expose these leverage presets
TRADINGVIEW_LEVERAGE_OPTIONS = (1, 10, 50, 100)

CLAUDE_SEARCH_PLANNER_SYSTEM_PROMPT = """You are the live gold catalyst search
planner for an intraday XAUUSD spot gold prop-firm agent.

Use your market knowledge to decide where a gold trader should look right now
for high-impact information. Return JSON only with:
{
  "queries": ["8-12 focused live-news search queries"],
  "high_impact_watchlist": [
    {
      "topic": "event/data type/source area",
      "why_gold_cares": "concise transmission mechanism",
      "impact_threshold": "what historically makes it high impact",
      "directional_risk": "BULLISH_GOLD/BEARISH_GOLD/TWO_WAY"
    }
  ],
  "impact_rubric": [
    {"data_type": "category", "rank": 1, "reason": "why it matters intraday"}
  ]
}

Rules:
- Do not claim remembered headlines are current facts.
- Build queries from the supplied market snapshot, prior signal feedback, and
  historical gold drivers: Fed path, real yields, DXY, CPI/PCE/NFP, Treasury
  auctions, Forex Factory high-impact USD events, geopolitical shocks, banking
  stress, sanctions, ceasefires, central bank gold demand, and liquidity events.
- Keep quantifiable values out of the watchlist unless they were supplied.
"""


METRIC_RECOVERY_SYSTEM_PROMPT = """You are the metric recovery planner for an
intraday XAUUSD spot gold prop-firm agent.

The caller will provide quantifiable metrics that failed retrieval or look
invalid. Return JSON only with:
{
  "missing_metrics": [
    {
      "metric": "name",
      "preferred_static_sources": ["source/symbol/API to check"],
      "search_queries": ["targeted searches to initiate"],
      "validation_method": "how to confirm the value is current and correct",
      "do_not_infer": true
    }
  ]
}

Rules:
- Never invent current values for XAUUSD spot, DXY, RSI, moving averages,
  yields, or other quantifiable metrics.
- Prefer structured sources and recomputation from OHLCV data over articles.
- For XAUUSD, validate that the value is spot gold versus USD.
"""


CLAUDE_EVENT_RANKER_SYSTEM_PROMPT = """You rank candidate live gold-market
headlines for an intraday XAUUSD spot gold prop-firm agent.

Use the supplied headline/title, description, source, query, market snapshot,
and Claude search plan. Return JSON only with:
{
  "events": [
    {
      "title": "exact supplied title",
      "disruption_score": 0,
      "direction_hint": "BULLISH_GOLD/BEARISH_GOLD/TWO_WAY/UNKNOWN",
      "claude_impact_reason": "concise reason",
      "historical_impact_basis": "what historically makes this high/low impact"
    }
  ]
}

Rules:
- Do not assert a headline is true beyond the supplied candidate text.
- Rank high impact only when there is a plausible near-term transmission path
  through Fed path, real yields, DXY, inflation expectations, liquidity,
  risk sentiment, or safe-haven demand.
- Penalize stale, generic, low-source-quality, or non-transmitting headlines.
"""


class GoldAgentState(TypedDict, total=False):
    cycle: int
    trading_log_enabled: bool
    market_data: dict[str, Any]
    intraday: dict[str, Any]
    scout_queries: list[str]
    claude_search_plan: dict[str, Any]
    metric_recovery_plan: dict[str, Any]
    loopback_evaluation: dict[str, Any]
    disruptive_events: list[dict[str, Any]]
    user_message: str
    insights_text: str
    signal_data: dict[str, Any]
    current_gold_price: float | None
    errors: list[str]


def estimate_prompt_tokens(text: str) -> int:
    """Rough estimate: ~4 chars = 1 token."""
    return len(text) // 4


def _append_error(state: GoldAgentState, message: str) -> list[str]:
    return [*state.get("errors", []), message]


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, "", "n/a"):
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _safe_fetch(label: str, fetcher, fallback: Any) -> tuple[Any, str | None]:
    try:
        return fetcher(), None
    except Exception as exc:
        return fallback, f"{label}: {exc}"


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _valid_positive_float(value: Any) -> bool:
    parsed = _safe_float(value)
    return parsed is not None and parsed > 0


def map_effective_leverage_to_tradingview(
    effective_leverage: float | None,
    *,
    prefer_leverage: int | None = None,
) -> dict[str, Any]:
    """
    Map calculated effective leverage to TradingView's allowed presets: 1:1, 10:1, 50:1, 100:1.

    When prefer_leverage is set (e.g. 100 from PROP ACCOUNT SNAPSHOT), use it if it still
    covers the required effective leverage; otherwise pick the smallest preset that fits.
    """
    if effective_leverage is None or effective_leverage <= 0:
        return {
            "effective_leverage": effective_leverage,
            "tradingview_leverage": 1,
            "tradingview_label": "1:1",
            "note": "No positive effective leverage; default to 1:1 on TradingView.",
        }

    lev = float(effective_leverage)
    if prefer_leverage and prefer_leverage in TRADINGVIEW_LEVERAGE_OPTIONS and prefer_leverage >= lev:
        chosen = prefer_leverage
        note = (
            f"Required ~{lev:.2f}x; using preferred {chosen}:1 on TradingView "
            f"(snapshot-aligned, covers required leverage)."
        )
    else:
        chosen = TRADINGVIEW_LEVERAGE_OPTIONS[-1]
        for option in TRADINGVIEW_LEVERAGE_OPTIONS:
            if option >= lev:
                chosen = option
                break
        note = f"Required ~{lev:.2f}x; use {chosen}:1 on TradingView (smallest preset that fits)."

    if lev > TRADINGVIEW_LEVERAGE_OPTIONS[-1]:
        note = (
            f"Required ~{lev:.2f}x exceeds TradingView max 100:1; cap size or use 100:1 "
            "and verify margin manually."
        )

    return {
        "effective_leverage": round(lev, 2),
        "tradingview_leverage": chosen,
        "tradingview_label": f"{chosen}:1",
        "note": note,
    }


def _fast_info_get(fast_info: Any, *keys: str) -> Any:
    for key in keys:
        try:
            if isinstance(fast_info, dict) and key in fast_info:
                return fast_info[key]
            value = getattr(fast_info, key)
            if value is not None:
                return value
        except Exception:
            continue
    return None


def _to_iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if hasattr(value, "isoformat"):
            return value.isoformat()
    except Exception:
        pass
    return str(value)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(cleaned[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _is_missing_quant(value: Any, invalid_zero: bool = False) -> bool:
    if value in (None, "", "n/a", "N/A"):
        return True
    parsed = _safe_float(value)
    return parsed is None or (invalid_zero and parsed == 0)


def identify_missing_quant_metrics(
    market_data: dict[str, Any],
    intraday: dict[str, Any],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    intraday_symbol = intraday.get("symbol") or SPOT_GOLD_YF_SYMBOL
    intraday_basis = intraday.get("price_basis") or "XAUUSD spot intraday bars"

    def add_if_missing(name: str, value: Any, source: str, invalid_zero: bool = False) -> None:
        if _is_missing_quant(value, invalid_zero=invalid_zero):
            missing.append({"metric": name, "source": source, "value": value})

    add_if_missing("XAUUSD spot price", market_data.get("gold_spot", {}).get("price"), "gold-api XAU", True)
    add_if_missing("DXY value", market_data.get("dxy", {}).get("value"), "yfinance DX-Y.NYB", True)
    add_if_missing("10Y real yield DFII10", market_data.get("real_yield_dfii10"), "FRED DFII10")
    add_if_missing("10Y breakeven T10YIE", market_data.get("breakeven_t10yie"), "FRED T10YIE")

    treasury = market_data.get("treasury", {})
    add_if_missing("10Y Treasury yield", _treasury_series(treasury, "dgs10", "y10").get("value"), "FRED DGS10")
    add_if_missing("2Y Treasury yield", _treasury_series(treasury, "dgs2", "y2").get("value"), "FRED DGS2")

    for timeframe, bar_data in intraday.get("intervals", {}).items():
        if bar_data.get("error"):
            missing.append({
                "metric": f"{timeframe} XAUUSD OHLCV/technicals",
                "source": f"yfinance {intraday_symbol} ({intraday_basis})",
                "value": bar_data.get("error"),
            })
            continue

        for metric in ("current_price", "sma_9", "sma_21", "sma_50", "rsi"):
            add_if_missing(
                f"{timeframe} {metric}",
                bar_data.get(metric),
                f"computed from yfinance {intraday_symbol} ({intraday_basis})",
                invalid_zero=metric != "rsi",
            )

    return missing


def _treasury_series(treasury: dict[str, Any], nested_key: str, flat_prefix: str) -> dict[str, Any]:
    """Normalize Treasury snapshots from older flat and newer nested schemas."""
    nested = treasury.get(nested_key, {})
    if isinstance(nested, dict) and nested:
        return nested

    return {
        "value": treasury.get(f"{flat_prefix}_latest"),
        "change_1d_bps": treasury.get(f"{flat_prefix}_daily_change_bps"),
        "change_5d_bps": treasury.get(f"{flat_prefix}_weekly_change_bps"),
        "error": treasury.get(f"{flat_prefix}_error"),
    }


def _treasury_tga(treasury: dict[str, Any]) -> dict[str, Any]:
    nested = treasury.get("tga", {})
    if isinstance(nested, dict) and nested:
        return nested

    return {
        "value_billions": treasury.get("tga_latest_billions"),
        "prior_billions": treasury.get("tga_prior_billions"),
        "weekly_change_billions": treasury.get("tga_weekly_change_billions"),
        "error": treasury.get("tga_error"),
    }


def build_metric_recovery_plan(
    missing_metrics: list[dict[str, Any]],
    market_data: dict[str, Any],
) -> dict[str, Any]:
    if not missing_metrics:
        return {"status": "all_quant_metrics_available", "missing_metrics": []}

    user_payload = {
        "timestamp_et": market_data.get("timestamp_et"),
        "missing_metrics": missing_metrics,
        "known_sources": {
            "spot_gold": "gold-api XAU",
            "xauusd_intraday_technicals": f"yfinance {SPOT_GOLD_YF_SYMBOL}",
            "dxy": "yfinance DX-Y.NYB",
            "fred": ["DFII10", "T10YIE", "DGS10", "DGS2"],
            "intraday_technicals": f"computed from yfinance {SPOT_GOLD_YF_SYMBOL} OHLCV bars",
        },
    }

    response = call_anthropic(
        METRIC_RECOVERY_SYSTEM_PROMPT,
        json.dumps(user_payload, indent=2, default=str),
        mode="metric_recovery_planning",
    )
    parsed = _extract_json_object(response)
    if parsed is None:
        return {
            "status": "claude_metric_recovery_unparsed",
            "missing_metrics": missing_metrics,
            "raw_response": response,
        }

    parsed.setdefault("missing_metrics", missing_metrics)
    parsed["status"] = "claude_metric_recovery_plan"
    return parsed


def _load_previous_agent_snapshot() -> dict[str, Any]:
    if not AGENT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(AGENT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _signal_direction(signal: Any) -> int:
    normalized = str(signal or "").upper()
    if normalized == "LONG":
        return 1
    if normalized == "SHORT":
        return -1
    return 0


def _ranked_factor(
    data_type: str,
    score: float,
    evidence: str,
    verdict: str,
) -> dict[str, Any]:
    return {
        "data_type": data_type,
        "importance_score": round(max(0, min(score, 10)), 2),
        "evidence": evidence,
        "verdict": verdict,
    }


def build_loopback_evaluation(
    previous_snapshot: dict[str, Any],
    market_data: dict[str, Any],
    intraday: dict[str, Any],
    current_spot_gold_price: float | None,
) -> dict[str, Any]:
    current_time = market_data.get("timestamp_et")
    if not previous_snapshot:
        return {
            "status": "no_prior_signal_to_evaluate",
            "timestamp_et": current_time,
            "current_spot_gold_price": current_spot_gold_price,
            "message": "First cycle with no persisted prior agent state.",
        }

    previous_signal_data = previous_snapshot.get("signal_data", {})
    previous_market = previous_snapshot.get("market_data_snapshot", {})
    previous_price = _first_float(
        previous_snapshot.get("current_spot_gold_price"),
        previous_market.get("gold_spot", {}).get("price"),
        previous_snapshot.get("current_gold_price"),
    )
    previous_verified_spot = (
        previous_snapshot.get("current_gold_price_symbol") == SPOT_GOLD_SYMBOL
        or previous_market.get("gold_spot", {}).get("symbol") == SPOT_GOLD_SYMBOL
    )

    signal = previous_signal_data.get("signal")
    direction = _signal_direction(signal)
    price_delta = (
        current_spot_gold_price - previous_price
        if current_spot_gold_price is not None and previous_price is not None
        else None
    )
    price_delta_pct = (
        round(price_delta / previous_price * 100, 3)
        if price_delta is not None and previous_price not in (None, 0)
        else None
    )

    if price_delta is None:
        outcome = "UNRANKED_NO_XAUUSD_COMPARISON"
    elif direction == 0:
        outcome = "NEUTRAL_OK" if abs(price_delta_pct or 0) < 0.15 else "NEUTRAL_MISSED_MOVE"
    elif price_delta * direction > 0:
        outcome = "ALIGNED_WITH_SIGNAL"
    elif abs(price_delta_pct or 0) < 0.05:
        outcome = "FLAT_AFTER_SIGNAL"
    else:
        outcome = "AGAINST_SIGNAL"

    ranked: list[dict[str, Any]] = []
    low_weight: list[dict[str, Any]] = []

    if price_delta_pct is not None:
        score = min(abs(price_delta_pct) * 18, 10)
        ranked.append(_ranked_factor(
            "XAUUSD spot price action",
            score,
            f"{previous_price} -> {current_spot_gold_price} ({price_delta_pct}%)",
            "Price movement is the final arbiter for whether the prior thesis transmitted.",
        ))

    prev_dxy = _first_float(previous_market.get("dxy", {}).get("value"))
    curr_dxy = _first_float(market_data.get("dxy", {}).get("value"))
    if prev_dxy and curr_dxy:
        dxy_delta_pct = (curr_dxy - prev_dxy) / prev_dxy * 100
        alignment = (
            "aligned with gold move"
            if price_delta is not None and price_delta * -dxy_delta_pct > 0
            else "not clearly aligned"
        )
        ranked.append(_ranked_factor(
            "DXY",
            min(abs(dxy_delta_pct) * 20, 10),
            f"{prev_dxy} -> {curr_dxy} ({dxy_delta_pct:.3f}%), {alignment}",
            "Dollar moves usually matter when the intraday change is large enough to transmit to gold.",
        ))
    else:
        low_weight.append(_ranked_factor("DXY", 0, "Missing previous or current DXY", "Cannot score."))

    prev_real_yield = _first_float(previous_market.get("real_yield_dfii10"))
    curr_real_yield = _first_float(market_data.get("real_yield_dfii10"))
    if prev_real_yield is not None and curr_real_yield is not None:
        real_yield_delta_bps = (curr_real_yield - prev_real_yield) * 100
        ranked.append(_ranked_factor(
            "10Y real yield",
            min(abs(real_yield_delta_bps) * 1.5, 10),
            f"{prev_real_yield}% -> {curr_real_yield}% ({real_yield_delta_bps:.1f} bps)",
            "Real yields are high-impact for gold when they change materially, but daily FRED data can be stale intraday.",
        ))
    else:
        low_weight.append(_ranked_factor("10Y real yield", 0, "Missing previous or current real yield", "Cannot score."))

    intervals = intraday.get("intervals", {})
    chart_availability = [
        tf for tf, data in intervals.items()
        if not data.get("error") and data.get("current_price") is not None
    ]
    if chart_availability:
        tensor = intraday.get("confluence_tensor", {})
        aggregate_score = _safe_float(tensor.get("aggregate_score"))
        tensor_strength = abs(aggregate_score) if aggregate_score is not None else 0
        ranked.append(_ranked_factor(
            "XAUUSD intraday confluence tensor",
            min(max(tensor_strength, 3), 8) if chart_availability else 0,
            (
                f"Available timeframes: {', '.join(chart_availability)}; "
                f"aggregate tensor score {tensor.get('aggregate_score', 'n/a')} "
                f"({tensor.get('alignment_label', 'n/a')})"
            ),
            "RSI/MAs/candle momentum are now scored as a normalized tensor for entry timing and confirmation.",
        ))
    else:
        low_weight.append(_ranked_factor("XAUUSD intraday confluence tensor", 0, "No usable RSI/MA chart data", "Cannot score."))

    previous_events = previous_snapshot.get("top_disruptive_events", [])
    max_event_score = max((event.get("disruption_score", 0) for event in previous_events), default=0)
    failed_event_count = sum(1 for event in previous_events if "failed" in str(event.get("title", "")).lower())
    if max_event_score >= 4:
        ranked.append(_ranked_factor(
            "live catalyst/news flow",
            max_event_score,
            f"Prior max disruption score {max_event_score}/10",
            "Fresh catalysts matter when source, freshness, and transmission mechanism are clear.",
        ))
    else:
        low_weight.append(_ranked_factor(
            "live catalyst/news flow",
            max_event_score,
            f"Prior max disruption score {max_event_score}/10; failed scouts: {failed_event_count}",
            "Low score, stale, or failed news candidates should not drive the model.",
        ))

    fed_change = _first_float(
        market_data.get("fed_balance_sheet", {}).get("four_week_change_billions"),
        market_data.get("fed_balance_sheet", {}).get("change_4w_billions"),
    )
    if fed_change is not None:
        target = ranked if abs(fed_change) >= 50 else low_weight
        target.append(_ranked_factor(
            "Fed balance sheet/liquidity",
            min(abs(fed_change) / 10, 10),
            f"4-week change {fed_change}B",
            "Useful macro context, but usually lower intraday weight unless the liquidity shift is large.",
        ))

    ranked.sort(key=lambda item: item["importance_score"], reverse=True)
    low_weight.sort(key=lambda item: item["importance_score"])

    return {
        "status": "evaluated_prior_signal",
        "timestamp_et": current_time,
        "previous_timestamp": previous_snapshot.get("timestamp"),
        "previous_signal": signal,
        "previous_confidence": previous_signal_data.get("confidence"),
        "previous_spot_gold_price": previous_price,
        "previous_price_verified_spot": previous_verified_spot,
        "current_spot_gold_price": current_spot_gold_price,
        "price_change": round(price_delta, 2) if price_delta is not None else None,
        "price_change_pct": price_delta_pct,
        "signal_outcome": outcome,
        "ranked_data_influence": ranked,
        "low_or_no_weight_data": low_weight,
        "scoring_feedback": (
            "Increase weight on the highest-ranked data types only when they aligned with the "
            "actual XAUUSD spot move; reduce weight on failed, stale, or non-transmitting inputs."
        ),
    }


def build_claude_search_plan(
    market_data: dict[str, Any],
    intraday: dict[str, Any],
    loopback_evaluation: dict[str, Any],
) -> dict[str, Any]:
    user_payload = {
        "timestamp_et": market_data.get("timestamp_et"),
        "gold_spot": market_data.get("gold_spot"),
        "dxy": market_data.get("dxy"),
        "real_yield_dfii10": market_data.get("real_yield_dfii10"),
        "breakeven_t10yie": market_data.get("breakeven_t10yie"),
        "treasury": market_data.get("treasury"),
        "fed_balance_sheet": market_data.get("fed_balance_sheet"),
        "intraday_summary": {
            tf: {
                "price": data.get("current_price"),
                "rsi": data.get("rsi"),
                "sma_9": data.get("sma_9"),
                "sma_21": data.get("sma_21"),
                "sma_50": data.get("sma_50"),
                "bars_count": data.get("bars_count"),
                "confluence_vector": data.get("confluence_vector"),
                "confluence_score": data.get("confluence_score"),
                "error": data.get("error"),
            }
            for tf, data in intraday.get("intervals", {}).items()
        },
        "confluence_tensor": intraday.get("confluence_tensor"),
        "loopback_evaluation": loopback_evaluation,
    }
    response = call_anthropic(
        CLAUDE_SEARCH_PLANNER_SYSTEM_PROMPT,
        json.dumps(user_payload, indent=2, default=str),
        mode="claude_search_planning",
    )
    parsed = _extract_json_object(response)
    if parsed is None:
        return {
            "status": "claude_search_plan_unparsed",
            "queries": build_disruption_queries(market_data),
            "high_impact_watchlist": [],
            "impact_rubric": [],
            "raw_response": response,
        }

    queries = [str(query).strip() for query in parsed.get("queries", []) if str(query).strip()]
    if not queries:
        queries = build_disruption_queries(market_data)

    parsed["queries"] = queries[:12]
    parsed["status"] = "claude_search_plan"
    return parsed


def _claude_watchlist_event(search_plan: dict[str, Any]) -> dict[str, Any] | None:
    watchlist = search_plan.get("high_impact_watchlist") or []
    if not watchlist:
        return None
    return {
        "title": "Claude high-impact gold catalyst watchlist",
        "description": json.dumps(watchlist[:8], indent=2, default=str),
        "source": "claude_market_knowledge",
        "published_at": datetime.now().isoformat(),
        "url": "",
        "query": "Claude-generated high-impact watchlist, not a live headline",
        "disruption_score": 6,
        "direction_hint": "TWO_WAY",
        "impact_scored_by": "claude",
        "claude_impact_reason": "Claude-generated map of historically high-impact gold catalysts to monitor.",
    }


def claude_rank_disruption_events(
    events: list[dict[str, Any]],
    search_plan: dict[str, Any],
    market_data: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates = [
        {
            "title": event.get("title"),
            "description": event.get("description"),
            "source": event.get("source"),
            "published_at": event.get("published_at"),
            "query": event.get("query"),
            "static_disruption_score": event.get("disruption_score"),
            "static_direction_hint": event.get("direction_hint"),
        }
        for event in events
        if event.get("title")
        and "failed" not in str(event.get("title", "")).lower()
        and event.get("source") != "claude_market_knowledge"
    ][:20]

    if not candidates:
        return events

    user_payload = {
        "market_snapshot": {
            "timestamp_et": market_data.get("timestamp_et"),
            "gold_spot": market_data.get("gold_spot"),
            "dxy": market_data.get("dxy"),
            "real_yield_dfii10": market_data.get("real_yield_dfii10"),
            "breakeven_t10yie": market_data.get("breakeven_t10yie"),
        },
        "claude_search_plan": search_plan,
        "candidate_events": candidates,
    }
    response = call_anthropic(
        CLAUDE_EVENT_RANKER_SYSTEM_PROMPT,
        json.dumps(user_payload, indent=2, default=str),
        mode="claude_event_impact_ranking",
    )
    parsed = _extract_json_object(response)
    if parsed is None:
        for event in events:
            event.setdefault("impact_scored_by", "static_prefilter")
            event.setdefault("claude_ranking_error", response)
        return events

    rankings = {
        _normalize_title(item.get("title", "")): item
        for item in parsed.get("events", [])
        if item.get("title")
    }

    ranked_events: list[dict[str, Any]] = []
    for event in events:
        ranking = rankings.get(_normalize_title(event.get("title", "")))
        if ranking:
            event["disruption_score"] = int(_safe_float(ranking.get("disruption_score")) or 0)
            event["direction_hint"] = ranking.get("direction_hint", event.get("direction_hint", "UNKNOWN"))
            event["claude_impact_reason"] = ranking.get("claude_impact_reason")
            event["historical_impact_basis"] = ranking.get("historical_impact_basis")
            event["impact_scored_by"] = "claude"
        else:
            event.setdefault("impact_scored_by", "static_prefilter")
        ranked_events.append(event)

    ranked_events.sort(key=lambda item: item.get("disruption_score", 0), reverse=True)
    return ranked_events[:25]


def _format_intraday_block(intraday: dict[str, Any]) -> str:
    block = "INTRADAY CHART DATA (5m/15m/1h/4h):\n\n"
    if intraday:
        block += f"Chart symbol: {intraday.get('symbol', 'n/a')}\n"
        block += f"Price basis: {intraday.get('price_basis', 'n/a')}\n"
        block += f"Requested primary symbol: {intraday.get('requested_symbol', intraday.get('symbol', 'n/a'))}\n"
        if intraday.get("chart_proxy"):
            block += (
                "Fallback note: XAUUSD spot intraday bars were unavailable, so "
                "proxy bars are included for chart structure only. "
                "Entries, stops, targets, and logging must still use XAUUSD spot.\n"
            )
        if intraday.get("cache_fallback"):
            block += f"Cache note: chart data is stale/cached from {intraday.get('cache_source', 'unknown cache')}.\n"
        if intraday.get("fallback_reason"):
            block += f"Fallback reason: {intraday.get('fallback_reason')}\n"
        block += "\n"

    tensor = intraday.get("confluence_tensor", {})
    timeframe_order = tensor.get("timeframe_order") or ["5m", "15m", "1h", "4h"]
    if tensor:
        block += "CONFLUENCE TENSOR:\n"
        block += f"  Features: {', '.join(tensor.get('feature_names', []))}\n"
        block += f"  Timeframes: {', '.join(timeframe_order)}\n"
        block += f"  Matrix: {json.dumps(tensor.get('matrix', []))}\n"
        block += f"  Timeframe scores: {json.dumps(tensor.get('timeframe_scores', {}))}\n"
        block += f"  Candle caps: {json.dumps(tensor.get('candle_limits', {}))}\n"
        block += (
            f"  Aggregate: {tensor.get('aggregate_score', 'n/a')} "
            f"({tensor.get('alignment_label', 'n/a')})\n"
        )
        block += f"  Scale: {tensor.get('scale_note', 'n/a')}\n\n"

    block += "CANDLE COUNTS FED TO THIS PROMPT:\n"
    for tf in timeframe_order:
        bar_data = intraday.get("intervals", {}).get(tf, {})
        if "error" in bar_data:
            block += f"  {tf}: 0 candles (error: {bar_data['error']})\n"
            continue
        candles = bar_data.get("candles") or []
        block += (
            f"  {tf}: {len(candles)} candles "
            f"(available {bar_data.get('candles_available', 'n/a')}, "
            f"cap {bar_data.get('candle_limit', 'n/a')})\n"
        )
    block += "\n"

    for tf in timeframe_order:
        bar_data = intraday.get("intervals", {}).get(tf, {})
        if "error" in bar_data:
            block += f"[{tf.upper()}] Error: {bar_data['error']}\n\n"
            continue

        block += f"[{tf.upper()}]\n"
        block += f"  Price: ${bar_data.get('current_price', 'n/a')}\n"
        block += (
            f"  Open/High/Low: ${bar_data.get('open', 'n/a')} / "
            f"${bar_data.get('high', 'n/a')} / ${bar_data.get('low', 'n/a')}\n"
        )
        block += f"  SMA(9):  ${bar_data.get('sma_9', 'n/a')}\n"
        block += f"  SMA(21): ${bar_data.get('sma_21', 'n/a')}\n"
        block += f"  SMA(50): ${bar_data.get('sma_50', 'n/a')}\n"
        block += f"  RSI(14): {bar_data.get('rsi', 'n/a')}\n"
        block += f"  Confluence score: {bar_data.get('confluence_score', 'n/a')} (-10 bearish to +10 bullish)\n"
        block += f"  Confluence vector: {json.dumps(bar_data.get('confluence_vector', {}))}\n"
        block += (
            f"  Candles fed to prompt: "
            f"{bar_data.get('candles_fed_to_prompt', len(bar_data.get('candles') or []))} "
            f"(available {bar_data.get('candles_available', 'n/a')}, "
            f"cap {bar_data.get('candle_limit', 'n/a')})\n\n"
        )
        candles = bar_data.get("candles") or []
        if candles:
            block += f"  OHLCV candle records fed to prompt ({len(candles)} total):\n"
            for candle in candles:
                block += (
                    "    "
                    f"{candle.get('ts')} "
                    f"O:{candle.get('open')} H:{candle.get('high')} "
                    f"L:{candle.get('low')} C:{candle.get('close')} "
                    f"V:{candle.get('volume')}\n"
                )
            block += "\n"
    return block


def _impact_score(text: str) -> tuple[int, str]:
    lowered = text.lower()
    score_terms = {
        "breaking": 2,
        "surprise": 2,
        "unexpected": 2,
        "urgent": 2,
        "spike": 2,
        "plunge": 2,
        "jumps": 2,
        "slumps": 2,
        "fomc": 2,
        "powell": 2,
        "federal reserve": 2,
        "fed": 1,
        "dxy": 2,
        "dollar": 1,
        "treasury yield": 2,
        "real yield": 2,
        "cpi": 2,
        "pce": 2,
        "nfp": 2,
        "payroll": 2,
        "jobs": 1,
        "war": 2,
        "attack": 2,
        "sanction": 2,
        "ceasefire": 2,
        "banking crisis": 3,
        "central bank": 1,
    }
    bullish_terms = [
        "war", "attack", "sanction", "banking crisis", "safe haven",
        "rate cut", "dovish", "weaker dollar", "dollar falls",
        "yields fall", "real yields fall", "recession",
    ]
    bearish_terms = [
        "hawkish", "rate hike", "higher for longer", "stronger dollar",
        "dollar rises", "yields rise", "real yields rise", "ceasefire",
        "risk-on", "hot jobs", "hot cpi",
    ]

    score = sum(weight for term, weight in score_terms.items() if term in lowered)
    bullish_hits = sum(1 for term in bullish_terms if term in lowered)
    bearish_hits = sum(1 for term in bearish_terms if term in lowered)

    if bullish_hits > bearish_hits:
        direction = "BULLISH_GOLD"
    elif bearish_hits > bullish_hits:
        direction = "BEARISH_GOLD"
    elif bullish_hits or bearish_hits:
        direction = "TWO_WAY"
    else:
        direction = "UNKNOWN"

    return min(score + max(bullish_hits, bearish_hits), 10), direction


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def build_disruption_queries(market_data: dict[str, Any]) -> list[str]:
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    gold_price = market_data.get("gold_spot", {}).get("price", "gold")
    dxy = market_data.get("dxy", {}).get("value", "DXY")

    return [
        f'XAUUSD spot gold breaking news {today}',
        f'XAUUSD spot gold sudden move {today}',
        f'XAUUSD Federal Reserve Powell FOMC surprise {today}',
        f'XAUUSD DXY dollar yields disruption {today}',
        f'XAUUSD CPI PCE NFP jobs data surprise {today}',
        f'Forex Factory high impact USD gold calendar {today}',
        f'XAUUSD geopolitical war sanctions ceasefire {today}',
        f'XAUUSD central bank reserves liquidity crisis {today}',
        f'XAUUSD price {gold_price} DXY {dxy} market shock',
    ]


def search_gnews_for_disruptions(
    queries: list[str],
    max_per_query: int = 5,
    prefilter_min_score: int = 0,
) -> list[dict[str, Any]]:
    if not GNEWS_API_KEY:
        return [{
            "title": "GNews API key missing",
            "description": "Set GNEWS_API_KEY or the project constant before using live internet scouting.",
            "source": "agent",
            "published_at": datetime.now().isoformat(),
            "url": "",
            "query": "",
            "disruption_score": 0,
            "direction_hint": "UNKNOWN",
        }]

    seen: set[str] = set()
    events: list[dict[str, Any]] = []

    for query in queries:
        try:
            response = requests.get(
                "https://gnews.io/api/v4/search",
                params={
                    "q": query,
                    "token": GNEWS_API_KEY,
                    "lang": "en",
                    "max": max_per_query,
                    "sortby": "publishedAt",
                },
                timeout=30,
            )
            response.raise_for_status()
            articles = response.json().get("articles", [])
        except Exception as exc:
            events.append({
                "title": f"News scout failed for query: {query}",
                "description": str(exc),
                "source": "agent",
                "published_at": datetime.now().isoformat(),
                "url": "",
                "query": query,
                "disruption_score": 0,
                "direction_hint": "UNKNOWN",
            })
            continue

        for article in articles:
            title = article.get("title") or ""
            key = _normalize_title(title)
            if not key or key in seen:
                continue
            seen.add(key)

            description = article.get("description") or ""
            score, direction = _impact_score(f"{title} {description}")
            if score < prefilter_min_score:
                continue

            source = article.get("source") or {}
            events.append({
                "title": title,
                "description": description,
                "source": source.get("name", "unknown"),
                "published_at": article.get("publishedAt"),
                "url": article.get("url"),
                "query": query,
                "disruption_score": score,
                "direction_hint": direction,
            })

    events.sort(key=lambda item: item.get("disruption_score", 0), reverse=True)
    return events[:25]


def _format_disruption_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "(No disruptive live-news candidates found by the scout.)"

    lines = []
    for event in events:
        lines.append(
            "- "
            f"[score {event.get('disruption_score', 0)}/10, {event.get('direction_hint', 'UNKNOWN')}] "
            f"{event.get('title', 'Untitled')} "
            f"({event.get('source', 'unknown')}, {event.get('published_at', 'time n/a')})\n"
            f"  Query: {event.get('query', 'n/a')}\n"
            f"  Summary: {event.get('description', '')}\n"
            f"  Impact scoring: {event.get('impact_scored_by', 'static_prefilter')}\n"
            f"  Claude impact reason: {event.get('claude_impact_reason', 'n/a')}\n"
            f"  URL: {event.get('url', '')}"
        )
    return "\n".join(lines)


def _validation_row(check: str, status: str, source: str, value: Any, detail: str) -> dict[str, Any]:
    return {
        "check": check,
        "status": status,
        "source": source,
        "value": value,
        "detail": detail,
    }


def _latest_candle_timestamp(bar_data: dict[str, Any]) -> datetime | None:
    candles = bar_data.get("candles") or []
    if not candles:
        return None
    return _parse_timestamp(candles[-1].get("ts"))


def _candle_age_minutes(latest_ts: datetime | None) -> float | None:
    if latest_ts is None:
        return None
    now = datetime.now(latest_ts.tzinfo or ZoneInfo("America/New_York"))
    return max((now - latest_ts).total_seconds() / 60, 0)


def build_data_validation_report(state: GoldAgentState) -> dict[str, Any]:
    market_data = state.get("market_data", {})
    intraday = state.get("intraday", {})
    events = state.get("disruptive_events", [])
    loopback = state.get("loopback_evaluation", {})
    rows: list[dict[str, Any]] = []

    gold_spot = market_data.get("gold_spot", market_data.get("gold", {}))
    spot_price = _safe_float(gold_spot.get("price"))
    rows.append(_validation_row(
        "XAUUSD spot decision price",
        "OK" if spot_price and spot_price > 0 else "FAIL",
        gold_spot.get("source", "gold-api XAU"),
        spot_price,
        "Primary execution/logging price must be live spot gold versus USD.",
    ))

    bid = _safe_float(gold_spot.get("bid"))
    ask = _safe_float(gold_spot.get("ask"))
    if bid and ask and ask >= bid:
        spread = round(ask - bid, 3)
        spread_pct = round(spread / spot_price * 100, 4) if spot_price else None
        rows.append(_validation_row(
            "XAUUSD bid/ask spread",
            "OK",
            gold_spot.get("source", "gold-api XAU"),
            {"bid": bid, "ask": ask, "spread": spread, "spread_pct": spread_pct},
            "Bid/ask is usable for spread-aware prop-firm sizing.",
        ))
    else:
        rows.append(_validation_row(
            "XAUUSD bid/ask spread",
            "WARN",
            gold_spot.get("source", "gold-api XAU"),
            {"bid": gold_spot.get("bid"), "ask": gold_spot.get("ask")},
            "Bid/ask missing or invalid; include extra slippage buffer and do not over-size.",
        ))

    dxy = market_data.get("dxy", {})
    dxy_value = _safe_float(dxy.get("value"))
    dxy_source = dxy.get("source", "DX-Y.NYB direct / synthetic FX")
    rows.append(_validation_row(
        "DXY",
        "OK" if dxy_value and dxy_value > 0 else "FAIL",
        dxy_source,
        dxy,
        "DXY is a primary gold transmission driver; failed/zero values must lower macro confidence.",
    ))

    for name, value, source in (
        ("10Y real yield DFII10", market_data.get("real_yield_dfii10"), "FRED DFII10"),
        ("10Y breakeven T10YIE", market_data.get("breakeven_t10yie"), "FRED T10YIE"),
    ):
        parsed = _safe_float(value)
        rows.append(_validation_row(
            name,
            "OK" if parsed is not None else "WARN",
            source,
            value,
            "Daily macro series; valid but may be stale intraday." if parsed is not None else "Missing macro input.",
        ))

    treasury = market_data.get("treasury", {})
    for label, series in (
        ("10Y Treasury yield", _treasury_series(treasury, "dgs10", "y10")),
        ("2Y Treasury yield", _treasury_series(treasury, "dgs2", "y2")),
    ):
        parsed = _safe_float(series.get("value"))
        rows.append(_validation_row(
            label,
            "OK" if parsed is not None else "WARN",
            "FRED Treasury snapshot",
            series,
            "Treasury yield available." if parsed is not None else "Treasury yield missing.",
        ))

    fed_balance = market_data.get("fed_balance_sheet", {})
    fed_value = _first_float(
        fed_balance.get("latest_trillions"),
        fed_balance.get("value_trillions"),
        fed_balance.get("four_week_change_billions"),
        fed_balance.get("change_4w_billions"),
    )
    rows.append(_validation_row(
        "Fed balance sheet/liquidity",
        "OK" if fed_value is not None else "WARN",
        "FRED WALCL",
        fed_balance,
        "Liquidity context available." if fed_value is not None else "Fed balance sheet context missing; lower liquidity confidence.",
    ))

    expected_timeframes = ["5m", "15m", "1h", "4h"]
    timeframe_max_age_minutes = {"5m": 30, "15m": 60, "1h": 180, "4h": 720}
    intraday_symbol = intraday.get("symbol")
    intraday_basis = intraday.get("price_basis", "n/a")
    for timeframe in expected_timeframes:
        bar_data = intraday.get("intervals", {}).get(timeframe, {})
        source = f"{intraday_symbol or 'n/a'} / {intraday_basis}"
        if not bar_data or bar_data.get("error"):
            rows.append(_validation_row(
                f"{timeframe} XAUUSD chart data",
                "FAIL",
                source,
                bar_data.get("error") if isinstance(bar_data, dict) else bar_data,
                "No usable OHLCV/technical data for this timeframe.",
            ))
            continue

        latest_ts = _latest_candle_timestamp(bar_data)
        age_minutes = _candle_age_minutes(latest_ts)
        has_core_technicals = (
            _valid_positive_float(bar_data.get("current_price"))
            and _safe_float(bar_data.get("rsi")) is not None
            and _valid_positive_float(bar_data.get("sma_9"))
            and _valid_positive_float(bar_data.get("sma_21"))
        )
        stale = (
            age_minutes is not None
            and age_minutes > timeframe_max_age_minutes.get(timeframe, 180)
        )
        status = "OK" if has_core_technicals and not stale and not intraday.get("cache_fallback") else "WARN"
        rows.append(_validation_row(
            f"{timeframe} XAUUSD chart data",
            status,
            source,
            {
                "current_price": bar_data.get("current_price"),
                "rsi": bar_data.get("rsi"),
                "sma_9": bar_data.get("sma_9"),
                "sma_21": bar_data.get("sma_21"),
                "sma_50": bar_data.get("sma_50"),
                "candles": len(bar_data.get("candles") or []),
                "latest_candle": latest_ts.isoformat() if latest_ts else None,
                "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
            },
            (
                "Usable current chart data."
                if status == "OK"
                else "Chart data is cached, stale, or missing a core technical; reduce confidence."
            ),
        ))

    tensor = intraday.get("confluence_tensor", {})
    tensor_score = _safe_float(tensor.get("aggregate_score"))
    rows.append(_validation_row(
        "XAUUSD confluence tensor",
        "OK" if tensor_score is not None and not intraday.get("cache_fallback") else "WARN",
        intraday_basis,
        tensor,
        "Confluence tensor available." if tensor_score is not None else "Confluence tensor missing or invalid.",
    ))

    event_count = len(events)
    failed_events = sum(1 for event in events if "failed" in str(event.get("title", "")).lower())
    rows.append(_validation_row(
        "Live news/catalyst events",
        "OK" if event_count and failed_events < event_count else "WARN",
        "GNews + public feeds + Forex Factory calendar",
        {"events": event_count, "failed_events": failed_events},
        "At least one live/candidate event source returned." if event_count else "No live/candidate events returned.",
    ))

    snapshot = market_data.get("prop_account_snapshot", {})
    manual_log = market_data.get("manual_trade_backlog_context", "")
    decided_trades = snapshot.get("decided_trades", 0)
    rows.append(_validation_row(
        "Trade-log learning inputs",
        "OK" if decided_trades > 0 and manual_log and manual_log != "(No history yet.)" else "WARN",
        "trade_backlog.txt",
        {
            "total_trades": snapshot.get("total_trades", 0),
            "decided_trades": decided_trades,
            "current_equity_usd": snapshot.get("current_equity_usd"),
            "total_closed_pnl_usd": snapshot.get("total_closed_pnl_usd"),
            "win_rate_pct": snapshot.get("win_rate_pct"),
        },
        "trade_backlog.txt available with resolved outcomes."
        if decided_trades > 0
        else "trade_backlog.txt missing or has no WIN/LOSS rows.",
    ))

    rows.append(_validation_row(
        "Loopback prior signal comparison",
        "OK" if loopback.get("status") == "evaluated_prior_signal" and loopback.get("previous_price_verified_spot") else "WARN",
        "gold_agent_state.json",
        {
            "status": loopback.get("status"),
            "previous_spot_gold_price": loopback.get("previous_spot_gold_price"),
            "current_spot_gold_price": loopback.get("current_spot_gold_price"),
            "signal_outcome": loopback.get("signal_outcome"),
            "previous_price_verified_spot": loopback.get("previous_price_verified_spot"),
        },
        "Prior signal can be compared against verified XAUUSD spot." if loopback.get("previous_price_verified_spot") else "Prior price basis missing or not verified as XAUUSD spot.",
    ))

    fail_count = sum(1 for row in rows if row["status"] == "FAIL")
    warn_count = sum(1 for row in rows if row["status"] == "WARN")
    ok_count = sum(1 for row in rows if row["status"] == "OK")
    failed_checks = {row["check"] for row in rows if row["status"] == "FAIL"}
    blocking_failures = [
        check for check in ("XAUUSD spot decision price", "DXY")
        if check in failed_checks
    ]
    for check in ("4h XAUUSD chart data", "1h XAUUSD chart data"):
        if check in failed_checks:
            blocking_failures.append(check)
    if {"15m XAUUSD chart data", "5m XAUUSD chart data"}.issubset(failed_checks):
        blocking_failures.append("15m/5m execution chart data")

    return {
        "summary": {
            "ok": ok_count,
            "warn": warn_count,
            "fail": fail_count,
            "blocking_failures": blocking_failures,
            "trade_permission": "NO-TRADE" if blocking_failures else "TRADE_ALLOWED_IF_SETUP_CONFIRMS",
        },
        "checks": rows,
    }


def _format_data_validation_report(report: dict[str, Any]) -> str:
    if not report:
        return "(No validation report generated.)"
    lines = [
        f"SUMMARY: {json.dumps(report.get('summary', {}), default=str)}",
        "",
        "CHECKS:",
    ]
    for row in report.get("checks", []):
        lines.append(
            f"- [{row.get('status', 'UNKNOWN')}] {row.get('check')}\n"
            f"  Source: {row.get('source')}\n"
            f"  Value: {json.dumps(row.get('value'), default=str)}\n"
            f"  Detail: {row.get('detail')}"
        )
    return "\n".join(lines)


def _intraday_has_usable_bars(intraday: dict[str, Any]) -> bool:
    return any(
        not data.get("error") and data.get("current_price") is not None
        for data in intraday.get("intervals", {}).values()
        if isinstance(data, dict)
    )


def _intraday_error_summary(intraday: dict[str, Any]) -> str:
    errors = [
        f"{timeframe}: {data.get('error')}"
        for timeframe, data in intraday.get("intervals", {}).items()
        if isinstance(data, dict) and data.get("error")
    ]
    return "; ".join(errors) if errors else "No usable intraday bars returned"


def _load_cached_intraday_snapshot() -> dict[str, Any]:
    cache_paths = [DAY_TRADING_INTRADAY_SNAPSHOT_PATH, Path(__file__).resolve().parent / "intraday_data.json"]
    for path in cache_paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        intraday = payload.get("intraday") if isinstance(payload, dict) else None
        if not isinstance(intraday, dict):
            intraday = payload if isinstance(payload, dict) else {}
        if _intraday_has_usable_bars(intraday):
            intraday = dict(intraday)
            intraday["cache_source"] = str(path)
            return intraday
    return {}


def fetch_xauusd_intraday_with_cache_fallback() -> dict[str, Any]:
    """
    Fetch XAUUSD intraday bars via GC=F chart proxy (Yahoo direct API, SSL bypass).
    GC=F tracks spot XAUUSD with >0.99 intraday correlation; spot price from
    gold-api remains the execution/logging price.
    """
    intervals = ["5m", "15m", "1h", "4h"]
    intraday = fetch_intraday_bars(symbol=SPOT_GOLD_SYMBOL, intervals=intervals, retries=2)
    intraday["requested_symbol"] = SPOT_GOLD_SYMBOL
    intraday["price_basis"] = "GC=F chart proxy for XAUUSD technicals; gold-api spot for execution"
    intraday["chart_proxy"] = "GC=F"

    if _intraday_has_usable_bars(intraday):
        return intraday

    # Fallback to cached snapshot if live fetch failed
    cached_intraday = _load_cached_intraday_snapshot()
    if cached_intraday and _intraday_has_usable_bars(cached_intraday):
        cached_intraday["requested_symbol"] = SPOT_GOLD_SYMBOL
        cached_intraday["price_basis"] = "cached chart context; use live spot quote for execution"
        cached_intraday["cache_fallback"] = True
        cached_intraday["fallback_reason"] = f"Live fetch failed: {_intraday_error_summary(intraday)}"
        return cached_intraday

    return intraday


def collect_market_context(state: GoldAgentState) -> GoldAgentState:
    print("[agent] Collecting market data, macro data, and intraday bars...")
    errors = state.get("errors", [])

    metals, err = _safe_fetch("gold/silver price", fetch_gold_price, {"gold": {}, "silver": {}})
    if err:
        errors.append(err)

    real_yield, err = _safe_fetch("10Y real yield", lambda: fetch_macro("DFII10"), "n/a")
    if err:
        errors.append(err)

    breakeven, err = _safe_fetch("10Y breakeven", lambda: fetch_macro("T10YIE"), "n/a")
    if err:
        errors.append(err)

    dxy_data, err = _safe_fetch("DXY", fetch_dxy, {})
    if err:
        errors.append(err)

    treasury, err = _safe_fetch("Treasury snapshot", fetch_treasury_snapshot, {})
    if err:
        errors.append(err)

    fed_balance, err = _safe_fetch("Fed balance sheet", fetch_fed_balance_sheet_snapshot, {})
    if err:
        errors.append(err)

    intraday, err = _safe_fetch(
        "intraday bars",
        fetch_xauusd_intraday_with_cache_fallback,
        {"intervals": {}},
    )
    if err:
        errors.append(err)

    gold_raw = metals.get("gold", {})
    gold_source = gold_raw.get("source", "gold-api XAU")
    gold_spot = {
        **gold_raw,
        "symbol": SPOT_GOLD_SYMBOL,
        "instrument": "Spot gold versus US dollar",
        "source": gold_source,
        "verification": (
            "Decision/logging price is XAUUSD spot via gold-api."
            if not gold_raw.get("fallback")
            else (
                "Decision/logging price uses GC=F chart proxy last close because "
                "gold-api was unreachable; intraday charts also use GC=F proxy."
            )
        ),
    }
    gold_price = _safe_float(gold_spot.get("price"))
    silver_price = _safe_float(metals.get("silver", {}).get("price"))
    gsr = round(gold_price / silver_price, 2) if gold_price and silver_price else None

    _ensure_manual_trade_backlog_file()
    prop_account_state = refresh_prop_account_state(
        MANUAL_TRADE_BACKLOG_TXT_PATH,
        today=date.today().isoformat(),
    )
    snapshot = prop_account_state.get("snapshot", {})
    print(
        "[agent] Prop account snapshot: "
        f"equity=${snapshot.get('current_equity_usd', PROP_ACCOUNT_SIZE):,.2f} "
        f"closed_pnl=${snapshot.get('total_closed_pnl_usd', 0):,.2f} "
        f"leverage_mode={snapshot.get('leverage_mode', 'n/a')} "
        f"tv={snapshot.get('recommended_tv_leverage', 'n/a')}:1"
    )

    market_data = {
        "timestamp_et": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "gold_spot": gold_spot,
        "gold": gold_spot,
        "silver": metals.get("silver", {}),
        "gold_silver_ratio": gsr,
        "real_yield_dfii10": real_yield,
        "breakeven_t10yie": breakeven,
        "dxy": dxy_data,
        "treasury": treasury,
        "fed_balance_sheet": fed_balance,
        "model_trade_log_context": _read_recent_text(MODEL_TRADE_LOG_TXT_PATH, max_chars=9000),
        "manual_trade_backlog_context": prop_account_state.get("table_text", ""),
        "prop_account_snapshot": prop_account_state.get("snapshot", {}),
        "prop_account_snapshot_text": prop_account_state.get("snapshot_text", ""),
        "prop_account_rows": prop_account_state.get("rows", []),
    }
    missing_quant_metrics = identify_missing_quant_metrics(market_data, intraday)
    metric_recovery_plan = build_metric_recovery_plan(missing_quant_metrics, market_data)
    market_data["missing_quant_metrics"] = missing_quant_metrics
    market_data["metric_recovery_plan"] = metric_recovery_plan

    previous_snapshot = _load_previous_agent_snapshot()
    loopback_evaluation = build_loopback_evaluation(
        previous_snapshot,
        market_data,
        intraday,
        gold_price,
    )

    return {
        "market_data": market_data,
        "intraday": intraday,
        "current_gold_price": gold_price,
        "metric_recovery_plan": metric_recovery_plan,
        "loopback_evaluation": loopback_evaluation,
        "errors": errors,
    }


def scout_internet(state: GoldAgentState) -> GoldAgentState:
    print("[agent] Asking Claude where to search, then scouting disruptive gold catalysts...")
    market_data = state.get("market_data", {})
    search_plan = build_claude_search_plan(
        market_data,
        state.get("intraday", {}),
        state.get("loopback_evaluation", {}),
    )
    queries = search_plan.get("queries") or build_disruption_queries(market_data)
    events = search_gnews_for_disruptions(queries)
    errors = state.get("errors", [])

    if HAS_ADDITIONAL_NEWS:
        try:
            extra_articles = fetch_additional_metals_news()
            for article in extra_articles[:15]:
                if not isinstance(article, dict):
                    continue
                title = article.get("title") or ""
                description = article.get("description") or ""
                score, direction = _impact_score(
                    f"{title} {description} {article.get('category', '')}"
                )
                events.append({
                    "title": title,
                    "description": description,
                    "source": article.get("source", "additional_news"),
                    "published_at": article.get("published_at") or datetime.now().isoformat(),
                    "url": article.get("url", ""),
                    "query": f"public RSS feed: {article.get('category', 'macro/news')}",
                    "disruption_score": score,
                    "direction_hint": direction,
                    "impact_scored_by": "static_prefilter",
                })
            extra_text = format_additional_news_for_prompt(extra_articles, max_articles=10)
            if extra_text:
                events.append({
                    "title": "Additional public macro/news feed digest",
                    "description": extra_text[:1500],
                    "source": "additional_news_digest",
                    "published_at": datetime.now().isoformat(),
                    "url": "",
                    "query": "public feeds: gold, Fed, economics, politics, markets, Forex Factory calendar",
                    "disruption_score": 1,
                    "direction_hint": "UNKNOWN",
                    "impact_scored_by": "digest_context",
                })
        except Exception as exc:
            errors = [*errors, f"additional news scout: {exc}"]

    events = claude_rank_disruption_events(events, search_plan, market_data)
    watchlist_event = _claude_watchlist_event(search_plan)
    if watchlist_event:
        events.append(watchlist_event)
        events.sort(key=lambda item: item.get("disruption_score", 0), reverse=True)

    return {
        "scout_queries": queries,
        "claude_search_plan": search_plan,
        "disruptive_events": events,
        "errors": errors,
    }


def build_agent_user_message(state: GoldAgentState) -> str:
    market_data = state.get("market_data", {})
    intraday = state.get("intraday", {})
    fed_balance = market_data.get("fed_balance_sheet", {})
    treasury = market_data.get("treasury", {})
    treasury_10y = _treasury_series(treasury, "dgs10", "y10")
    treasury_2y = _treasury_series(treasury, "dgs2", "y2")
    treasury_tga = _treasury_tga(treasury)
    gold_spot = market_data.get("gold_spot", market_data.get("gold", {}))
    metric_recovery_plan = state.get("metric_recovery_plan") or market_data.get("metric_recovery_plan", {})
    loopback_evaluation = state.get("loopback_evaluation", {})
    claude_search_plan = state.get("claude_search_plan", {})
    data_validation_report = build_data_validation_report(state)

    return f"""
===================================================================
LIVE LANGGRAPH GOLD DISRUPTION AGENT - CYCLE {state.get('cycle', 1)}
===================================================================

CURRENT GOLD PRICES:
- Spot gold (XAUUSD) decision price: ${gold_spot.get('price', 'n/a')}
- XAUUSD source: {gold_spot.get('source', 'n/a')}
- XAUUSD bid/ask: ${gold_spot.get('bid', 'n/a')} / ${gold_spot.get('ask', 'n/a')}
- XAUUSD open/high/low: ${gold_spot.get('open', 'n/a')} / ${gold_spot.get('high', 'n/a')} / ${gold_spot.get('low', 'n/a')}
- XAUUSD change: {gold_spot.get('change', 'n/a')} ({gold_spot.get('change_pct', 'n/a')}%)
- Silver spot: ${market_data.get('silver', {}).get('price', 'n/a')}
- Gold/Silver Ratio: {market_data.get('gold_silver_ratio', 'n/a')}

PRICE SOURCE VERIFICATION:
{gold_spot.get('verification', 'XAUUSD spot quote unavailable. Do not substitute futures data.')}

FUNDEDNEXT STELLAR 1-STEP CFD — $25,000 ACCOUNT:
- Account size: ${PROP_ACCOUNT_SIZE:,}
- Daily max loss: {PROP_DAILY_LOSS_SOFT_LIMIT_PCT:.1%} soft (${PROP_ACCOUNT_SIZE * PROP_DAILY_LOSS_SOFT_LIMIT_PCT:,.0f}) / {PROP_DAILY_LOSS_HARD_LIMIT_PCT:.0%} hard (${PROP_ACCOUNT_SIZE * PROP_DAILY_LOSS_HARD_LIMIT_PCT:,.0f}) — includes floating P&L
- Total max loss: {PROP_TOTAL_DRAWDOWN_SOFT_LIMIT_PCT:.0%} soft (${PROP_ACCOUNT_SIZE * PROP_TOTAL_DRAWDOWN_SOFT_LIMIT_PCT:,.0f}) / {PROP_TOTAL_DRAWDOWN_HARD_LIMIT_PCT:.0%} hard (${PROP_ACCOUNT_SIZE * PROP_TOTAL_DRAWDOWN_HARD_LIMIT_PCT:,.0f}); floor ${PROP_ACCOUNT_SIZE * (1 - PROP_TOTAL_DRAWDOWN_HARD_LIMIT_PCT):,.0f}
- Profit target: {PROP_PROFIT_TARGET_PCT:.0%} (${PROP_ACCOUNT_SIZE * PROP_PROFIT_TARGET_PCT:,.0f})
- Min aligned Target 1 profit: ${PROP_MIN_TARGET1_PROFIT_USD:,} (verify xau_ounces × |T1−entry| ≥ this)
- Daily budget sizing: one trade at a time — use snapshot recommended_max_risk_usd (~{PROP_DAILY_ROOM_USE_PCT:.0%} of remaining daily loss room)
- Min trading days to pass: {PROP_MIN_TRADING_DAYS}
- TradingView leverage presets: {", ".join(f"{x}:1" for x in TRADINGVIEW_LEVERAGE_OPTIONS)} — aligned default {PROP_PREFERRED_TV_LEVERAGE}:1 when snapshot allows
- PROP ACCOUNT SNAPSHOT is READ from trade_backlog.txt last row only — agent never writes that file
- Your real EQUITY and CUMULATIVE_PNL = exact values in the LAST ROW — never re-sum or override

===================================================================
PROP ACCOUNT SNAPSHOT (trade_backlog.txt ONLY — last row authoritative)
===================================================================

{market_data.get('prop_account_snapshot_text', '(No prop account history yet.)')}

Snapshot JSON:
{json.dumps(market_data.get('prop_account_snapshot', {}), indent=2, default=str)}

MACRO INDICATORS:
- 10Y Real yield (TIPS, DFII10): {market_data.get('real_yield_dfii10', 'n/a')}%
- 10Y Breakeven inflation (T10YIE): {market_data.get('breakeven_t10yie', 'n/a')}%
- DXY: {market_data.get('dxy', {}).get('value', 'n/a')} (open {market_data.get('dxy', {}).get('open', 'n/a')}, high {market_data.get('dxy', {}).get('high', 'n/a')}, low {market_data.get('dxy', {}).get('low', 'n/a')})

TREASURY DATA:
- 10Y Treasury yield: {treasury_10y.get('value', 'n/a')}% ({treasury_10y.get('change_1d_bps', 'n/a')} bps 1d, {treasury_10y.get('change_5d_bps', 'n/a')} bps 5d)
- 2Y Treasury yield: {treasury_2y.get('value', 'n/a')}% ({treasury_2y.get('change_1d_bps', 'n/a')} bps 1d, {treasury_2y.get('change_5d_bps', 'n/a')} bps 5d)
- Treasury General Account: ${treasury_tga.get('value_billions', 'n/a')}B ({treasury_tga.get('weekly_change_billions', 'n/a')}B weekly change)

FED BALANCE SHEET:
- Total assets: ${fed_balance.get('latest_trillions', fed_balance.get('value_trillions', 'n/a'))}T
- 4-week change: ${fed_balance.get('four_week_change_billions', fed_balance.get('change_4w_billions', 'n/a'))}B

===================================================================
DATA VALIDATION REPORT
===================================================================

Validate every retrieved input before assigning a signal. Any FAIL or WARN below
must be reflected in macro clarity, chart confidence, execution chart selection,
and prop-firm risk sizing. Blocking failures mean NO-TRADE unless the report
explicitly says trading is allowed.

{_format_data_validation_report(data_validation_report)}

===================================================================
QUANT METRIC RECOVERY PLAN
===================================================================

Quantifiable metrics must come from structured/static sources. If any are
missing, Claude generated this recovery/search plan. Do not invent replacement
values from memory.

{json.dumps(metric_recovery_plan, indent=2, default=str)}

===================================================================
LIVE INTERNET DISRUPTION SCOUT
===================================================================

Claude generated the search intent for this cycle from market knowledge,
historical high-impact gold drivers, Forex Factory-style event risk, and prior
loopback feedback. Treat the items below as candidates, not truth. Verify impact
through XAUUSD spot price action, DXY, yields, and freshness.

CLAUDE SEARCH PLAN:
{json.dumps(claude_search_plan, indent=2, default=str)}

SEARCH QUERIES:
{json.dumps(state.get('scout_queries', []), indent=2)}

DISRUPTIVE CATALYST CANDIDATES:
{_format_disruption_events(state.get('disruptive_events', []))}

===================================================================
TIER 2-5 - INTRADAY CHARTS
===================================================================

The agent uses XAUUSD OHLCV candle arrays for 5m/15m/1h/4h each cycle and
constructs a normalized confluence tensor from those candles. The prompt shows
the exact candle count fed from each chart plus the included candle records.
The same arrays are persisted to:
- Latest snapshot: {DAY_TRADING_INTRADAY_SNAPSHOT_PATH}
- Cycle archive: {DAY_TRADING_INTRADAY_ARCHIVE_PATH}

{_format_intraday_block(intraday)}

===================================================================
LOOPBACK SELF-EVALUATION
===================================================================

At every loopback, recall the prior signal and evaluate it against the XAUUSD
spot price collected then versus the XAUUSD spot price collected now. Use the
ranking to adjust which data types matter for the current score.

LOOPBACK IS NOT A PRICE TARGET LIST: Prior spot prices and prior NO-TRADE notes
(e.g. "need $4540") are for thesis/signal evaluation only. This cycle's entries
and re-entry conditions must use fresh levels from the intraday block below.

{json.dumps(loopback_evaluation, indent=2, default=str)}

===================================================================
TRADE BACKLOG (trade_backlog.txt — SOLE SOURCE for equity, PnL, win rate)
===================================================================

{market_data.get('manual_trade_backlog_context', '(No history yet.)')}

MODEL SIGNAL HISTORY (reference only — NOT for equity or PnL):
{market_data.get('model_trade_log_context', '(No history yet.)')}

===================================================================
ANALYSIS REQUEST
===================================================================

Apply the TOP-DOWN scoring system and produce a fresh trade signal. Start with
DISRUPTION WATCH, then complete the normal tiered trading signal.

Required:
1. Start with DATA VALIDATION: cite each FAIL/WARN/OK category that affects the
   signal and state whether trade_permission allows trading.
2. Identify whether any internet-scouted item can move gold acutely up or down.
3. Explain the transmission mechanism through DXY, yields, risk sentiment,
   inflation expectations, Fed path, safe-haven demand, or liquidity.
4. Reconcile live catalysts against 4H, 1H, 15m, and 5m price action.
5. Explicitly choose the execution chart: 15M, 5M, or NONE.
6. Update the trade signal only if the catalyst and price action justify it.
7. Score CONFLUENCE ALIGNMENT (FULL / PARTIAL / MISALIGN) and pick aggression accordingly.
8. Cite last-row EQUITY and CUMULATIVE_PNL verbatim from trade_backlog.txt. Output BOTH
   TradingView (100:1 + risk USD) and MetaTrader 5 (100:1 + risk USD + lots) blocks.
   On FULL ALIGNMENT: use snapshot recommended_max_risk_usd (daily room model), 100:1, 2R T1 (≥${PROP_MIN_TARGET1_PROFIT_USD:,}), 2.5-3R T2.
9. Explicitly state which ranked data types from the loopback should gain or
   lose weight in this cycle's scoring.
10. Confirm the trade stays below snapshot daily-loss and max-drawdown rooms; cite
    snapshot numbers in the FundedNext / TradingView Risk Plan block.
11. Finish with the exact mandatory last line: GOLD_SIGNAL=[LONG/SHORT/NEUTRAL]
"""


def call_anthropic(system_prompt: str, user_message: str, mode: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "ERROR: Missing ANTHROPIC_API_KEY in environment."

    system_tokens = estimate_prompt_tokens(system_prompt)
    user_tokens = estimate_prompt_tokens(user_message)
    total_input_estimated = system_tokens + user_tokens

    print("\n[ANTHROPIC API CALL]")
    print(f"  Estimated input tokens: ~{total_input_estimated}")
    print(f"    System prompt: ~{system_tokens}")
    print(f"    User message:  ~{user_tokens}")

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": "claude-opus-4-8",
                "max_tokens": 4096,
                "system": [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [
                    {"role": "user", "content": user_message}
                ],
            },
            timeout=120,
        )
        response_json = response.json()
    except Exception as exc:
        return f"ERROR: Anthropic request failed: {exc}"

    if "error" in response_json:
        error_msg = response_json["error"].get("message", "Unknown error")
        print(f"\nERROR: {error_msg}")
        return f"ERROR: {error_msg}"

    if "usage" in response_json:
        usage = response_json["usage"]
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        total_tokens = input_tokens + output_tokens + cache_creation + cache_read
        cost = calculate_cost(input_tokens + cache_creation, output_tokens)

        print("\n  Actual usage:")
        print(f"    Input tokens:        {input_tokens}")
        print(f"    Output tokens:       {output_tokens}")
        print(f"    Cache creation:      {cache_creation}")
        print(f"    Cache read:          {cache_read}")
        print(f"    Total tokens:        {total_tokens}")
        print(f"    Cost this run:       ${cost:.6f}")

        log_token_usage(
            event_type="actual",
            input_tokens=input_tokens + cache_creation,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            details={
                "model": "claude-sonnet-4-20250514",
                "endpoint": "day_trading_insights",
                "mode": mode,
                "cache_creation": cache_creation,
                "cache_read": cache_read,
            },
        )

    if response_json.get("content"):
        return response_json["content"][0].get("text", "ERROR: Empty response text")
    return "ERROR: No content in response"


def generate_trade_signal(state: GoldAgentState) -> GoldAgentState:
    print("[agent] Generating updated gold trade signal...")
    user_message = build_agent_user_message(state)
    DAY_TRADING_LIVE_DATA_PATH.write_text(user_message, encoding="utf-8")

    insights_text = call_anthropic(
        DISRUPTION_SCOUT_SYSTEM_PROMPT,
        user_message,
        mode="langgraph_disruption_agent",
    )

    cycle = state.get("cycle", 1)
    if cycle <= 1:
        DAY_TRADING_INSIGHTS_PATH.write_text(insights_text, encoding="utf-8")
    else:
        with DAY_TRADING_INSIGHTS_PATH.open("a", encoding="utf-8") as fh:
            fh.write("\n\n")
            fh.write("=" * 80)
            fh.write(f"\nCYCLE {cycle} APPENDED AT {datetime.now(ZoneInfo('America/New_York')).isoformat()}\n")
            fh.write("=" * 80)
            fh.write("\n\n")
            fh.write(insights_text)
    print(f"\n  Insights saved to: {DAY_TRADING_INSIGHTS_PATH.name}")

    return {
        "user_message": user_message,
        "insights_text": insights_text,
    }


def parse_agent_gold_signal(insights_text: str) -> dict[str, Any]:
    signal_data = parse_gold_signal(insights_text)
    if not insights_text:
        return signal_data

    if signal_data.get("signal") is None:
        signal_match = re.search(
            r"SIGNAL\s*:\s*(LONG|SHORT|NO-TRADE|NEUTRAL)",
            insights_text,
            flags=re.IGNORECASE,
        )
        if signal_match:
            raw_signal = signal_match.group(1).upper()
            signal_data["signal"] = "NEUTRAL" if raw_signal == "NO-TRADE" else raw_signal

    if signal_data.get("confidence") is None:
        confidence_match = re.search(
            r"Confidence\s*:\s*(LOW|MEDIUM|HIGH)",
            insights_text,
            flags=re.IGNORECASE,
        )
        if confidence_match:
            signal_data["confidence"] = confidence_match.group(1).upper()

    if signal_data.get("entry_low") is None:
        entry_match = re.search(r"Entry\s*:\s*\$?([\d,]+(?:\.\d+)?)", insights_text, re.IGNORECASE)
        if entry_match:
            entry = _safe_float(entry_match.group(1))
            signal_data["entry_low"] = entry
            signal_data["entry_high"] = entry

    if signal_data.get("stop_loss") is None:
        stop_match = re.search(r"Stop(?:\s+Loss)?\s*:\s*\$?([\d,]+(?:\.\d+)?)", insights_text, re.IGNORECASE)
        if stop_match:
            signal_data["stop_loss"] = _safe_float(stop_match.group(1))

    if signal_data.get("target1") is None:
        target1_match = re.search(r"Target\s*1\s*:\s*\$?([\d,]+(?:\.\d+)?)", insights_text, re.IGNORECASE)
        if target1_match:
            signal_data["target1"] = _safe_float(target1_match.group(1))

    if signal_data.get("target2") is None:
        target2_match = re.search(r"Target\s*2\s*:\s*\$?([\d,]+(?:\.\d+)?)", insights_text, re.IGNORECASE)
        if target2_match:
            signal_data["target2"] = _safe_float(target2_match.group(1))

    if signal_data.get("recommended_leverage") is None:
        leverage_match = re.search(
            r"(?:Chosen|Recommended|Implied|Effective)\s*(?:leverage)?\s*(?:\(calculated\))?\s*:\s*([^\n]+)",
            insights_text,
            flags=re.IGNORECASE,
        )
        if leverage_match:
            signal_data["recommended_leverage"] = leverage_match.group(1).strip()

    tv_leverage_match = re.search(
        r"TradingView\s+leverage\s+preset\s*:\s*([^\n]+)",
        insights_text,
        flags=re.IGNORECASE,
    )
    if tv_leverage_match:
        signal_data["tradingview_leverage"] = tv_leverage_match.group(1).strip()

    # If we have numeric effective leverage but no TV mapping, compute it
    eff_match = re.search(
        r"Effective\s+leverage\s*\(calculated\)\s*:\s*([\d.]+)\s*x?",
        insights_text,
        flags=re.IGNORECASE,
    )
    if eff_match and not signal_data.get("tradingview_leverage"):
        tv_map = map_effective_leverage_to_tradingview(
            _safe_float(eff_match.group(1)),
            prefer_leverage=PROP_PREFERRED_TV_LEVERAGE,
        )
        signal_data["tradingview_leverage"] = tv_map.get("tradingview_label")
        signal_data["tradingview_leverage_note"] = tv_map.get("note")

    mt5_lev_match = re.search(
        r"MetaTrader\s*5.*?\n.*?Account\s+leverage\s*:\s*([^\n]+)",
        insights_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if mt5_lev_match:
        signal_data["mt5_leverage"] = mt5_lev_match.group(1).strip()
    else:
        mt5_simple = re.search(r"MT5.*?leverage\s*:\s*([^\n]+)", insights_text, re.IGNORECASE)
        if mt5_simple:
            signal_data["mt5_leverage"] = mt5_simple.group(1).strip()

    if signal_data.get("risk_mode") is None:
        risk_mode_match = re.search(
            r"Risk(?:\s+cap)?\s+mode\s*:\s*(AGGRESSIVE|BALANCED|CONSERVATIVE|NO-TRADE|NO-LEVERAGE)",
            insights_text,
            flags=re.IGNORECASE,
        )
        if risk_mode_match:
            signal_data["risk_mode"] = risk_mode_match.group(1).upper()

    return signal_data


def persist_results(state: GoldAgentState) -> GoldAgentState:
    print("[agent] Persisting signal, scout state, and optional trading log...")
    insights_text = state.get("insights_text", "")
    signal_data = parse_agent_gold_signal(insights_text)
    current_gold_price = state.get("current_gold_price")

    if current_gold_price is None:
        try:
            current_gold_price = _safe_float(fetch_gold_price().get("gold", {}).get("price"))
        except Exception as exc:
            return {
                "signal_data": signal_data,
                "errors": _append_error(state, f"XAUUSD spot price refetch: {exc}"),
            }

    DISRUPTION_EVENTS_PATH.write_text(
        json.dumps(state.get("disruptive_events", []), indent=1, default=str),
        encoding="utf-8",
    )
    intraday_archive_record = {
        "timestamp": datetime.now().isoformat(),
        "cycle": state.get("cycle", 1),
        "spot_gold_price": current_gold_price,
        "intraday": state.get("intraday", {}),
    }
    DAY_TRADING_INTRADAY_SNAPSHOT_PATH.write_text(
        json.dumps(intraday_archive_record, indent=2, default=str),
        encoding="utf-8",
    )
    with DAY_TRADING_INTRADAY_ARCHIVE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(intraday_archive_record, default=str) + "\n")

    agent_snapshot = {
        "timestamp": datetime.now().isoformat(),
        "cycle": state.get("cycle", 1),
        "signal_data": signal_data,
        "current_gold_price": current_gold_price,
        "current_spot_gold_price": current_gold_price,
        "current_gold_price_symbol": SPOT_GOLD_SYMBOL,
        "current_gold_price_basis": "XAUUSD spot gold via gold-api XAU",
        "market_data_snapshot": state.get("market_data", {}),
        "intraday_snapshot": state.get("intraday", {}),
        "metric_recovery_plan": state.get("metric_recovery_plan", {}),
        "claude_search_plan": state.get("claude_search_plan", {}),
        "loopback_evaluation": state.get("loopback_evaluation", {}),
        "top_disruptive_events": state.get("disruptive_events", [])[:10],
        "errors": state.get("errors", []),
        "output_files": {
            "insights": str(DAY_TRADING_INSIGHTS_PATH),
            "live_data": str(DAY_TRADING_LIVE_DATA_PATH),
            "disruption_events": str(DISRUPTION_EVENTS_PATH),
            "loopback_evaluations": str(LOOPBACK_EVALUATIONS_PATH),
            "intraday_snapshot": str(DAY_TRADING_INTRADAY_SNAPSHOT_PATH),
            "intraday_archive": str(DAY_TRADING_INTRADAY_ARCHIVE_PATH),
        },
    }
    AGENT_STATE_PATH.write_text(
        json.dumps(agent_snapshot, indent=2, default=str),
        encoding="utf-8",
    )
    if state.get("loopback_evaluation"):
        with LOOPBACK_EVALUATIONS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(state["loopback_evaluation"], default=str) + "\n")

    if state.get("trading_log_enabled"):
        if signal_data["signal"] is None:
            print("Could not parse a signal from day trading insights; skipping log update.")
        elif current_gold_price is None:
            print("No gold price available; skipping signal log update.")
        else:
            log_df = update_signal_log(current_gold_price, signal_data)
            append_model_trade_log(signal_data, current_gold_price)
            print(f"\nSignal log updated. Latest row:\n{log_df.tail(1).to_string(index=False)}")
            print("\nRolling accuracy stats:")
            print(f"  7-day:    {log_df.iloc[-1]['ACCURACY_7D']}")
            print(f"  30-day:   {log_df.iloc[-1]['ACCURACY_30D']}")
            print(f"  All-time: {log_df.iloc[-1]['ACCURACY_ALL']}")

    return {
        "signal_data": signal_data,
        "current_gold_price": current_gold_price,
    }


def build_gold_agent_graph():
    if not HAS_LANGGRAPH:
        raise RuntimeError("LangGraph is not installed. Run: pip install langgraph")

    workflow = StateGraph(GoldAgentState)
    workflow.add_node("collect_market_context", collect_market_context)
    workflow.add_node("scout_internet", scout_internet)
    workflow.add_node("generate_trade_signal", generate_trade_signal)
    workflow.add_node("persist_results", persist_results)

    workflow.set_entry_point("collect_market_context")
    workflow.add_edge("collect_market_context", "scout_internet")
    workflow.add_edge("scout_internet", "generate_trade_signal")
    workflow.add_edge("generate_trade_signal", "persist_results")
    workflow.add_edge("persist_results", END)

    return workflow.compile()


def run_agent_cycle(trading_log_enabled: bool = False, cycle: int = 1) -> GoldAgentState:
    print("=" * 60)
    print(f"LANGGRAPH GOLD DISRUPTION AGENT - CYCLE {cycle}")
    print("=" * 60)

    graph = build_gold_agent_graph()
    initial_state: GoldAgentState = {
        "cycle": cycle,
        "trading_log_enabled": trading_log_enabled,
        "errors": [],
    }
    return graph.invoke(initial_state)


def analyze_day_trading() -> str:
    """Run one LangGraph agent cycle with no trading log side effects."""
    result = run_agent_cycle(trading_log_enabled=False, cycle=1)
    return result.get("insights_text", "")


def run_default() -> str:
    """Generate analysis only; no trading log side effects."""
    insights_text = analyze_day_trading()
    print("\n" + "=" * 60)
    print("DAY TRADING INSIGHTS:")
    print("=" * 60)
    print(insights_text)
    return insights_text


def run_trading() -> str:
    """Generate analysis and log to signal_log.csv for tracking."""
    result = run_agent_cycle(trading_log_enabled=True, cycle=1)
    insights_text = result.get("insights_text", "")
    print_cycle_trade_details(1, insights_text)
    return insights_text


def print_cycle_trade_details(cycle: int, insights_text: str) -> None:
    """Print the complete Claude trade analysis for terminal monitoring."""
    print("\n" + "=" * 60)
    print(f"DAY TRADING INSIGHTS - CYCLE {cycle}")
    print("=" * 60)
    print(insights_text or "(No insights text returned.)")
    print("=" * 60)


def run_agent_loop(
    interval_minutes: float = 5.0,
    trading_log_enabled: bool = True,
    run_once: bool = False,
    max_cycles: int | None = None,
) -> None:
    """Keep the LangGraph gold agent active and refresh signals on a schedule."""
    cycle = 0
    print(
        "Starting live LangGraph gold disruption agent "
        f"(interval: {interval_minutes}m, trading_log_enabled={trading_log_enabled})"
    )

    while True:
        cycle += 1
        result = run_agent_cycle(trading_log_enabled=trading_log_enabled, cycle=cycle)
        signal = result.get("signal_data", {}).get("signal")
        print(f"\n[agent] Cycle {cycle} complete. Latest signal: {signal or 'unparsed'}")
        print_cycle_trade_details(cycle, result.get("insights_text", ""))

        if result.get("errors"):
            print("[agent] Non-fatal errors:")
            for error in result["errors"]:
                print(f"  - {error}")

        if run_once or (max_cycles is not None and cycle >= max_cycles):
            break

        sleep_seconds = max(interval_minutes, 0.1) * 60
        print(f"[agent] Sleeping {interval_minutes} minutes before next internet scout...")
        time.sleep(sleep_seconds)


def parse_args():
    parser = argparse.ArgumentParser(
        description="LangGraph gold day-trading agent with live disruption scouting."
    )
    parser.add_argument(
        "--option",
        choices=("default", "trading", "agent"),
        default="default",
        help=(
            "default = one agent analysis only; trading = one analysis + signal logging; "
            "agent = continuous live scouting + signal logging."
        ),
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Run the live LangGraph agent loop regardless of --option.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="For --option agent, run one active agent cycle and exit.",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=5.0,
        help="Minutes between active internet-scout cycles in agent mode.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Optional maximum cycles for agent mode.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.option == "agent" or args.continuous:
        run_agent_loop(
            interval_minutes=args.interval_minutes,
            trading_log_enabled=True,
            run_once=args.once,
            max_cycles=args.max_cycles,
        )
    elif args.option == "trading":
        run_trading()
    else:
        run_default()
