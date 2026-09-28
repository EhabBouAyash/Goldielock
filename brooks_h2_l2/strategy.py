"""High 2 / Low 2 setup detection (Al Brooks) on completed bars, pandas/NumPy only.

A High 2 is the second attempt, inside a bull pullback, for a bar to trade above
the prior bar's high. Low 2 is the mirror image, and is detected by running the
High 2 routine on the price series reflected through zero.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Config:
    timeframe: str = "5min"
    htf: str | None = "1h"          # higher-timeframe trend filter; None disables it
    htf_ema: int = 20
    htf_slope_bars: int = 3
    ema: int = 20
    slope_bars: int = 5
    atr_period: int = 14
    stop_mode: str = "pullback"     # "pullback" (below the pullback low) or "signal_bar"
    rr: float = 1.5
    max_pullback_bars: int = 20     # longer pullbacks are treated as a new structure, not an H2
    signal_close_pos: float = 0.5   # long signal bar must close in the upper part of its range
    min_risk_atr: float = 0.3
    max_risk_atr: float = 3.0
    min_risk_bps: float = 13.0      # risk >= 0.13% of price keeps spread+slippage under ~20% of 1R
    session: str = "all"            # "all" or "london_ny" (07:00-20:00 UTC decisions)
    valid_bars: int = 1             # stop order lives for this many bars after the signal bar
    max_hold_bars: int = 48
    scale_out_r: float | None = None  # take half off here and move the stop to breakeven
    tick: float = 0.10

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = bars["close"].shift()
    tr = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev).abs(), (bars["low"] - prev).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def h2_long_setups(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    trend_ok: np.ndarray,
    max_pullback_bars: int,
    signal_close_pos: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (is_signal_bar, signal_bar_low, pullback_low) per bar.

    A pullback starts with the first lower high after the leg high. Each bar whose
    high exceeds the prior bar's high, after at least one lower high, is one
    attempt (H1, H2, ...). After H1, any bar that resumes the pullback with a lower
    high is an H2 signal bar: a buy stop goes one tick above it. A new leg high
    resets the count.
    """
    n = len(high)
    signal = np.zeros(n, dtype=bool)
    pb_low_out = np.full(n, np.nan)
    leg_high = -np.inf
    in_pb = armed = False
    count = pb_len = 0
    pb_low = np.inf
    for i in range(1, n):
        if high[i] > leg_high:
            leg_high, in_pb, armed, count, pb_len, pb_low = high[i], False, False, 0, 0, np.inf
            continue
        if not in_pb:
            if high[i] < high[i - 1]:
                in_pb, armed, pb_len, pb_low = True, True, 0, low[i]
            else:
                continue
        pb_low = min(pb_low, low[i])
        pb_len += 1
        if high[i] > high[i - 1]:
            if armed:
                count += 1
                armed = False
        elif high[i] < high[i - 1]:
            armed = True
        if pb_len > max_pullback_bars:
            leg_high, in_pb, armed, count, pb_len, pb_low = high[i], False, False, 0, 0, np.inf
            continue
        rng = high[i] - low[i]
        close_pos = (close[i] - low[i]) / rng if rng > 0 else 0.0
        if count == 1 and armed and high[i] < high[i - 1] and trend_ok[i] and close_pos >= signal_close_pos:
            signal[i] = True
            pb_low_out[i] = pb_low
    return signal, low.copy(), pb_low_out


def htf_trend(bars: pd.DataFrame, cfg: Config) -> np.ndarray:
    """+1/-1/0 trend of the last *completed* higher-timeframe bar at each bar's close."""
    if cfg.htf is None:
        return np.zeros(len(bars))
    htf = bars.resample(cfg.htf, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    line = ema(htf["close"], cfg.htf_ema)
    up = (htf["close"] > line) & (line > line.shift(cfg.htf_slope_bars))
    down = (htf["close"] < line) & (line < line.shift(cfg.htf_slope_bars))
    state = pd.Series(np.where(up, 1, np.where(down, -1, 0)), index=htf.index + pd.Timedelta(cfg.htf))
    decision = bars.index + pd.Timedelta(cfg.timeframe)
    known = state.reindex(state.index.union(decision)).ffill().reindex(decision)
    return known.fillna(0).to_numpy()


def session_ok(decision_times: pd.DatetimeIndex, session: str) -> np.ndarray:
    hours = decision_times.hour
    rollover = (hours >= 21) & (hours < 23)  # CME break / rollover spreads
    if session == "london_ny":
        return (hours >= 7) & (hours < 20)
    return ~rollover


def generate_signals(bars: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Signals known at each completed bar's close. Prices are mid-price levels."""
    close = bars["close"]
    line = ema(close, cfg.ema)
    slope = (line - line.shift(cfg.slope_bars)).to_numpy()
    rng = atr(bars, cfg.atr_period).to_numpy()
    c, h, low = close.to_numpy(), bars["high"].to_numpy(), bars["low"].to_numpy()
    e = line.to_numpy()
    htf = htf_trend(bars, cfg)
    decision = bars.index + pd.Timedelta(cfg.timeframe)
    allowed = session_ok(decision, cfg.session)
    warm = np.arange(len(bars)) >= max(cfg.ema, cfg.atr_period) * 3

    frames = []
    for direction in (1, -1):
        # Reflect prices for shorts so one routine handles both sides.
        hh, ll, cc, ee = (h, low, c, e) if direction == 1 else (-low, -h, -c, -e)
        sl = slope * direction
        trend_ok = (sl > 0) & (cc > ee) & warm & allowed
        if cfg.htf is not None:
            trend_ok &= htf == direction
        sig, bar_low, pb_low = h2_long_setups(hh, ll, cc, trend_ok, cfg.max_pullback_bars, cfg.signal_close_pos)
        idx = np.flatnonzero(sig)
        entry = hh[idx] + cfg.tick
        stop_base = pb_low[idx] if cfg.stop_mode == "pullback" else bar_low[idx]
        stop = stop_base - cfg.tick
        risk = entry - stop
        ok = ((risk >= cfg.min_risk_atr * rng[idx]) & (risk <= cfg.max_risk_atr * rng[idx])
              & (risk >= cfg.min_risk_bps / 1e4 * c[idx]))
        idx, entry, stop, risk = idx[ok], entry[ok], stop[ok], risk[ok]
        frames.append(pd.DataFrame({
            "bar_time": bars.index[idx],
            "decision_time": decision[idx],
            "direction": direction,
            "entry": entry * direction,
            "stop": stop * direction,
            "target": (entry + cfg.rr * risk) * direction,
            "risk": risk,
            "atr": rng[idx],
        }))
    out = pd.concat(frames, ignore_index=True).sort_values("decision_time").reset_index(drop=True)
    return out
