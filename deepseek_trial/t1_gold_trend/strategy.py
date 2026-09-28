"""T1 — gold daily trend-or-cash: 12-1 and close > SMA(200). Long or flat only."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from deepseek_trial.indicators import wilder_atr

STRATEGY_ID = "T1"
INSTRUMENT = "XAUUSD"
EXPERIMENTAL = False
WARMUP = 260
LOOKBACK = 252
SKIP = 21
SMA_N = 200
ATR_N = 20
STOP_K = 2.0
HOLD_WEEKEND = True


@dataclass
class T1View:
    allow_long: bool
    ret_12_1: float
    sma200: float
    close: float
    atr: float
    reason: str


def t1_view(close: np.ndarray, high: np.ndarray, low: np.ndarray, t: int) -> T1View:
    if t < WARMUP or t < LOOKBACK or t < SMA_N - 1:
        return T1View(False, float("nan"), float("nan"), float("nan"), float("nan"), "DATA_MISSING")
    c_now = close[t]
    c_skip = close[t - SKIP]
    c_lb = close[t - LOOKBACK]
    if min(c_now, c_skip, c_lb) <= 0:
        return T1View(False, float("nan"), float("nan"), c_now, float("nan"), "DATA_MISSING")
    ret = c_skip / c_lb - 1.0
    ma = float(np.mean(close[t - SMA_N + 1:t + 1]))
    atr = wilder_atr(high, low, close, ATR_N)[t]
    if not np.isfinite(ret) or not np.isfinite(ma) or not np.isfinite(atr) or atr <= 0:
        return T1View(False, ret, ma, c_now, atr, "DATA_MISSING")
    allow = ret > 0 and c_now > ma
    return T1View(allow, ret, ma, c_now, atr, "VALID_SIGNAL" if allow else "NO_SIGNAL")


def t1_signal(view: T1View, in_long: bool) -> tuple[str, str]:
    """Return (action, reason) ignoring calendar/risk. Never short."""
    if view.reason == "DATA_MISSING":
        return ("CLOSE", "DATA_MISSING") if in_long else ("NO_TRADE", "DATA_MISSING")
    if in_long and not view.allow_long:
        return "CLOSE", "SIGNAL_EXIT"
    if (not in_long) and view.allow_long:
        return "OPEN", "VALID_SIGNAL"
    if in_long:
        return "HOLD", "NO_SIGNAL"
    return "NO_TRADE", "NO_SIGNAL"


def ratchet_stop(close: float, atr: float, current_stop: float, tick: float) -> float | None:
    if not np.isfinite(close) or not np.isfinite(atr) or atr <= 0:
        return None
    candidate = close - STOP_K * atr
    if candidate > current_stop:
        return candidate
    return None
