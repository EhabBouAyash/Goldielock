"""Strategy C: NY 09:30–09:45 range break, acceptance, retest. GOLD-OCT26-v1."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np

from deepseek_trial.calendar_rules import iso, ny_c_breakout_deadline, ny_c_entry, reference_overlaps_blackout
from deepseek_trial.candidate import Candidate
from deepseek_trial.indicators import atr_prior, h4_direction

STRATEGY_ID = "C"
NY = ZoneInfo("America/New_York")
HOLD_HOURS = 3


@dataclass
class CRange:
    date: str
    h: float
    l: float
    w: float
    valid: bool
    start: datetime
    end: datetime


@dataclass
class CState:
    ranges: list[CRange] = field(default_factory=list)
    today: str = ""
    range: CRange | None = None
    disabled: bool = False
    frozen: bool = False
    collecting: list = field(default_factory=list)
    collecting_start: datetime | None = None
    armed: bool = False
    side: str = ""
    brk_i: int = -1
    ab: float = 0.0
    waited: int = 0
    tried_long: bool = False
    tried_short: bool = False
    filled: bool = False


def ny_date(ts: datetime) -> str:
    return ts.astimezone(NY).date().isoformat()


class StrategyC:
    def __init__(self):
        self.s = CState()
        self.n_arm = 0
        self.n_confirm = 0
        self.n_cancel = 0
        self.n_frozen = 0
        self.n_width_fail = 0
        self.n_hist_short = 0
        self.n_ref_blackout = 0

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
        events,
    ) -> Candidate | None:
        loc_start = bar_start.astimezone(NY)
        dkey = loc_start.date().isoformat()
        if dkey != self.s.today:
            self._roll_day(dkey)

        # Reference [09:30, 09:45) NY by bar *start* — three M5 candles.
        if time(9, 30) <= loc_start.time() < time(9, 45):
            if self.s.collecting_start is None:
                self.s.collecting_start = bar_start
            self.s.collecting.append((float(h[i]), float(l[i]), bar_start))
        if time(9, 45) <= loc_start.time() < time(10, 0) and not self.s.frozen:
            self.s.frozen = True
            self._freeze(events)

        if self.s.disabled or self.s.filled or self.s.range is None:
            return None
        if not ny_c_entry(ts) or blackout:
            self.invalidate()
            return None

        rng = self.s.range
        h4 = h4_direction(h4_close, h4_e20, h4_e50, h4_t)
        cand = None

        if self.s.armed:
            self.s.waited = i - self.s.brk_i
            if self.s.waited > 6:
                self.invalidate()
            else:
                ab = self.s.ab
                if self.s.side == "BUY":
                    if c[i] < rng.h - 0.25 * ab:
                        self.invalidate()
                    elif self.s.waited >= 1:
                        low_ok = (rng.h - 0.25 * ab) <= l[i] <= (rng.h + 0.20 * ab)
                        if low_ok and c[i] > rng.h:
                            cand = self._confirm("BUY", i, ts, h, l, c, h4)
                else:
                    if c[i] > rng.l + 0.25 * ab:
                        self.invalidate()
                    elif self.s.waited >= 1:
                        high_ok = (rng.l - 0.20 * ab) <= h[i] <= (rng.l + 0.25 * ab)
                        if high_ok and c[i] < rng.l:
                            cand = self._confirm("SELL", i, ts, h, l, c, h4)

        if not self.s.armed and ny_c_breakout_deadline(ts) and i >= 1:
            a5 = atr_prior(h, l, c, i, 14)
            rng_hl = h[i] - l[i]
            if np.isfinite(a5) and a5 > 0 and rng_hl > 0:
                body = abs(c[i] - o[i])
                bull = body >= 0.60 * rng_hl and c[i] > o[i]
                bear = body >= 0.60 * rng_hl and c[i] < o[i]
                if (not self.s.tried_long) and bull and c[i] > rng.h + 0.10 * a5:
                    self.s.armed = True
                    self.s.side = "BUY"
                    self.s.brk_i = i
                    self.s.ab = a5
                    self.s.waited = 0
                    self.s.tried_long = True
                    self.n_arm += 1
                elif (not self.s.tried_short) and bear and c[i] < rng.l - 0.10 * a5:
                    self.s.armed = True
                    self.s.side = "SELL"
                    self.s.brk_i = i
                    self.s.ab = a5
                    self.s.waited = 0
                    self.s.tried_short = True
                    self.n_arm += 1
        return cand

    def _roll_day(self, dkey: str) -> None:
        self.s.today = dkey
        self.s.range = None
        self.s.disabled = False
        self.s.frozen = False
        self.s.collecting = []
        self.s.collecting_start = None
        self.s.armed = False
        self.s.tried_long = False
        self.s.tried_short = False
        self.s.filled = False

    def _freeze(self, events) -> None:
        self.n_frozen += 1
        if len(self.s.collecting) < 3:
            self.s.disabled = True
            return
        hh = max(x[0] for x in self.s.collecting)
        ll = min(x[1] for x in self.s.collecting)
        w = hh - ll
        start = self.s.collecting[0][2]
        end = self.s.collecting[-1][2]
        if events is not None and reference_overlaps_blackout(start, end, events):
            self.s.disabled = True
            self.n_ref_blackout += 1
            return
        self.s.ranges.append(CRange(self.s.today, hh, ll, w, True, start, end))
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
        self.s.range = self.s.ranges[-1]

    def _confirm(self, side, i, ts, h, l, c, h4) -> Candidate:
        a_retest = atr_prior(h, l, c, i, 14)
        sl = self.s.brk_i
        if side == "BUY":
            swing = float(np.min(l[sl:i + 1]))
            stop = swing - 0.20 * a_retest
        else:
            swing = float(np.max(h[sl:i + 1]))
            stop = swing + 0.20 * a_retest
        self.s.armed = False
        self.n_confirm += 1
        rng = self.s.range
        return Candidate(
            candidate_id=f"C-{side}-{iso(ts)}",
            strategy="C", side=side, confirm_time=ts, confirm_m5=i,
            structural_stop=stop, atr=a_retest, target_r=2.50, min_rr=0.0,
            reason="C_CONFIRM", h4_dir=h4,
            extras={"H": rng.h if rng else None, "L": rng.l if rng else None, "Ab": self.s.ab, "h4": h4},
        )

    def commit_fill(self) -> None:
        self.s.filled = True

    def mark_unfilled(self) -> None:
        self.s.filled = False
