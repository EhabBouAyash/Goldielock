"""Wilder ATR, SMA, and empirical percentile — no look-ahead windows."""
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


def wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    """Seed with the mean of the first n true ranges, then Wilder smooth."""
    tr = true_range(high, low, close)
    out = np.full(len(tr), np.nan)
    if len(tr) < n or n <= 0:
        return out
    out[n - 1] = float(np.mean(tr[:n]))
    for i in range(n, len(tr)):
        out[i] = ((n - 1) * out[i - 1] + tr[i]) / n
    return out


def sma(close: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if n <= 0 or len(close) < n:
        return out
    csum = np.cumsum(close, dtype=float)
    out[n - 1] = csum[n - 1] / n
    for i in range(n, len(close)):
        out[i] = (csum[i] - csum[i - n]) / n
    return out


def percentile_inclusive(window: np.ndarray, q: float) -> float:
    finite = window[np.isfinite(window)]
    if finite.size == 0:
        return float("nan")
    return float(np.percentile(finite, q))
