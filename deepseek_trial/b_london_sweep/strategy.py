"""Strategy B: London overnight sweep, reclaim, return toward range. GOLD-OCT26-v1."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np

from deepseek_trial.calendar_rules import iso, london_b
from deepseek_trial.candidate import Candidate
from deepseek_trial.indicators import atr_prior, h4_direction

STRATEGY_ID = "B"
LONDON = ZoneInfo("Europe/London")
HOLD_HOURS = 2


@dataclass
class DayRange:
    date: str
    h: float
    l: float
    m: float
    w: float
    valid: bool


@dataclass
class BState:
    ranges: list[DayRange] = field(default_factory=list)
    today: str = ""
    range: DayRange | None = None
    disabled: bool = False
    frozen: bool = False
    armed: bool = False
    side: str = ""
    k: float = 0.0
    exc_i: int = -1
    waited: int = 0
    tried_low: bool = False
    tried_high: bool = False
    filled: bool = False
    collecting: list = field(default_factory=list)


def london_date(ts: datetime) -> str:
    return ts.astimezone(LONDON).date().isoformat()


class StrategyB:
    def __init__(self):
        self.s = BState()
        self.n_arm = 0
        self.n_confirm = 0
        self.n_cancel = 0
        self.n_frozen = 0
        self.n_width_fail = 0
        self.n_hist_short = 0

    def invalidate(self) -> None:
        if self.s.armed:
            self.s.armed = False
            self.n_cancel += 1

    def on_m5(
        self,
        i: int,
        ts: datetime,
        bar_start: datetime,
        o, h, l, c,
        h4_close, h4_e20, h4_e50, h4_t: int,
        blackout: bool,
    ) -> Candidate | None:
        loc_start = bar_start.astimezone(LONDON)
        dkey = loc_start.date().isoformat()
        if dkey != self.s.today:
            self._roll_day(dkey)

        # Overnight reference [00:00, 08:00) London by bar *start* (spec interval).
        if time(0, 0) <= loc_start.time() < time(8, 0) and loc_start.weekday() < 5:
            self.s.collecting.append((float(h[i]), float(l[i])))
        if (
            loc_start.weekday() < 5
            and time(8, 0) <= loc_start.time() < time(8, 30)
            and not self.s.frozen
        ):
            self.s.frozen = True
            self._freeze_range()

        if self.s.disabled or self.s.filled or self.s.range is None:
            return None
        if not london_b(ts) or blackout:
            self.invalidate()
            return None

        h4 = h4_direction(h4_close, h4_e20, h4_e50, h4_t)
        rng = self.s.range
        a5 = atr_prior(h, l, c, i, 14)
        cand = None

        if self.s.armed:
            self.s.waited = i - self.s.exc_i
            if self.s.side == "BUY" and h4 == "DOWN":
                self.invalidate()
            elif self.s.side == "SELL" and h4 == "UP":
                self.invalidate()
            elif self.s.waited > 3:
                self.invalidate()
            else:
                if self.s.side == "BUY" and c[i] > rng.l and c[i] > self.s.k:
                    cand = self._confirm("BUY", i, ts, h, l, c, h4)
                elif self.s.side == "SELL" and c[i] < rng.h and c[i] < self.s.k:
                    cand = self._confirm("SELL", i, ts, h, l, c, h4)

        if not self.s.armed and np.isfinite(a5) and a5 > 0 and i >= 5:
            beyond_both = l[i] < rng.l and h[i] > rng.h
            if beyond_both:
                return cand
            long_exc = (not self.s.tried_low) and h4 in ("UP", "NEUTRAL") and (rng.l - l[i]) >= 0.05 * a5
            short_exc = (not self.s.tried_high) and h4 in ("DOWN", "NEUTRAL") and (h[i] - rng.h) >= 0.05 * a5
            if long_exc:
                self.s.armed = True
                self.s.side = "BUY"
                self.s.exc_i = i
                self.s.waited = 0
                self.s.k = float(np.max(h[i - 5:i]))
                self.s.tried_low = True
                self.n_arm += 1
                if c[i] > rng.l and c[i] > self.s.k:
                    cand = self._confirm("BUY", i, ts, h, l, c, h4)
            elif short_exc:
                self.s.armed = True
                self.s.side = "SELL"
                self.s.exc_i = i
                self.s.waited = 0
                self.s.k = float(np.min(l[i - 5:i]))
                self.s.tried_high = True
                self.n_arm += 1
                if c[i] < rng.h and c[i] < self.s.k:
                    cand = self._confirm("SELL", i, ts, h, l, c, h4)
        return cand

    def _roll_day(self, dkey: str) -> None:
        self.s.today = dkey
        self.s.range = None
        self.s.disabled = False
        self.s.frozen = False
        self.s.armed = False
        self.s.tried_low = False
        self.s.tried_high = False
        self.s.filled = False
        self.s.collecting = []

    def _freeze_range(self) -> None:
        self.n_frozen += 1
        if not self.s.collecting:
            self.s.disabled = True
            return
        hh = max(x[0] for x in self.s.collecting)
        ll = min(x[1] for x in self.s.collecting)
        w = hh - ll
        if w <= 0:
            self.s.disabled = True
            return
        rng = DayRange(self.s.today, hh, ll, (hh + ll) / 2, w, True)
        self.s.ranges.append(rng)
        prior = [r.w for r in self.s.ranges[:-1][-20:]]
        if len(prior) < 20:
            self.s.disabled = True
            self.s.range = None
            self.n_hist_short += 1
            return
        med = float(np.median(prior))
        if not (0.50 * med <= w <= 1.50 * med):
            self.s.disabled = True
            self.s.range = None
            self.n_width_fail += 1
            return
        self.s.range = rng

    def _confirm(self, side: str, i: int, ts: datetime, h, l, c, h4: str) -> Candidate | None:
        if self.s.filled:
            self.s.armed = False
            return None
        a5 = atr_prior(h, l, c, i, 14)
        sl = self.s.exc_i
        if side == "BUY":
            swing = float(np.min(l[sl:i + 1]))
            stop = swing - 0.20 * a5
        else:
            swing = float(np.max(h[sl:i + 1]))
            stop = swing + 0.20 * a5
        self.s.armed = False
        self.n_confirm += 1
        rng = self.s.range
        return Candidate(
            candidate_id=f"B-{side}-{iso(ts)}",
            strategy="B", side=side, confirm_time=ts, confirm_m5=i,
            structural_stop=stop, atr=a5, target_r=2.0, min_rr=1.50,
            reason="B_CONFIRM", h4_dir=h4,
            extras={"M": rng.m if rng else None, "H": rng.h if rng else None, "L": rng.l if rng else None, "K": self.s.k, "h4": h4},
        )

    def commit_fill(self) -> None:
        self.s.filled = True

    def mark_unfilled(self) -> None:
        self.s.filled = False
