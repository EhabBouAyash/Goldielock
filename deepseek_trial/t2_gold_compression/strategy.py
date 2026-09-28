"""T2 — gold H1 compression breakout (EXPERIMENTAL). Both sides. Quiet ATR only."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from deepseek_trial.indicators import percentile_inclusive, wilder_atr

STRATEGY_ID = "T2"
INSTRUMENT = "XAUUSD"
EXPERIMENTAL = True
WARMUP = 120
ATR_QUIET_N = 10
ATR_STOP_N = 20
QUIET_WINDOW = 100
QUIET_PCT = 20.0
DONCHIAN = 20
STOP_K = 2.0
TIME_EXIT_BARS = 24
HOLD_WEEKEND = False


@dataclass
class T2View:
    quiet: bool
    upper20: float
    lower20: float
    close: float
    atr: float
    break_up: bool
    break_down: bool
    reason: str


def t2_view(close: np.ndarray, high: np.ndarray, low: np.ndarray, t: int,
            atr10: np.ndarray | None = None, atr20: np.ndarray | None = None) -> T2View:
    if atr10 is None:
        atr10 = wilder_atr(high, low, close, ATR_QUIET_N)
    if atr20 is None:
        atr20 = wilder_atr(high, low, close, ATR_STOP_N)
    need = max(WARMUP, QUIET_WINDOW, DONCHIAN, ATR_STOP_N)
    if t < need:
        return T2View(False, float("nan"), float("nan"), float("nan"), float("nan"),
                      False, False, "DATA_MISSING")
    window = atr10[t - QUIET_WINDOW + 1:t + 1]
    if window.size < QUIET_WINDOW or not np.all(np.isfinite(window)):
        return T2View(False, float("nan"), float("nan"), close[t], atr20[t],
                      False, False, "DATA_MISSING")
    q20 = percentile_inclusive(window, QUIET_PCT)
    quiet = bool(np.isfinite(atr10[t]) and np.isfinite(q20) and atr10[t] <= q20)
    upper = float(np.max(high[t - DONCHIAN:t]))
    lower = float(np.min(low[t - DONCHIAN:t]))
    c = float(close[t])
    a = float(atr20[t])
    if not np.isfinite(a) or a <= 0:
        return T2View(quiet, upper, lower, c, a, False, False, "DATA_MISSING")
    up = quiet and c > upper
    down = quiet and c < lower
    if not quiet and (c > upper or c < lower):
        return T2View(False, upper, lower, c, a, False, False, "NOT_QUIET")
    reason = "VALID_SIGNAL" if (up or down) else "NO_SIGNAL"
    return T2View(quiet, upper, lower, c, a, up, down, reason)


def t2_signal(view: T2View, in_position: bool) -> tuple[str, str, str]:
    """Return (action, side, reason). Side NONE unless OPEN."""
    if view.reason == "DATA_MISSING":
        return ("CLOSE", "NONE", "DATA_MISSING") if in_position else ("NO_TRADE", "NONE", "DATA_MISSING")
    if in_position:
        return "HOLD", "NONE", "NO_SIGNAL"
    if view.break_up:
        return "OPEN", "BUY", "VALID_SIGNAL"
    if view.break_down:
        return "OPEN", "SELL", "VALID_SIGNAL"
    return "NO_TRADE", "NONE", view.reason
