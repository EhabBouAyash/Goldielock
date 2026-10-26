"""Shared types for the unvalidated GOLD-ORB-v2 research track.

These rules are a starting specification. Nothing here is a claim that the
method makes money.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

SPEC_VERSION = "GOLD-ORB-v2"
SPEC_STATUS = "unvalidated_research_specification"

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# London opening range and the only window that may produce an entry.
OR_START = time(8, 0)
OR_END = time(8, 30)  # half-open [08:00, 08:30)
ENTRY_START = time(8, 30)
ENTRY_END = time(10, 30)  # half-open; 10:30 does not enter
FLAT_BY = time(11, 0)

RETEST_BARS = 3
STOP_ATR_MULT = 0.10
ATR_N = 14
TARGET_R = 2.0
EMA_FAST = 20
EMA_SLOW = 50
MAX_FILLS_PER_DAY = 1

INITIAL_BALANCE = 25_000.0
RISK_FRACTION = 0.0025
R_BASE = INITIAL_BALANCE * RISK_FRACTION  # $62.50, not compounded, not 1%
DAILY_LOSS_HARD = 750.0
TOTAL_LOSS_HARD = 1_500.0
EQUITY_FLOOR = INITIAL_BALANCE - TOTAL_LOSS_HARD  # $23,500
PROFIT_TARGET = 2_500.0
MIN_TRADING_DAYS = 2

# Subtracted from remaining room so a full stop is not planned on the line.
BUFFER_DAILY = 25.0
BUFFER_TOTAL = 25.0
BUFFER_EXPOSURE = 10.0

FRICTION_MAX_R = 0.10
CONTRACT = 100.0
VOLUME_STEP = 0.01
MIN_VOLUME = 0.01

# Research cost assumption until the MT5 symbol replaces it.
# Bar prices are treated as mids. A live quote uses the real bid and ask.
DEFAULT_SPREAD = 0.30
DEFAULT_SLIPPAGE = 0.05
COMMISSION_RATE_PER_SIDE = 0.000016
DEFAULT_LEVERAGE = 100.0

DATA_MAX_AGE = timedelta(minutes=6)
QUOTE_MAX_AGE = timedelta(seconds=5)
NEWS_BEFORE = timedelta(minutes=30)
NEWS_AFTER = timedelta(minutes=30)
NEWS_FLATTEN_BEFORE = timedelta(minutes=15)
QUICK_STRIKE = timedelta(minutes=5)

VARIANTS = ("orb_only", "orb_h1_trend")


def shift_time(value: time, minutes: int) -> time:
    """Move a clock time by a few minutes without crossing into another date."""
    base = datetime(2000, 1, 3, value.hour, value.minute) + timedelta(minutes=minutes)
    return base.time()


@dataclass(frozen=True)
class Bar:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float

    def ok(self) -> bool:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            return False
        if self.end <= self.start:
            return False
        prices = (self.open, self.high, self.low, self.close)
        if not all(isinstance(p, (int, float)) and p == p and p > 0 for p in prices):
            return False
        return self.high >= max(self.open, self.close, self.low) and self.low <= min(self.open, self.close)


@dataclass(frozen=True)
class CostModel:
    spread: float = DEFAULT_SPREAD
    slippage_per_side: float = DEFAULT_SLIPPAGE
    commission_rate_per_side: float = COMMISSION_RATE_PER_SIDE
    leverage: float = DEFAULT_LEVERAGE

    def entry_from_mid(self, side: str, mid: float) -> float:
        half = self.spread / 2.0
        if side == "LONG":
            return mid + half + self.slippage_per_side
        return mid - half - self.slippage_per_side

    def exit_from_mid(self, side: str, mid: float) -> float:
        half = self.spread / 2.0
        if side == "LONG":
            return mid - half - self.slippage_per_side
        return mid + half + self.slippage_per_side

    def friction_price(self, mid: float, spread: float | None = None) -> float:
        width = self.spread if spread is None else spread
        return width + 2.0 * self.slippage_per_side + 2.0 * self.commission_rate_per_side * mid


@dataclass(frozen=True)
class NewsEvent:
    start: datetime
    end: datetime
    title: str
    tag_only: bool = False
    block_afternoon: bool = False


@dataclass
class CalendarState:
    loaded: bool
    events: list[NewsEvent] = field(default_factory=list)
    reason: str = ""
    as_of: datetime | None = None


@dataclass
class AccountSnapshot:
    source: str
    as_of: datetime | None
    authoritative: bool
    research_only: bool
    balance: float
    equity: float
    free_margin: float
    margin_used: float
    daily_trade_pnl: float
    open_risk_usd: float
    pending_risk_usd: float
    open_positions: int
    pending_orders: int
    withdrawals_usd: float
    automation_permitted: bool
    missing_stop: bool
    reconciliation_ok: bool
    notes: tuple[str, ...] = ()


@dataclass
class Quote:
    time: datetime
    bid: float
    ask: float
    source: str

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class MarketData:
    source: str
    tradable: bool
    reason: str
    m5: list[Bar]
    h1: list[Bar]
    as_of: datetime | None
    quote: Quote | None = None
    real_yield: dict | None = None
    display_only: bool = False


@dataclass
class SignalEvent:
    setup_id: str
    status: str
    reason: str
    side: str
    stop: float
    boundary: float
    retest_extreme: float
    atr: float
    opposing_high: float | None
    opposing_low: float | None
    signal_time: datetime
    london_date: str
    variant: str
    bar_index: int


@dataclass
class Decision:
    signal: str
    reason: str
    setup_id: str = ""
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    lots: float = 0.0
    risk_usd: float = 0.0
    friction_r: float | None = None
    r_budget: float | None = None
    explanation: str = ""
    execution: str = "NO_TRADE"
    send_order: bool = False
    notes: list[str] = field(default_factory=list)
    as_of: str = ""
    context: dict = field(default_factory=dict)
