"""UTC M5/M15/H4 bars from midpoint quotes. H4 opens at 00/04/08/12/16/20 UTC."""
from __future__ import annotations

import pandas as pd


def ensure_utc(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = pd.to_datetime(out.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    out.index = idx
    return out.sort_index()


def resample_ohlc(df: pd.DataFrame, rule: str, origin: str = "epoch") -> pd.DataFrame:
    x = ensure_utc(df)[["open", "high", "low", "close"]]
    out = x.resample(rule, label="left", closed="left", origin=origin).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return out.dropna()


def m15_from_m5(m5: pd.DataFrame) -> pd.DataFrame:
    return resample_ohlc(m5, "15min")


def h4_from_m5(m5: pd.DataFrame) -> pd.DataFrame:
    """H4 bars begin 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC."""
    return resample_ohlc(m5, "4h", origin="epoch")


def last_completed_index(index: pd.DatetimeIndex, now: pd.Timestamp) -> int | None:
    """Largest i such that bar i has fully closed by `now` (bar end = start + duration)."""
    if len(index) < 2:
        return None if len(index) == 0 else 0
    duration = index[1] - index[0]
    closed = index + duration
    ok = closed <= now
    if not ok.any():
        return None
    return int(ok.to_numpy().nonzero()[0][-1])
