"""T3 — Bitcoin daily 5/100, long or cash. Never short."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from deepseek_trial.indicators import sma, wilder_atr

STRATEGY_ID = "T3"
INSTRUMENT = "BTCUSD"
EXPERIMENTAL = False
WARMUP = 120
FAST = 5
SLOW = 100
BAND = 1.01
ATR_N = 14
STOP_K = 2.0
MAX_ENTRIES_PER_WEEK = 2
HOLD_WEEKEND = False


@dataclass
class T3View:
    state: str
    sma5: float
    sma100: float
    close: float
    atr: float
    reason: str


def t3_view(close: np.ndarray, high: np.ndarray, low: np.ndarray, t: int) -> T3View:
    if t < max(WARMUP, SLOW) - 1:
        return T3View("CASH", float("nan"), float("nan"), float("nan"), float("nan"), "DATA_MISSING")
    fast = sma(close, FAST)[t]
    slow = sma(close, SLOW)[t]
    atr = wilder_atr(high, low, close, ATR_N)[t]
    c = close[t]
    if not all(np.isfinite(x) for x in (fast, slow, atr, c)) or atr <= 0 or slow <= 0:
        return T3View("CASH", fast, slow, c, atr, "DATA_MISSING")
    state = "LONG" if fast > slow * BAND else "CASH"
    return T3View(state, float(fast), float(slow), float(c), float(atr), "VALID_SIGNAL" if state == "LONG" else "NO_SIGNAL")


def t3_signal(
    view: T3View,
    prev_state: str,
    in_long: bool,
    stop_blocked: bool,
    entries_this_week: int,
) -> tuple[str, str]:
    """Return (action, reason). Never emits a short."""
    if view.reason == "DATA_MISSING":
        return ("CLOSE", "DATA_MISSING") if in_long else ("NO_TRADE", "DATA_MISSING")
    if in_long and view.state != "LONG":
        return "CLOSE", "SIGNAL_EXIT"
    if in_long:
        return "HOLD", "NO_SIGNAL"
    transition = prev_state != "LONG" and view.state == "LONG"
    if not transition:
        return "NO_TRADE", "NO_SIGNAL"
    if stop_blocked:
        return "NO_TRADE", "NO_SIGNAL"
    if entries_this_week >= MAX_ENTRIES_PER_WEEK:
        return "NO_TRADE", "RISK_LIMIT"
    return "OPEN", "VALID_SIGNAL"
