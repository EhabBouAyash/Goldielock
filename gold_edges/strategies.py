"""The rules I am willing to defend, each with a reason it could exist."""
from __future__ import annotations

import numpy as np
import pandas as pd

from gold_edges.core import atr, rsi

TICK = 0.10


def tsmom(df: pd.DataFrame, lookback: int = 252, skip: int = 21) -> np.ndarray:
    """Sign of the return from lookback days ago to skip days ago (AQR 12-1).

    Moskowitz, Ooi, Pedersen 2012: assets that rose over the past year keep
    rising over the next month. Skipping the most recent month avoids the
    1-month reversal. Position is decided at the close, entered next open.
    """
    c = df.close
    past = c.shift(skip) / c.shift(lookback) - 1
    return np.sign(past.to_numpy())


def dual_ma(df: pd.DataFrame, fast: int = 50, slow: int = 200) -> np.ndarray:
    """Long when the fast SMA is above the slow SMA. Brock et al. 1992 style."""
    f = df.close.rolling(fast).mean()
    s = df.close.rolling(slow).mean()
    return np.sign((f - s).to_numpy())


def always_long(df: pd.DataFrame) -> np.ndarray:
    """Control: gold's structural bid, no timing."""
    return np.ones(len(df))


def clip_long(df: pd.DataFrame, hold: int = 21) -> list:
    """Always-long control that is forced to flatten every `hold` days.

    A single 2 ATR stop from 2011 never triggers in the 2020s bull, so a
    never-flatten long prints as one trade. Clips keep the control comparable
    to the other systems.
    """
    a = atr(df)
    C = df.close.to_numpy()
    sig = []
    i = 20
    while i < len(df) - 1:
        if np.isfinite(a[i]) and a[i] > 0:
            sig.append((i, 1, C[i], C[i] - 2.0 * a[i], "next_open", min(i + hold, len(df) - 1)))
        i += hold
    return sig


def connors_rsi2(df: pd.DataFrame, thresh: float = 5.0, exit_rsi: float = 65.0,
                 sma: int = 200, stop_k: float = 2.0) -> list:
    """Larry Connors: RSI(2) washout in an uptrend, exit when RSI(2) recovers.

    The 200-day filter is the whole point. Without it this is just buying
    falling knives. Stop is 2 ATR so a trend day cannot become a 10R loss.
    """
    r = rsi(df.close, 2)
    ma = df.close.rolling(sma).mean().to_numpy()
    a = atr(df)
    C = df.close.to_numpy()
    sig, in_trade = [], False
    for i in range(sma + 5, len(df) - 1):
        if in_trade:
            if r[i] >= exit_rsi or not np.isfinite(r[i]):
                in_trade = False
            continue
        if np.isfinite(r[i]) and r[i] < thresh and C[i] > ma[i] and a[i] > 0:
            stop = C[i] - stop_k * a[i]
            last = i + 1
            while last < len(df) - 1 and not (np.isfinite(r[last]) and r[last] >= exit_rsi):
                last += 1
            sig.append((i, 1, C[i], stop, "next_open", last))
            in_trade = True
    return sig


def down_streak(df: pd.DataFrame, days: int = 5, sma: int = 50, stop_k: float = 2.0) -> list:
    """Jegadeesh-style short-term reversal: N down closes in a row, above an SMA.

    Exit on the first close that is not a new low for the trade, or at 5 days,
    whichever first. Encoded as a 2 ATR stop plus a short time cap in the
    simulator (max_hold=5).
    """
    c = df.close.to_numpy()
    ma = df.close.rolling(sma).mean().to_numpy()
    a = atr(df)
    ret = df.close.diff().to_numpy()
    sig = []
    for i in range(sma + days, len(df) - 1):
        if not np.isfinite(a[i]) or a[i] <= 0 or c[i] <= ma[i]:
            continue
        if np.all(ret[i - days + 1:i + 1] < 0):
            sig.append((i, 1, c[i], c[i] - stop_k * a[i], "next_open"))
    return sig


def bollinger_reversion(df: pd.DataFrame, window: int = 20, k: float = 2.0, stop_k: float = 2.0) -> list:
    """Close below the lower 20,2 band, only when the 200-day SMA is rising."""
    mid = df.close.rolling(window).mean()
    sd = df.close.rolling(window).std()
    lower = (mid - k * sd).to_numpy()
    ma200 = df.close.rolling(200).mean()
    rising = (ma200 > ma200.shift(20)).to_numpy()
    C = df.close.to_numpy()
    a = atr(df)
    sig = []
    for i in range(220, len(df) - 1):
        if C[i] < lower[i] and rising[i] and a[i] > 0:
            sig.append((i, 1, C[i], C[i] - stop_k * a[i], "next_open"))
    return sig


def dip_in_uptrend(df: pd.DataFrame, stretch: float = 1.5, sma: int = 200, stop_k: float = 2.0) -> list:
    """Buy when close is stretch*ATR below the 20-day high and the 200-day SMA is rising.

    This is the mechanical form of 'buy the dip' that I actually believe in:
    a trend that has pulled back, not a support line drawn after the fact.
    """
    hh = df.high.rolling(20).max().to_numpy()
    ma = df.close.rolling(sma).mean()
    rising = (ma > ma.shift(20)).to_numpy()
    C = df.close.to_numpy()
    a = atr(df)
    sig = []
    for i in range(sma + 20, len(df) - 1):
        if rising[i] and a[i] > 0 and hh[i] - C[i] >= stretch * a[i]:
            sig.append((i, 1, C[i], C[i] - stop_k * a[i], "next_open"))
    return sig


def nr7_breakout(df: pd.DataFrame) -> list:
    """Toby Crabel NR7: today's range is the narrowest of the last 7 days.

    Buy a stop through tomorrow's break of the NR7 high; stop at the NR7 low.
    The idea is that realized vol is mean-reverting, so a quiet day is more
    likely to be followed by an expansion than by another quiet day.
    """
    H, L = df.high.to_numpy(), df.low.to_numpy()
    rng = H - L
    sig = []
    for i in range(7, len(df) - 1):
        window = rng[i - 6:i + 1]
        if rng[i] > 0 and rng[i] == window.min() and (window == rng[i]).sum() == 1:
            sig.append((i, 1, H[i] + TICK, L[i] - TICK, "stop"))
            sig.append((i, -1, L[i] - TICK, H[i] + TICK, "stop"))
    return sig


def atr_compression_breakout(df: pd.DataFrame, look: int = 20, q: float = 0.20) -> list:
    """Donchian breakout, but only when 10-day ATR is in the lowest quintile of the last 100."""
    a10 = pd.Series(atr(df, 10))
    quiet = a10 <= a10.rolling(100).quantile(q)
    hh = df.high.rolling(look).max().shift().to_numpy()
    ll = df.low.rolling(look).min().shift().to_numpy()
    a = atr(df)
    H, L = df.high.to_numpy(), df.low.to_numpy()
    qv = quiet.to_numpy()
    sig = []
    for i in range(120, len(df)):
        if not qv[i] or not np.isfinite(a[i]) or a[i] <= 0:
            continue
        if H[i] > hh[i] and H[i - 1] <= hh[i - 1]:
            sig.append((i, 1, hh[i] + TICK, hh[i] + TICK - 2 * a[i], "now"))
        elif L[i] < ll[i] and L[i - 1] >= ll[i - 1]:
            sig.append((i, -1, ll[i] - TICK, ll[i] - TICK + 2 * a[i], "now"))
    return sig
