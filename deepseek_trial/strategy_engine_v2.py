"""London opening-range breakout, then a retest. Fixed rules, one setup at a time.

The range is 08:00–08:30 London. A trade is allowed only if a later 5-minute
candle closes outside that range and, within the next three candles, price
touches the broken edge and closes back on the breakout side. The optional
hourly filter uses only the last finished hour.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np

from deepseek_trial.indicators import ema_seeded, wilder_atr
from deepseek_trial.research_types_v2 import (
    ATR_N,
    EMA_FAST,
    EMA_SLOW,
    ENTRY_END,
    ENTRY_START,
    LONDON,
    OR_END,
    OR_START,
    RETEST_BARS,
    STOP_ATR_MULT,
    Bar,
    SignalEvent,
    shift_time,
)


def previous_weekday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def london_daily_levels(bars: list[Bar]) -> dict[str, tuple[float, float]]:
    """High and low of each London date, from bars whose start falls on that date."""
    buckets: dict[str, list[Bar]] = {}
    for bar in bars:
        key = bar.start.astimezone(LONDON).date().isoformat()
        buckets.setdefault(key, []).append(bar)
    return {
        key: (max(b.high for b in rows), min(b.low for b in rows))
        for key, rows in buckets.items()
    }


def hourly_trend(h1: list[Bar], as_of: datetime) -> str:
    """UP, DOWN, FLAT, or UNKNOWN from hourly bars that have already closed."""
    closed = [bar for bar in h1 if bar.end <= as_of]
    if len(closed) < EMA_SLOW:
        return "UNKNOWN"
    closes = np.array([bar.close for bar in closed], dtype=float)
    ema_fast = ema_seeded(closes, EMA_FAST)
    ema_slow = ema_seeded(closes, EMA_SLOW)
    last, fast, slow = closes[-1], ema_fast[-1], ema_slow[-1]
    if not all(np.isfinite(x) for x in (last, fast, slow)):
        return "UNKNOWN"
    if last > slow and fast > slow:
        return "UP"
    if last < slow and fast < slow:
        return "DOWN"
    return "FLAT"


class OpeningRangeRetest:
    def __init__(
        self,
        variant: str = "orb_h1_trend",
        session_shift_minutes: int = 0,
        stop_atr_mult: float = STOP_ATR_MULT,
    ):
        if variant not in ("orb_only", "orb_h1_trend"):
            raise ValueError(variant)
        if abs(session_shift_minutes) > 30:
            raise ValueError("session shift must stay within 30 minutes")
        self.variant = variant
        self.shift = session_shift_minutes
        self.stop_atr_mult = stop_atr_mult
        self.consumed: set[str] = set()
        self.filled = False
        self._reset_day("")

    def _reset_day(self, day: str) -> None:
        self.day = day
        self.collected: list[time] = []
        self.highs: list[float] = []
        self.lows: list[float] = []
        self.frozen = False
        self.invalid = False
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.armed = False
        self.side = ""
        self.waited = 0
        self.active_id = ""
        self.filled = False

    def mark_filled(self) -> None:
        self.filled = True
        self.armed = False
        if self.active_id:
            self.consumed.add(self.active_id)

    def note_consumed(self, setup_id: str) -> None:
        self.consumed.add(setup_id)
        self.armed = False

    def _windows(self) -> tuple[time, time, time, time]:
        return (
            shift_time(OR_START, self.shift),
            shift_time(OR_END, self.shift),
            shift_time(ENTRY_START, self.shift),
            shift_time(ENTRY_END, self.shift),
        )

    def _expected_starts(self) -> set[time]:
        start, end, _, _ = self._windows()
        cursor = datetime(2000, 1, 3, start.hour, start.minute)
        stop = datetime(2000, 1, 3, end.hour, end.minute)
        found = set()
        while cursor < stop:
            found.add(cursor.time())
            cursor += timedelta(minutes=5)
        return found

    def on_bar(
        self,
        i: int,
        m5: list[Bar],
        h1: list[Bar],
        daily_levels: dict[str, tuple[float, float]],
    ) -> SignalEvent | None:
        bar = m5[i]
        local_start = bar.start.astimezone(LONDON)
        day = local_start.date().isoformat()
        if day != self.day:
            self._reset_day(day)
        if local_start.weekday() >= 5:
            return None

        or_start, or_end, _, entry_end = self._windows()
        clock = local_start.time()
        close_clock = bar.end.astimezone(LONDON).time()

        if or_start <= clock < or_end:
            self.collected.append(clock)
            self.highs.append(bar.high)
            self.lows.append(bar.low)
            return None

        if not self.frozen and clock >= or_end:
            self.frozen = True
            if set(self.collected) != self._expected_starts() or not self.highs:
                self.invalid = True
            else:
                self.range_high = max(self.highs)
                self.range_low = min(self.lows)
                if self.range_high <= self.range_low:
                    self.invalid = True

        if self.invalid or not self.frozen or self.range_high is None or self.range_low is None:
            return None

        if self.armed:
            return self._retest(i, m5, h1, daily_levels, close_clock, entry_end)

        if self.filled or close_clock < shift_time(ENTRY_START, self.shift) or close_clock >= entry_end:
            return None
        if bar.close > self.range_high:
            return self._arm("LONG", i, self.range_high)
        if bar.close < self.range_low:
            return self._arm("SHORT", i, self.range_low)
        return None

    def _arm(self, side: str, i: int, boundary: float) -> None:
        setup_id = f"{self.day}|{side}|{boundary:.5f}|{self.variant}"
        if setup_id in self.consumed:
            return None
        self.armed = True
        self.side = side
        self.waited = 0
        self.active_id = setup_id
        return None

    def _event(self, i: int, m5: list[Bar], status: str, reason: str, stop: float, atr: float, high, low) -> SignalEvent:
        return SignalEvent(
            setup_id=self.active_id,
            status=status,
            reason=reason,
            side=self.side,
            stop=stop,
            boundary=self.range_high if self.side == "LONG" else self.range_low,
            retest_extreme=m5[i].low if self.side == "LONG" else m5[i].high,
            atr=atr,
            opposing_high=None if high is None else high,
            opposing_low=None if low is None else low,
            signal_time=m5[i].end,
            london_date=self.day,
            variant=self.variant,
            bar_index=i,
        )

    def _finish(self, event: SignalEvent) -> SignalEvent:
        self.consumed.add(event.setup_id)
        self.armed = False
        self.active_id = ""
        return event

    def _retest(self, i, m5, h1, daily_levels, close_clock, entry_end) -> SignalEvent | None:
        self.waited += 1
        bar = m5[i]
        if self.waited > RETEST_BARS or close_clock >= entry_end:
            event = self._event(i, m5, "EXPIRED", "SETUP_EXPIRED", 0.0, float("nan"), None, None)
            return self._finish(event)

        boundary = self.range_high if self.side == "LONG" else self.range_low
        if self.side == "LONG":
            touched = bar.low <= boundary and bar.close > boundary
        else:
            touched = bar.high >= boundary and bar.close < boundary
        if not touched:
            return None

        atr = self._atr(m5, i)
        if atr != atr or atr <= 0:
            event = self._event(i, m5, "REJECT", "DATA_INVALID", 0.0, atr, None, None)
            return self._finish(event)
        extreme = bar.low if self.side == "LONG" else bar.high
        buffer = self.stop_atr_mult * atr
        stop = extreme - buffer if self.side == "LONG" else extreme + buffer
        prev = daily_levels.get(previous_weekday(date.fromisoformat(self.day)).isoformat())
        high = low = None
        if prev is not None:
            high, low = prev
        if self.variant == "orb_h1_trend":
            trend = hourly_trend(h1, bar.end)
            needed = "UP" if self.side == "LONG" else "DOWN"
            if trend != needed:
                reason = "DATA_INVALID" if trend == "UNKNOWN" else "TREND_FILTER"
                event = self._event(i, m5, "REJECT", reason, stop, atr, high, low)
                return self._finish(event)
        if high is None or low is None:
            event = self._event(i, m5, "REJECT", "PRIOR_LEVEL_UNKNOWN", stop, atr, high, low)
            return self._finish(event)
        event = self._event(i, m5, "READY", "", stop, atr, high, low)
        # Consumed now so a later cycle cannot rebuild the same setup.
        # The caller still holds this object long enough to price the next quote.
        return self._finish(event)

    def _atr(self, m5: list[Bar], i: int) -> float:
        if i + 1 < ATR_N:
            return float("nan")
        window = m5[: i + 1]
        high = np.array([bar.high for bar in window], dtype=float)
        low = np.array([bar.low for bar in window], dtype=float)
        close = np.array([bar.close for bar in window], dtype=float)
        series = wilder_atr(high, low, close, ATR_N)
        return float(series[i])
