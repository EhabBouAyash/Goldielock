"""Closed-bar features for the fixed rules.

An old saved snapshot can still be shown so you can see what the chart looked
like. It cannot approve a new trade. There is no 50/20/12/10/8 score here.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from deepseek_trial.indicators import wilder_atr
from deepseek_trial.research_types_v2 import ATR_N, Bar, MarketData
from deepseek_trial.strategy_engine_v2 import hourly_trend


def closed_only(bars: list[Bar], now: datetime) -> list[Bar]:
    return [bar for bar in bars if bar.end <= now]


def prepare(
    m5: list[Bar],
    h1: list[Bar],
    now: datetime,
    source: str,
    using_snapshot_fallback: bool = False,
) -> MarketData:
    m5_closed = closed_only(m5, now)
    h1_closed = closed_only(h1, now)
    if using_snapshot_fallback:
        return MarketData(
            source="snapshot_fallback",
            tradable=False,
            reason="DATA_INVALID",
            m5=m5_closed,
            h1=h1_closed,
            as_of=now,
            display_only=True,
        )
    if source != "mt5":
        return MarketData(
            source=source or "unknown",
            tradable=False,
            reason="DATA_INVALID",
            m5=m5_closed,
            h1=h1_closed,
            as_of=now,
            display_only=True,
        )
    if any(not bar.ok() for bar in m5_closed + h1_closed):
        return MarketData(
            source="mt5",
            tradable=False,
            reason="DATA_INVALID",
            m5=[],
            h1=[],
            as_of=now,
        )
    return MarketData(
        source="mt5",
        tradable=True,
        reason="",
        m5=m5_closed,
        h1=h1_closed,
        as_of=now,
    )


def deterministic_features(m5: list[Bar], h1: list[Bar], as_of: datetime) -> dict:
    """Numbers the rules can see. Not a weighted vote across timeframes."""
    atr = None
    if len(m5) >= ATR_N:
        series = wilder_atr(
            np.array([bar.high for bar in m5], dtype=float),
            np.array([bar.low for bar in m5], dtype=float),
            np.array([bar.close for bar in m5], dtype=float),
            ATR_N,
        )
        last = float(series[-1])
        atr = None if last != last else last
    return {
        "m5_atr14": atr,
        "h1_trend": hourly_trend(h1, as_of),
        "weights_used": None,
        "plain": "These are inputs to fixed rules. They are not a 50/20/12/10/8 score.",
    }
