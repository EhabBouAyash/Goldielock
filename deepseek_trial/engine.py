"""GOLD-OCT26-v1 paper engine: six books + combined mechanical portfolio."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from deepseek_trial.a_h4_m15_pullback.strategy import HOLD_HOURS as A_HOLD
from deepseek_trial.a_h4_m15_pullback.strategy import StrategyA
from deepseek_trial.auditor import decide
from deepseek_trial.b_london_sweep.strategy import HOLD_HOURS as B_HOLD
from deepseek_trial.b_london_sweep.strategy import StrategyB
from deepseek_trial.bars import h4_from_m5, m15_from_m5
from deepseek_trial.data import merge_h4
from deepseek_trial.c_ny_range_retest.strategy import HOLD_HOURS as C_HOLD
from deepseek_trial.c_ny_range_retest.strategy import StrategyC
from deepseek_trial.calendar_rules import (
    iso,
    load_calendar,
    london_1200,
    merged_blackout,
    news_flatten,
    ny_1545,
)
from deepseek_trial.candidate import Candidate
from deepseek_trial.config import (
    CHASE_ATR_FRAC,
    DAILY_STOP,
    H4_WARMUP,
    RISK_PER_TRADE,
    SPEC_VERSION,
    STARTING_BALANCE,
    TRIAL_DRAWDOWN,
    XAUUSD,
)
from deepseek_trial.indicators import ema_seeded, true_range, wilder_atr
from deepseek_trial.risk import apply_stop_band, size_trade, slippage_per_fill, spread_price

UTC = timezone.utc


@dataclass
class Position:
    strategy: str
    side: str
    lots: float
    entry: float
    stop: float
    target: float
    r_cash: float
    opened: datetime
    deadline: datetime
    candidate_id: str


@dataclass
class Book:
    name: str
    equity: float = STARTING_BALANCE
    peak: float = STARTING_BALANCE
    daily_pnl: float = 0.0
    day: str = ""
    kill: bool = False
    position: Position | None = None
    trades: list = field(default_factory=list)
    funnel: list = field(default_factory=list)
    vetoes: list = field(default_factory=list)


def _mid_ask_bid(mid: float):
    spr = spread_price(mid, XAUUSD)
    return mid + spr, mid  # ask, bid


def _entry_px(side: str, mid: float) -> float:
    ask, bid = _mid_ask_bid(mid)
    slip = slippage_per_fill(mid, XAUUSD)
    return ask + slip if side == "BUY" else bid - slip


def _exit_px(side: str, mid: float, is_stop: bool, stop: float, opened: float) -> float:
    ask, bid = _mid_ask_bid(mid)
    slip = slippage_per_fill(mid, XAUUSD)
    if is_stop:
        gap = (side == "BUY" and opened <= stop) or (side == "SELL" and opened >= stop)
        raw = opened if gap else stop
        return raw - slip if side == "BUY" else raw + slip
    return bid - slip if side == "BUY" else ask + slip


def _close(book: Book, pos: Position, ts: datetime, px: float, reason: str) -> None:
    sign = 1.0 if pos.side == "BUY" else -1.0
    pnl = sign * XAUUSD.contract_size * pos.lots * (px - pos.entry)
    book.equity += pnl
    book.daily_pnl += pnl
    book.peak = max(book.peak, book.equity)
    r = pnl / pos.r_cash if pos.r_cash else 0.0
    book.trades.append({
        "id": pos.candidate_id, "strategy": pos.strategy, "side": pos.side,
        "entry": pos.entry, "exit": px, "stop": pos.stop, "target": pos.target,
        "pnl": round(pnl, 4), "r_cash": round(r, 4), "reason": reason,
        "opened": iso(pos.opened), "closed": iso(ts),
    })
    book.position = None


def _manage(book: Book, ts: datetime, o, h, l, c, events) -> None:
    pos = book.position
    if pos is None:
        return
    if news_flatten(ts, events):
        _close(book, pos, ts, _exit_px(pos.side, c, False, pos.stop, o), "NEWS_EXIT")
        return
    hit_stop = (pos.side == "BUY" and l <= pos.stop) or (pos.side == "SELL" and h >= pos.stop)
    hit_tgt = (pos.side == "BUY" and h >= pos.target) or (pos.side == "SELL" and l <= pos.target)
    if hit_stop and hit_tgt:
        _close(book, pos, ts, _exit_px(pos.side, o, True, pos.stop, o), "STOP_AMBIGUOUS")
        return
    if hit_stop:
        _close(book, pos, ts, _exit_px(pos.side, o, True, pos.stop, o), "STOP")
        return
    if hit_tgt:
        _close(book, pos, ts, _exit_px(pos.side, c, False, pos.stop, o), "TARGET")
        return
    if ts >= pos.deadline:
        _close(book, pos, ts, _exit_px(pos.side, c, False, pos.stop, o), "TIME")


def _deadline(cand: Candidate, fill_time: datetime) -> datetime:
    if cand.strategy == "A":
        return min(fill_time + timedelta(hours=A_HOLD), ny_1545(fill_time))
    if cand.strategy == "B":
        return min(fill_time + timedelta(hours=B_HOLD), london_1200(fill_time))
    return min(fill_time + timedelta(hours=C_HOLD), ny_1545(fill_time))


def _target(cand: Candidate, entry: float, stop: float) -> tuple[float | None, str]:
    d = abs(entry - stop)
    if d <= 0:
        return None, "NO_DISTANCE"
    if cand.strategy == "B" and not cand.extras.get("v3"):
        m = cand.extras.get("M")
        if m is None:
            return None, "NO_MID"
        if cand.side == "BUY":
            tgt = min(m, entry + 2 * d)
            if tgt - entry < cand.min_rr * d:
                return None, "NO_ROOM"
        else:
            tgt = max(m, entry - 2 * d)
            if entry - tgt < cand.min_rr * d:
                return None, "NO_ROOM"
        return tgt, ""
    if cand.side == "BUY":
        return entry + cand.target_r * d, ""
    return entry - cand.target_r * d, ""


def _snapshot(cand: Candidate, m5, m15, h4, i5, i15, i4) -> dict:
    def pack(df, i, n):
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
    return {
        "spec_version": SPEC_VERSION,
        "candidate_id": cand.candidate_id,
        "strategy": cand.strategy,
        "side": cand.side,
        "structural_stop": cand.structural_stop,
        "atr": cand.atr,
        "h4_dir": cand.h4_dir,
        "h4": {
            "close": float(h4.close.iloc[i4]) if i4 >= 0 else None,
            "ema20": cand.extras.get("h4_ema20"),
            "ema50": cand.extras.get("h4_ema50"),
        },
        "m5": pack(m5, i5, 12),
        "m15": pack(m15, i15, 8),
        "levels": cand.extras,
        "session": iso(cand.confirm_time),
        "risk_budget_usd": RISK_PER_TRADE,
    }


def _try_fill(book: Book, cand: Candidate, fill_mid: float, confirm_mid: float, ts: datetime) -> str:
    if book.kill or book.position is not None:
        return "BUSY"
    if abs(fill_mid - confirm_mid) > CHASE_ATR_FRAC * cand.atr:
        return "CHASE"
    entry = _entry_px(cand.side, fill_mid)
    stop, err = apply_stop_band(cand.side, entry, cand.structural_stop, cand.atr, XAUUSD.tick)
    if stop is None:
        return err or "STOP"
    lots, r_cash, why = size_trade(XAUUSD, cand.side, entry, stop, book.equity)
    if lots <= 0:
        return why
    tgt, terr = _target(cand, entry, stop)
    if tgt is None:
        return terr
    book.position = Position(
        cand.strategy, cand.side, lots, entry, stop, tgt, r_cash, ts, _deadline(cand, ts), cand.candidate_id,
    )
    return "FILLED"


def replay_m5(
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
    e20_15 = ema_seeded(m15.close.to_numpy(), 20)
    e50_15 = ema_seeded(m15.close.to_numpy(), 50)
    e20_h4 = ema_seeded(h4.close.to_numpy(), 20)
    e50_h4 = ema_seeded(h4.close.to_numpy(), 50)
    h4c = h4.close.to_numpy()
    m15o, m15h, m15l, m15c = (m15[x].to_numpy() for x in ("open", "high", "low", "close"))
    m5o, m5h, m5l, m5c = (m5[x].to_numpy() for x in ("open", "high", "low", "close"))
    tr5 = true_range(m5h, m5l, m5c)
    atr5 = wilder_atr(m5h, m5l, m5c, 14)

    names = []
    for s in ("A", "B", "C"):
        if strategy_filter and s != strategy_filter:
            continue
        names += [f"{s}_mech", f"{s}_ai"]
    books = {n: Book(n) for n in names}
    books["combined_mech"] = Book("combined_mech")
    sa, sb, sc = StrategyA(), StrategyB(), StrategyC()
    pause_until = None
    last_m15 = -1
    m15_end = m15.index + pd.Timedelta(minutes=15)
    h4_end = h4.index + pd.Timedelta(hours=4)
    m5_end = m5.index + pd.Timedelta(minutes=5)
    bars_after_warmup = 0
    n_paused = n_blackout = n_gap = 0

    def completed(ends, now) -> int:
        ok = np.asarray(ends <= now)
        idx = np.flatnonzero(ok)
        return int(idx[-1]) if idx.size else -1

    def as_utc(ts) -> datetime:
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    for i in range(len(m5) - 1):
        now = as_utc(m5_end[i])
        bar_start = as_utc(m5.index[i])
        dkey = now.date().isoformat()
        i4 = completed(h4_end, m5_end[i])
        i15 = completed(m15_end, m5_end[i])
        warm = i4 >= H4_WARMUP - 1
        if warm:
            bars_after_warmup += 1

        if i > 0:
            gap_sec = (m5.index[i] - m5.index[i - 1]).total_seconds()
            if gap_sec > 10 * 60:
                sa.invalidate()
                sb.invalidate()
                sc.invalidate()
                n_gap += 1

        blackout = bool(cal_err) or merged_blackout(now, events)
        if i > 0 and np.isfinite(atr5[i - 1]) and tr5[i] > 3 * atr5[i - 1]:
            pause_until = now + timedelta(minutes=15)
        paused = pause_until is not None and now < pause_until
        blocked = blackout or paused
        if paused:
            n_paused += 1
        if blackout:
            n_blackout += 1

        for book in books.values():
            if book.day != dkey:
                book.day = dkey
                book.daily_pnl = 0.0
                book.kill = False
            if book.equity <= STARTING_BALANCE - TRIAL_DRAWDOWN or book.peak - book.equity >= TRIAL_DRAWDOWN:
                book.kill = True
            if book.daily_pnl <= -DAILY_STOP:
                book.kill = True
            if book.kill and book.position is not None:
                _close(book, book.position, now, _exit_px(book.position.side, float(m5c[i]), False, book.position.stop, float(m5o[i])), "KILL")
                continue
            _manage(book, now, float(m5o[i]), float(m5h[i]), float(m5l[i]), float(m5c[i]), events)

        cands: list[Candidate] = []
        if i15 > last_m15 and i15 >= 1 and (not strategy_filter or strategy_filter == "A"):
            ts15 = as_utc(m15_end[i15])
            ca = sa.on_m15(
                i15, ts15, m15o, m15h, m15l, m15c, e20_15, e50_15,
                h4c, e20_h4, e50_h4, i4, blocked,
            )
            if ca:
                ca.confirm_m5 = i
                cands.append(ca)
            last_m15 = i15
        if not strategy_filter or strategy_filter == "B":
            cb = sb.on_m5(i, now, bar_start, m5o, m5h, m5l, m5c, h4c, e20_h4, e50_h4, i4, blocked)
            if cb:
                cands.append(cb)
        if not strategy_filter or strategy_filter == "C":
            cc = sc.on_m5(i, now, bar_start, m5o, m5h, m5l, m5c, h4c, e20_h4, e50_h4, i4, blocked, events)
            if cc:
                cands.append(cc)

        if not warm or paused:
            cands = []

        fill_mid = float(m5o[i + 1])
        fill_ts = as_utc(m5.index[i + 1])
        confirm_mid = float(m5c[i])
        cands.sort(key=lambda x: {"A": 0, "B": 1, "C": 2}[x.strategy])

        for cand in cands:
            snap = _snapshot(cand, m5, m15, h4, i, i15, i4)
            mech = f"{cand.strategy}_mech"
            ai = f"{cand.strategy}_ai"
            if mech in books:
                why = _try_fill(books[mech], cand, fill_mid, confirm_mid, fill_ts)
                books[mech].funnel.append({"id": cand.candidate_id, "result": why})
                if why == "FILLED":
                    if cand.strategy == "A":
                        sa.commit_fill(cand.extras.get("window"))
                    elif cand.strategy == "B":
                        sb.commit_fill()
                    else:
                        sc.commit_fill()
                elif cand.strategy == "A":
                    sa.s.need_fresh = True
            if ai in books:
                decision, meta = ("TAKE", {"reason_code": "CONTEXT_SUPPORTS"}) if not audit else decide(snap)
                if decision != "TAKE":
                    books[ai].vetoes.append({"id": cand.candidate_id, **meta})
                    books[ai].funnel.append({"id": cand.candidate_id, "result": "AI_PASS"})
                else:
                    why = _try_fill(books[ai], cand, fill_mid, confirm_mid, fill_ts)
                    books[ai].funnel.append({"id": cand.candidate_id, "result": why, "ai": meta})
            if "combined_mech" in books and (not strategy_filter):
                why = _try_fill(books["combined_mech"], cand, fill_mid, confirm_mid, fill_ts)
                books["combined_mech"].funnel.append({"id": cand.candidate_id, "result": why})

    for book in books.values():
        if book.position is not None:
            last = as_utc(m5_end[-1])
            _close(book, book.position, last, _exit_px(book.position.side, float(m5c[-1]), False, book.position.stop, float(m5o[-1])), "END")

    diagnostics = {
        "m5_bars": int(len(m5)),
        "h4_bars": int(len(h4)),
        "h4_live_bars": int(len(h4_live)),
        "warmup_h4": H4_WARMUP,
        "bars_after_warmup": bars_after_warmup,
        "paused_bars": n_paused,
        "blackout_bars": n_blackout,
        "gap_invalidations": n_gap,
        "funnel": {
            "A": {"armed": sa.n_arm, "confirmed": sa.n_confirm, "cancelled": sa.n_cancel},
            "B": {
                "armed": sb.n_arm, "confirmed": sb.n_confirm, "cancelled": sb.n_cancel,
                "frozen_days": sb.n_frozen, "hist_short": sb.n_hist_short, "width_fail": sb.n_width_fail,
            },
            "C": {
                "armed": sc.n_arm, "confirmed": sc.n_confirm, "cancelled": sc.n_cancel,
                "frozen_days": sc.n_frozen, "hist_short": sc.n_hist_short, "width_fail": sc.n_width_fail,
                "ref_blackout": sc.n_ref_blackout,
            },
        },
        "h4_source": h4_source,
    }
    return {
        "source": source,
        "books": books,
        "spec": SPEC_VERSION,
        "n_h4": int(len(h4)),
        "warmup_h4": H4_WARMUP,
        "diagnostics": diagnostics,
    }


def summarize(book: Book) -> dict:
    closed = book.trades
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    pf = None
    if losses and wins:
        pf = sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
    net = book.equity - STARTING_BALANCE
    return {
        "book": book.name,
        "spec": SPEC_VERSION,
        "equity": round(book.equity, 2),
        "net_usd": round(net, 2),
        "trades": len(closed),
        "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(closed), 2) if closed else None,
        "expectancy_r": round(float(np.mean([t["r_cash"] for t in closed])), 4) if closed else None,
        "profit_factor": None if pf is None else round(pf, 3),
        "kill": book.kill,
        "vetoes": len(book.vetoes),
        "candidates": len(book.funnel),
    }


def save_books(result: dict, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, book in result["books"].items():
        summary = summarize(book)
        out[name] = summary
        path = folder / f"{name}.json"
        path.write_text(json.dumps({"summary": summary, "trades": book.trades, "funnel": book.funnel[-200:], "vetoes": book.vetoes}, indent=2))
    (folder / "summary.json").write_text(json.dumps({
        "source": result["source"],
        "spec": result["spec"],
        "diagnostics": result.get("diagnostics"),
        "books": out,
    }, indent=2))
