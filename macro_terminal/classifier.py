"""Classification pass for macro terminal items."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from macro_terminal.models import GoldBias, ItemType, PriorityBand, TerminalItem


CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("fed", ["fed", "fomc", "powell", "federal reserve", "central bank", "rate cut", "rate hike"]),
    ("inflation", ["cpi", "pce", "inflation", "breakeven", "hot cpi", "deflation"]),
    ("geopolitics", ["war", "attack", "sanction", "ceasefire", "geopolitical", "missile", "conflict"]),
    ("fx", ["dxy", "dollar", "usd", "forex", "currency"]),
    ("gold", ["gold", "xau", "comex", "bullion", "precious metal"]),
    ("calendar", ["calendar", "nfp", "payroll", "jobs report", "gdp", "ism", "retail sales"]),
    ("markets", ["yield", "treasury", "bond", "equity", "risk-on", "risk-off", "spx", "oil"]),
]

REGIME_TAG_RULES: list[tuple[str, list[str]]] = [
    ("USD_STRONG", ["stronger dollar", "dollar rises", "dollar jumps", "dxy up", "hawkish"]),
    ("USD_WEAK", ["weaker dollar", "dollar falls", "dollar drops", "dxy down", "dovish"]),
    ("RISK_OFF", ["safe haven", "banking crisis", "war", "attack", "sanction", "risk-off"]),
    ("RISK_ON", ["risk-on", "ceasefire", "stocks rally", "equities rise"]),
    ("INFLATION_UP", ["hot cpi", "hot pce", "inflation surprise", "sticky inflation"]),
    ("INFLATION_DOWN", ["disinflation", "cooling inflation", "deflation"]),
]

BREAKING_TERMS = ("breaking", "urgent", "surprise", "unexpected", "just in", "flash")

P1_TERMS = (
    "fomc statement", "fomc decision", "rate decision", "emergency meeting",
    "emergency rate", "surprise rate", "surprise cut", "surprise hike",
    "central bank surprise", "intervention", "war escalation", "missile strike",
    "invasion", "troops deployed", "live fomc", "active fomc",
)
P1_HOT_WAR = ("war", "attack", "missile", "strike", "invasion", "escalat")
P2_TERMS = (
    "powell", "fed speaker", "fed governor", "fed official", "fomc minutes",
    "cpi", "pce", "nfp", "payroll", "beat expectations", "miss expectations",
    "vs expected", "sanction", "tariff", "jobs report", "inflation data",
)
P3_TERMS = (
    "cot", "commitment of traders", "positioning", "slow drift", "outlook",
    "analysis", "commentary", "weekly wrap", "background", "geopolitical drift",
)


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def impact_score(text: str) -> tuple[int, str]:
    """Keyword disruption score and gold direction hint (mirrors day_trading_insights)."""
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
        direction = GoldBias.BULLISH.value
    elif bearish_hits > bullish_hits:
        direction = GoldBias.BEARISH.value
    elif bullish_hits or bearish_hits:
        direction = GoldBias.TWO_WAY.value
    else:
        direction = GoldBias.UNKNOWN.value

    return min(score + max(bullish_hits, bearish_hits), 10), direction


def _detect_category(text: str) -> str:
    lowered = text.lower()
    for category, terms in CATEGORY_RULES:
        if any(term in lowered for term in terms):
            return category
    return "markets"


def _detect_regime_tags(text: str) -> list[str]:
    lowered = text.lower()
    tags = [tag for tag, terms in REGIME_TAG_RULES if any(term in lowered for term in terms)]
    return tags[:4]


def _gold_bias_from_hint(hint: str) -> str:
    mapping = {
        "BULLISH_GOLD": GoldBias.BULLISH.value,
        "BEARISH_GOLD": GoldBias.BEARISH.value,
        "TWO_WAY": GoldBias.TWO_WAY.value,
        "UNKNOWN": GoldBias.UNKNOWN.value,
    }
    return mapping.get(hint, hint)


def classify_priority_tier(
    text: str,
    *,
    item_type: str,
    disruption_score: int,
) -> tuple[str, str]:
    """
    Assign P1 / P2 / P3 tier (semantic rules first, score fallback).
    Returns (tier, reason).
    """
    lowered = text.lower()
    is_breaking = any(term in lowered for term in BREAKING_TERMS)

    if any(term in lowered for term in P1_TERMS):
        return PriorityBand.P1.value, "immediate_mover_keyword"
    if is_breaking and any(term in lowered for term in ("fomc", "rate decision", "central bank", "fed cuts", "fed hikes")):
        return PriorityBand.P1.value, "breaking_cb_move"
    if is_breaking and any(term in lowered for term in P1_HOT_WAR):
        return PriorityBand.P1.value, "hot_war_escalation"
    if item_type == ItemType.CALENDAR.value and disruption_score >= 5:
        return PriorityBand.P1.value, "high_impact_calendar"

    if any(term in lowered for term in P2_TERMS):
        return PriorityBand.P2.value, "high_relevance_keyword"
    if item_type == ItemType.REGIME.value:
        return PriorityBand.P2.value, "macro_regime"
    if item_type == ItemType.CALENDAR.value:
        return PriorityBand.P2.value, "calendar_event"
    if disruption_score >= 6 or (is_breaking and disruption_score >= 4):
        return PriorityBand.P2.value, "elevated_disruption"

    if any(term in lowered for term in P3_TERMS):
        return PriorityBand.P3.value, "background_context"
    if disruption_score >= 3:
        return PriorityBand.P3.value, "moderate_disruption"
    return PriorityBand.P3.value, "default_context"


def _age_decay_minutes(published_at: str | None) -> float:
    if not published_at:
        return 0.0
    try:
        if published_at.endswith("Z"):
            published_at = published_at.replace("Z", "+00:00")
        published = datetime.fromisoformat(published_at)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 60
        return max(0.0, age_min)
    except (TypeError, ValueError):
        return 0.0


def compute_priority_score(
    disruption_score: int,
    text: str,
    published_at: str | None,
    item_type: str,
    regime_tags: list[str],
) -> float:
    lowered = text.lower()
    score = float(disruption_score) * 8.0
    if any(term in lowered for term in BREAKING_TERMS):
        score += 20.0
    if item_type == ItemType.CALENDAR.value:
        score += 12.0
    if item_type == ItemType.MACRO.value:
        score += 8.0
    if len(regime_tags) >= 2:
        score += 10.0
    score -= _age_decay_minutes(published_at) / 30.0
    return max(0.0, min(score, 100.0))


def make_item_id(title: str, source: str, published_at: str) -> str:
    raw = f"{normalize_title(title)}|{source}|{published_at}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def classify_raw_item(raw: dict[str, Any], *, default_type: str = ItemType.NEWS.value) -> TerminalItem:
    title = (raw.get("title") or "").strip()
    summary = (raw.get("description") or raw.get("summary") or "").strip()
    source = raw.get("source") or raw.get("feed") or "unknown"
    published_at = raw.get("published_at") or raw.get("published") or ""
    url = raw.get("url") or ""
    item_type = raw.get("item_type") or default_type

    text = f"{title} {summary}".strip()
    disruption, direction_hint = impact_score(text)
    if raw.get("disruption_score") is not None:
        disruption = int(raw["disruption_score"])
    if raw.get("direction_hint"):
        direction_hint = _gold_bias_from_hint(str(raw["direction_hint"]))

    category = raw.get("category") or _detect_category(text)
    regime_tags = _detect_regime_tags(text)
    if raw.get("regime_tags"):
        regime_tags = list(dict.fromkeys(regime_tags + list(raw["regime_tags"])))

    priority_score = compute_priority_score(
        disruption, text, published_at, item_type, regime_tags
    )
    if raw.get("priority_score") is not None:
        priority_score = float(raw["priority_score"])

    tier, tier_reason = classify_priority_tier(
        text, item_type=item_type, disruption_score=disruption
    )
    if raw.get("priority_band") in {b.value for b in PriorityBand}:
        tier = raw["priority_band"]
        if tier == "P0":
            tier = PriorityBand.P1.value

    reasons = []
    if disruption:
        reasons.append(f"disruption={disruption}")
    reasons.append(f"tier={tier_reason}")
    if regime_tags:
        reasons.append(f"tags={','.join(regime_tags)}")
    if any(term in text.lower() for term in BREAKING_TERMS):
        reasons.append("breaking")

    return TerminalItem(
        item_id=make_item_id(title, str(source), str(published_at)),
        item_type=item_type,
        title=title or "(untitled)",
        summary=summary[:280],
        source=str(source),
        published_at=str(published_at),
        url=url,
        category=category,
        gold_bias=direction_hint,
        regime_tags=regime_tags,
        priority_score=round(priority_score, 1),
        priority_band=tier,
        disruption_score=disruption,
        classification_reason="; ".join(reasons) or "baseline",
        ingested_at=datetime.now(timezone.utc).isoformat(),
    )
