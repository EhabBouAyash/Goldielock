"""Data models for the macro terminal."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class ItemType(str, Enum):
    NEWS = "NEWS"
    MACRO = "MACRO"
    CALENDAR = "CALENDAR"
    REGIME = "REGIME"


class GoldBias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    TWO_WAY = "TWO_WAY"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class PriorityBand(str, Enum):
    P1 = "P1"  # immediate market movers
    P2 = "P2"  # high relevance
    P3 = "P3"  # background context
    EXPIRED = "expired"


@dataclass
class TerminalItem:
    item_id: str
    item_type: str
    title: str
    summary: str
    source: str
    published_at: str
    url: str
    category: str
    gold_bias: str
    regime_tags: list[str] = field(default_factory=list)
    priority_score: float = 0.0
    priority_band: str = PriorityBand.P3.value
    disruption_score: int = 0
    classification_reason: str = ""
    ingested_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MacroRegimeState:
    primary_regime: str
    gold_bias: str
    clarity: int
    narrative: str
    drivers: list[str] = field(default_factory=list)
    confluence_label: str = ""
    confluence_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MacroTicker:
    gold_spot: float | None = None
    dxy: float | None = None
    dxy_change_pct: float | None = None
    real_yield: float | None = None
    breakeven: float | None = None
    treasury_10y: float | None = None
    treasury_10y_bps_1d: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TerminalSnapshot:
    timestamp_et: str
    candle_15m_window: str
    tick_in_candle: int
    macro_ticker: MacroTicker | None
    regime: MacroRegimeState | None
    top_items: list[dict[str, Any]] = field(default_factory=list)
    priority_store: dict[str, int] = field(default_factory=dict)
    queue_depth: int = 0
    expired_count: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_et": self.timestamp_et,
            "candle_15m_window": self.candle_15m_window,
            "tick_in_candle": self.tick_in_candle,
            "macro_ticker": self.macro_ticker.to_dict() if self.macro_ticker else None,
            "regime": self.regime.to_dict() if self.regime else None,
            "top_items": self.top_items,
            "priority_store": self.priority_store,
            "queue_depth": self.queue_depth,
            "expired_count": self.expired_count,
            "errors": self.errors,
        }
