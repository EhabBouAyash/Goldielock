"""Collect macro and news inputs shared with day_trading_insights."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import additional_news
from generate_insights import fetch_dxy, fetch_gold_price, fetch_macro, fetch_treasury_snapshot
from macro_terminal.classifier import classify_raw_item, impact_score
from macro_terminal.models import ItemType, MacroTicker, TerminalItem

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTRADAY_CACHE_PATHS = [
    PROJECT_ROOT / "day_trading_intraday_snapshot.json",
    PROJECT_ROOT / "intraday_data.json",
]


def _safe_fetch(label: str, fn: Callable[[], Any], default: Any) -> tuple[Any, str | None]:
    try:
        return fn(), None
    except Exception as exc:
        return default, f"{label}: {exc}"


def _load_intraday_snapshot() -> dict[str, Any]:
    for path in INTRADAY_CACHE_PATHS:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        intraday = payload.get("intraday") if isinstance(payload, dict) else payload
        if isinstance(intraday, dict) and intraday.get("intervals"):
            intraday = dict(intraday)
            intraday["cache_source"] = str(path)
            return intraday
    return {}


def fetch_market_bundle() -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []

    metals, err = _safe_fetch("gold/silver", fetch_gold_price, {"gold": {}, "silver": {}})
    if err:
        errors.append(err)

    real_yield, err = _safe_fetch("10Y real yield", lambda: fetch_macro("DFII10"), None)
    if err:
        errors.append(err)

    breakeven, err = _safe_fetch("10Y breakeven", lambda: fetch_macro("T10YIE"), None)
    if err:
        errors.append(err)

    dxy_data, err = _safe_fetch("DXY", fetch_dxy, {})
    if err:
        errors.append(err)

    treasury, err = _safe_fetch("Treasury snapshot", fetch_treasury_snapshot, {})
    if err:
        errors.append(err)

    gold_spot = metals.get("gold") or {}
    market = {
        "gold_spot": gold_spot,
        "silver_spot": metals.get("silver") or {},
        "real_yield": real_yield,
        "breakeven": breakeven,
        "dxy": dxy_data,
        "treasury": treasury,
    }
    return market, errors


def build_macro_ticker(market: dict[str, Any]) -> MacroTicker:
    gold = market.get("gold_spot") or {}
    dxy = market.get("dxy") or {}
    treasury = market.get("treasury") or {}

    def _f(value: Any) -> float | None:
        try:
            if value in (None, "", "n/a"):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    return MacroTicker(
        gold_spot=_f(gold.get("price")),
        dxy=_f(dxy.get("value")),
        dxy_change_pct=_f(dxy.get("change_pct")),
        real_yield=_f(market.get("real_yield")),
        breakeven=_f(market.get("breakeven")),
        treasury_10y=_f(treasury.get("y10_latest") or treasury.get("yield_10y")),
        treasury_10y_bps_1d=_f(treasury.get("y10_daily_change_bps") or treasury.get("change_bps_1d")),
    )


def fetch_additional_news_items(max_total: int = 25) -> list[dict[str, Any]]:
    articles = additional_news.fetch_all(max_total=max_total, per_feed_limit=6)
    for article in articles:
        if article.get("kind") == "forex_factory_calendar" or article.get("category") == "economic_calendar":
            article["item_type"] = ItemType.CALENDAR.value
        else:
            article["item_type"] = ItemType.NEWS.value
    return articles


def fetch_gnews_items(market: dict[str, Any], max_queries: int = 4) -> list[dict[str, Any]]:
    from day_trading_insights import build_disruption_queries, search_gnews_for_disruptions

    queries = build_disruption_queries(market)[:max_queries]
    events = search_gnews_for_disruptions(queries, max_per_query=4, prefilter_min_score=1)
    for event in events:
        event["item_type"] = ItemType.NEWS.value
    return events


def build_regime_items(regime_state) -> list[TerminalItem]:
    item = classify_raw_item(
        {
            "title": f"Macro regime: {regime_state.primary_regime}",
            "description": regime_state.narrative,
            "source": "macro_terminal",
            "published_at": "",
            "item_type": ItemType.REGIME.value,
            "category": "markets",
            "direction_hint": regime_state.gold_bias,
            "regime_tags": [regime_state.primary_regime],
            "priority_score": 45 + min(regime_state.clarity, 10),
        },
        default_type=ItemType.REGIME.value,
    )
    return [item]


def classify_news_batch(raw_items: list[dict[str, Any]]) -> list[TerminalItem]:
    classified: list[TerminalItem] = []
    for raw in raw_items:
        if raw.get("fetch_error"):
            continue
        title = (raw.get("title") or "").strip()
        if not title or title.startswith("News scout failed for query:"):
            continue
        if title == "GNews API key missing":
            continue
        text = f"{title} {raw.get('description') or ''}"
        disruption, direction = impact_score(text)
        if raw.get("relevance_score") is not None:
            disruption = max(disruption, int(raw["relevance_score"]) // 2)
        raw = dict(raw)
        raw.setdefault("disruption_score", disruption)
        raw.setdefault("direction_hint", direction)
        classified.append(classify_raw_item(raw))
    return classified
