"""GOLD-OCT26-v1 indicators: seeded EMA and Wilder ATR. A5/A15 use ATR through t-1."""
from __future__ import annotations

import numpy as np


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = len(close)
    tr = np.empty(n, dtype=float)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    return tr


def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """Seed with the mean of the first n true ranges, then Wilder smooth."""
    tr = true_range(high, low, close)
    out = np.full(len(tr), np.nan)
    if len(tr) < n or n <= 0:
        return out
    out[n - 1] = float(np.mean(tr[:n]))
    for i in range(n, len(tr)):
        out[i] = ((n - 1) * out[i - 1] + tr[i]) / n
    return out


def atr_prior(high: np.ndarray, low: np.ndarray, close: np.ndarray, t: int, n: int = 14) -> float:
    """ATR14 through t-1 (excludes the trigger candle)."""
    if t < 1:
        return float("nan")
    atr = wilder_atr(high[:t], low[:t], close[:t], n)
    return float(atr[t - 1]) if t - 1 < len(atr) else float("nan")


def ema_seeded(close: np.ndarray, n: int) -> np.ndarray:
    """First n-bar SMA seed, then alpha = 2/(n+1)."""
    out = np.full(len(close), np.nan)
    if n <= 0 or len(close) < n:
        return out
    out[n - 1] = float(np.mean(close[:n]))
    alpha = 2.0 / (n + 1)
    for i in range(n, len(close)):
        out[i] = alpha * close[i] + (1.0 - alpha) * out[i - 1]
    return out


def h4_direction(close: np.ndarray, ema20: np.ndarray, ema50: np.ndarray, t: int) -> str:
    """Direction of the last *completed* H4 bar t. Requires t >= 3 and finite EMAs."""
    if t < 3 or t >= len(close):
        return "NEUTRAL"
    c, e20, e50 = close[t], ema20[t], ema50[t]
    e20_prev3 = ema20[t - 3]
    if not all(np.isfinite(x) for x in (c, e20, e50, e20_prev3)):
        return "NEUTRAL"
    if c > e20 > e50 and e20 > e20_prev3:
        return "UP"
    if c < e20 < e50 and e20 < e20_prev3:
        return "DOWN"
    return "NEUTRAL"
