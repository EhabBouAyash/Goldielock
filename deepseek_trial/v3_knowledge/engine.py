"""v3 knowledge replay: DeepSeek proposes trades; 5 fills/day; software owns risk."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from deepseek_trial.bars import h4_from_m5, m15_from_m5
from deepseek_trial.calendar_rules import iso, load_calendar, london_a, london_b, merged_blackout, ny_a, ny_c_entry
from deepseek_trial.candidate import Candidate
from deepseek_trial.config import DAILY_STOP, H4_WARMUP, STARTING_BALANCE, TRIAL_DRAWDOWN
from deepseek_trial.data import merge_h4
from deepseek_trial.engine import Book, _close, _exit_px, _manage, _try_fill, save_books, summarize
from deepseek_trial.indicators import atr_prior, ema_seeded, h4_direction, true_range, wilder_atr
from deepseek_trial.v3_knowledge.auditor import propose

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")
V3_SPEC = "GOLD-OCT26-v3-knowledge"
MAX_FILLS_PER_DAY = 5
MAX_LOOKS_PER_DAY = 8


def _utc(ts) -> datetime:
    dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _pack(df, i, n):
    sl = max(0, i + 1 - n)
    rows = []
    for k in range(sl, i + 1):
        rows.append({
            "ts": str(df.index[k]),
            "o": round(float(df.open.iloc[k]), 3),
            "h": round(float(df.high.iloc[k]), 3),
            "l": round(float(df.low.iloc[k]), 3),
            "c": round(float(df.close.iloc[k]), 3),
        })
    return rows


def _snapshot(strategy, ts, side_hint, m5, m15, h4, i5, i15, i4, e20, e50, h4_dir, extras, fills, looks):
    return {
        "version": V3_SPEC,
        "strategy": strategy,
        "now": iso(ts),
        "fills_today": fills,
        "looks_today": looks,
        "max_fills_per_day": MAX_FILLS_PER_DAY,
        "h4_dir": h4_dir,
        "h4": {
            "close": float(h4.close.iloc[i4]) if i4 >= 0 else None,
            "ema20": float(e20[i4]) if i4 >= 0 and np.isfinite(e20[i4]) else None,
            "ema50": float(e50[i4]) if i4 >= 0 and np.isfinite(e50[i4]) else None,
        },
        "m5": _pack(m5, i5, 12),
        "m15": _pack(m15, i15, 8),
        "levels": extras,
    }


def replay_v3(
    m5: pd.DataFrame,
    *,
    audit: bool,
    source: str,
    strategy_filter: str | None = None,
    h4_hist: pd.DataFrame | None = None,
    h4_source: str = "",
) -> dict:
    events, cal_err = load_calendar()
    m15 = m15_from_m5(m5)
    h4_live = h4_from_m5(m5)
    h4 = merge_h4(h4_hist if h4_hist is not None else pd.DataFrame(), h4_live)
    e20_h4 = ema_seeded(h4.close.to_numpy(), 20)
    e50_h4 = ema_seeded(h4.close.to_numpy(), 50)
    h4c = h4.close.to_numpy()
    m5o, m5h, m5l, m5c = (m5[x].to_numpy() for x in ("open", "high", "low", "close"))
    tr5 = true_range(m5h, m5l, m5c)
    atr5 = wilder_atr(m5h, m5l, m5c, 14)
    m15_end = m15.index + pd.Timedelta(minutes=15)
    h4_end = h4.index + pd.Timedelta(hours=4)
    m5_end = m5.index + pd.Timedelta(minutes=5)

    names = []
    for s in ("A", "B", "C"):
        if strategy_filter and s != strategy_filter:
            continue
        names.append(f"{s}_v3")
    books = {n: Book(n) for n in names}

    last_m15 = -1
    looks = {s: 0 for s in "ABC"}
    fills = {s: 0 for s in "ABC"}
    look_day = {s: "" for s in "ABC"}
    pause_until = None
    overnight: list = []
    overnight_day = ""
    frozen_b = None
    n_looks = 0
    n_api = 0

    def completed(ends, now) -> int:
        ok = np.asarray(ends <= now)
        idx = np.flatnonzero(ok)
        return int(idx[-1]) if idx.size else -1

    for i in range(len(m5) - 1):
        now = _utc(m5_end[i])
        bar_start = _utc(m5.index[i])
        dkey = now.date().isoformat()
        i4 = completed(h4_end, m5_end[i])
        i15 = completed(m15_end, m5_end[i])
        if i4 < H4_WARMUP - 1:
            continue
        blackout = bool(cal_err) or merged_blackout(now, events)
        if i > 0 and np.isfinite(atr5[i - 1]) and tr5[i] > 3 * atr5[i - 1]:
            pause_until = now + timedelta(minutes=15)
        paused = pause_until is not None and now < pause_until
        blocked = blackout or paused

        loc = bar_start.astimezone(LONDON)
        ld = loc.date().isoformat()
        if ld != overnight_day:
            overnight_day = ld
            overnight = []
            frozen_b = None
        if time(0, 0) <= loc.time() < time(8, 0) and loc.weekday() < 5:
            overnight.append((float(m5h[i]), float(m5l[i])))
        if loc.weekday() < 5 and time(8, 0) <= loc.time() < time(8, 10) and frozen_b is None:
            if overnight:
                hh, ll = max(x[0] for x in overnight), min(x[1] for x in overnight)
                frozen_b = {"H": hh, "L": ll, "M": (hh + ll) / 2}

        for book in books.values():
            if book.day != dkey:
                book.day = dkey
                book.daily_pnl = 0.0
                book.kill = False
            sid = book.name[0]
            if look_day[sid] != dkey:
                look_day[sid] = dkey
                looks[sid] = 0
                fills[sid] = 0
            if book.equity <= STARTING_BALANCE - TRIAL_DRAWDOWN or book.peak - book.equity >= TRIAL_DRAWDOWN:
                book.kill = True
            if book.daily_pnl <= -DAILY_STOP:
                book.kill = True
            if book.kill and book.position is not None:
                _close(book, book.position, now, _exit_px(book.position.side, float(m5c[i]), False, book.position.stop, float(m5o[i])), "KILL")
                continue
            _manage(book, now, float(m5o[i]), float(m5h[i]), float(m5l[i]), float(m5c[i]), events)
            if book.position is None and book.day == dkey:
                pass

        if blocked:
            continue
        h4_dir = h4_direction(h4c, e20_h4, e50_h4, i4)
        a5 = atr_prior(m5h, m5l, m5c, i, 14)
        if not np.isfinite(a5) or a5 <= 0:
            continue

        def try_strategy(sid: str, eligible: bool, extras: dict) -> None:
            nonlocal n_looks, n_api
            book_name = f"{sid}_v3"
            if book_name not in books or not eligible:
                return
            book = books[book_name]
            if book.kill or book.position is not None:
                return
            if fills[sid] >= MAX_FILLS_PER_DAY or looks[sid] >= MAX_LOOKS_PER_DAY:
                return
            looks[sid] += 1
            n_looks += 1
            snap = _snapshot(sid, now, "", m5, m15, h4, i, i15, i4, e20_h4, e50_h4, h4_dir, extras, fills[sid], looks[sid])
            if not audit:
                book.funnel.append({"id": iso(now), "result": "LOOK_NO_API"})
                return
            n_api += 1
            prop, meta = propose(sid, snap)
            if prop is None:
                book.funnel.append({"id": iso(now), "result": "SKIP", "meta": {k: meta.get(k) for k in ("error", "decision")}})
                return
            close = float(m5c[i])
            if prop["side"] == "BUY":
                stop = close - prop["stop_atr"] * a5
            else:
                stop = close + prop["stop_atr"] * a5
            cand = Candidate(
                candidate_id=f"V3-{sid}-{prop['side']}-{iso(now)}",
                strategy=sid,
                side=prop["side"],
                confirm_time=now,
                confirm_m5=i,
                structural_stop=stop,
                atr=a5,
                target_r=prop["target_r"],
                min_rr=0.0,
                reason="V3_KNOWLEDGE",
                extras={**extras, "v3": True},
                h4_dir=h4_dir,
            )
            fill_mid = float(m5o[i + 1])
            fill_ts = _utc(m5.index[i + 1])
            why = _try_fill(book, cand, fill_mid, close, fill_ts)
            book.funnel.append({"id": cand.candidate_id, "result": why, "ai": meta})
            if why == "FILLED":
                fills[sid] += 1

        m15_close = i15 > last_m15 and i15 >= 1
        if m15_close:
            last_m15 = i15
        if not strategy_filter or strategy_filter == "A":
            try_strategy("A", m15_close and (london_a(now) or ny_a(now)), {})
        if not strategy_filter or strategy_filter == "B":
            try_strategy("B", london_b(now), frozen_b or {})
        if not strategy_filter or strategy_filter == "C":
            try_strategy("C", ny_c_entry(now), {})

    for book in books.values():
        if book.position is not None:
            last = _utc(m5_end[-1])
            _close(book, book.position, last, _exit_px(book.position.side, float(m5c[-1]), False, book.position.stop, float(m5o[-1])), "END")

    return {
        "source": source,
        "h4_source": h4_source,
        "spec": V3_SPEC,
        "books": books,
        "diagnostics": {
            "looks": n_looks,
            "api_calls": n_api,
            "max_fills_per_day": MAX_FILLS_PER_DAY,
            "max_looks_per_day": MAX_LOOKS_PER_DAY,
            "audit": audit,
        },
    }
