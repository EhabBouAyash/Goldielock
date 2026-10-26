"""Strategy A: H4 trend, M15 pullback and reclaim. GOLD-OCT26-v1."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from deepseek_trial.calendar_rules import iso, london_a, ny_a
from deepseek_trial.candidate import Candidate
from deepseek_trial.indicators import atr_prior, h4_direction

STRATEGY_ID = "A"
HOLD_HOURS = 6
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")


def _window_id(ts: datetime) -> str | None:
    if london_a(ts):
        d = ts.astimezone(LONDON).date().isoformat()
        return f"{d}-LON"
    if ny_a(ts):
        d = ts.astimezone(NEW_YORK).date().isoformat()
        return f"{d}-NY"
    return None


@dataclass
class AState:
    armed: bool = False
    side: str = ""
    arm_i: int = -1
    waited: int = 0
    day: str = ""
    fills_today: int = 0
    filled_windows: set[str] = field(default_factory=set)
    need_fresh: bool = False
    prev_close: float | None = None
    prev_ema20: float | None = None


def day_key(ts: datetime) -> str:
    return ts.astimezone(NEW_YORK).date().isoformat()


class StrategyA:
    def __init__(self):
        self.s = AState()
        self.n_arm = 0
        self.n_confirm = 0
        self.n_cancel = 0

    def invalidate(self) -> None:
        if self.s.armed:
            self.s.armed = False
            self.s.need_fresh = True
            self.n_cancel += 1

    def on_m15(
        self,
        i: int,
        ts: datetime,
        o, h, l, c, ema20, ema50,
        h4_close, h4_e20, h4_e50, h4_t: int,
        blackout: bool,
    ) -> Candidate | None:
        if day_key(ts) != self.s.day:
            self.s.day = day_key(ts)
            self.s.fills_today = 0
        wid = _window_id(ts)
        h4 = h4_direction(h4_close, h4_e20, h4_e50, h4_t)

        if self.s.armed and (
            wid is None
            or blackout
            or (self.s.side == "BUY" and h4 != "UP")
            or (self.s.side == "SELL" and h4 != "DOWN")
        ):
            self.invalidate()

        cand = None
        if self.s.armed and wid and not blackout and i > self.s.arm_i:
            self.s.waited = i - self.s.arm_i
            if self.s.waited > 4:
                self.invalidate()
            elif self.s.side == "BUY" and c[i] <= ema50[i]:
                self.invalidate()
            elif self.s.side == "SELL" and c[i] >= ema50[i]:
                self.invalidate()
            elif self.s.waited <= 4:
                prev_high, prev_low = h[i - 1], l[i - 1]
                if self.s.side == "BUY" and c[i] > ema20[i] and c[i] > prev_high:
                    cand = self._confirm("BUY", i, ts, h, l, c, h4)
                elif self.s.side == "SELL" and c[i] < ema20[i] and c[i] < prev_low:
                    cand = self._confirm("SELL", i, ts, h, l, c, h4)

        if not self.s.armed and wid and not blackout and i >= 1 and self.s.fills_today < 2:
            if wid not in self.s.filled_windows:
                pc, pe = self.s.prev_close, self.s.prev_ema20
                if pc is not None and pe is not None and np.isfinite(ema20[i]) and np.isfinite(ema50[i]):
                    long_arm = (
                        h4 == "UP" and pc > pe and c[i] <= ema20[i] and c[i] > ema50[i]
                    )
                    short_arm = (
                        h4 == "DOWN" and pc < pe and c[i] >= ema20[i] and c[i] < ema50[i]
                    )
                    # Fresh above-to-below (or inverse) transition is the arm itself.
                    if long_arm or short_arm:
                        self.s.armed = True
                        self.s.side = "BUY" if long_arm else "SELL"
                        self.s.arm_i = i
                        self.s.waited = 0
                        self.s.need_fresh = False
                        self.n_arm += 1

        self.s.prev_close = float(c[i]) if np.isfinite(c[i]) else None
        self.s.prev_ema20 = float(ema20[i]) if np.isfinite(ema20[i]) else None
        return cand

    def _confirm(self, side: str, i: int, ts: datetime, h, l, c, h4: str) -> Candidate | None:
        wid = _window_id(ts)
        if wid is None or wid in self.s.filled_windows:
            self.invalidate()
            return None
        sl = int(self.s.arm_i)
        if side == "BUY":
            swing = float(np.min(l[sl:i + 1]))
        else:
            swing = float(np.max(h[sl:i + 1]))
        a15 = atr_prior(h, l, c, i, 14)
        if not np.isfinite(a15) or a15 <= 0:
            self.invalidate()
            return None
        stop = swing - 0.20 * a15 if side == "BUY" else swing + 0.20 * a15
        cid = f"A-{side}-{iso(ts)}"
        extras = {"arm_i": sl, "window": wid, "swing": swing, "h4": h4}
        self.s.armed = False
        self.s.need_fresh = True
        self.n_confirm += 1
        return Candidate(
            candidate_id=cid, strategy="A", side=side, confirm_time=ts,
            confirm_m5=-1, structural_stop=stop, atr=a15, target_r=2.50, min_rr=0.0,
            reason="A_CONFIRM", extras=extras, h4_dir=h4,
        )

    def commit_fill(self, wid: str) -> None:
        if wid:
            self.s.filled_windows.add(wid)
        self.s.fills_today += 1
