"""Deterministic paper books. Protective exits never wait on DeepSeek."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from deepseek_trial.auditor import audit_ticket
from deepseek_trial.calendar_rules import (
    in_entry_blackout,
    iso,
    load_calendar,
    near_high_impact,
    no_new_entry_near_weekend,
    weekend_flatten_due,
)
from deepseek_trial.config import (
    DAILY_STOP,
    SIGNAL_VALIDITY_SEC,
    STARTING_BALANCE,
    TRIAL_DRAWDOWN_CAP,
    TRIAL_EQUITY_FLOOR,
    SymbolSpec,
)
from deepseek_trial.risk import round_away, size_trade, spread_price
from deepseek_trial.t1_gold_trend.strategy import (
    HOLD_WEEKEND as T1_HOLD_WEEKEND,
    STOP_K as T1_STOP_K,
    STRATEGY_ID as T1_ID,
    ratchet_stop,
    t1_signal,
    t1_view,
)
from deepseek_trial.t2_gold_compression.strategy import (
    HOLD_WEEKEND as T2_HOLD_WEEKEND,
    STOP_K as T2_STOP_K,
    STRATEGY_ID as T2_ID,
    TIME_EXIT_BARS,
    t2_signal,
    t2_view,
)
from deepseek_trial.t3_btc_ma.strategy import (
    HOLD_WEEKEND as T3_HOLD_WEEKEND,
    STOP_K as T3_STOP_K,
    STRATEGY_ID as T3_ID,
    t3_signal,
    t3_view,
)
from deepseek_trial.ticket import Ticket, idle_ticket, snapshot_hash


@dataclass
class Position:
    side: str
    volume: float
    entry: float
    stop: float
    planned_risk: float
    fill_index: int
    opened_utc: str


@dataclass
class Book:
    strategy_id: str
    book_name: str
    equity: float = STARTING_BALANCE
    peak: float = STARTING_BALANCE
    daily_pnl: float = 0.0
    daily_key: str = ""
    position: Position | None = None
    kill: bool = False
    prev_state: str = "CASH"
    stop_blocked: bool = False
    seeded: bool = False
    entries_this_week: int = 0
    week_key: str = ""
    trades: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    incidents: list = field(default_factory=list)
    source: str = ""
    experimental: bool = False

    @property
    def open_risk(self) -> float:
        return 0.0 if self.position is None else self.position.planned_risk


def _mid_bid_ask(mid: float, spec: SymbolSpec) -> tuple[float, float]:
    spread = spread_price(mid, spec)
    bid = mid
    ask = mid + spread
    return bid, ask


def _entry_fill(side: str, mid: float, spec: SymbolSpec) -> float:
    bid, ask = _mid_bid_ask(mid, spec)
    if side == "BUY":
        return ask + spec.tick
    return bid - spec.tick


def _exit_fill(side: str, mid: float, spec: SymbolSpec, is_stop: bool, stop: float, opened: float, high: float, low: float) -> float:
    bid, ask = _mid_bid_ask(mid, spec)
    if is_stop:
        gap_long = side == "BUY" and opened <= stop
        gap_short = side == "SELL" and opened >= stop
        raw = opened if (gap_long or gap_short) else stop
        if side == "BUY":
            return raw - spec.tick
        return raw + spec.tick
    if side == "BUY":
        return bid - spec.tick
    return ask + spec.tick


def _stop_hit(side: str, high: float, low: float, stop: float) -> bool:
    if side == "BUY":
        return low <= stop
    return high >= stop


def _mark(side: str, mid: float, entry: float, volume: float, spec: SymbolSpec) -> float:
    bid, ask = _mid_bid_ask(mid, spec)
    px = bid if side == "BUY" else ask
    sign = 1.0 if side == "BUY" else -1.0
    return sign * spec.contract_size * volume * (px - entry)


def _week_key(ts: pd.Timestamp) -> str:
    iso_cal = ts.isocalendar()
    return f"{iso_cal[0]}-W{iso_cal[1]:02d}"


def _reset_day(book: Book, ts: pd.Timestamp) -> None:
    key = str(ts.date())
    if book.daily_key != key:
        book.daily_key = key
        book.daily_pnl = 0.0
    week = _week_key(ts)
    if book.week_key != week:
        book.week_key = week
        book.entries_this_week = 0


def _kill(book: Book) -> bool:
    if book.equity <= TRIAL_EQUITY_FLOOR:
        return True
    if book.peak - book.equity >= TRIAL_DRAWDOWN_CAP:
        return True
    if book.daily_pnl <= -DAILY_STOP:
        return True
    return False


def _close_position(book: Book, spec: SymbolSpec, ts: pd.Timestamp, px: float, reason: str, idx: int) -> None:
    pos = book.position
    if pos is None:
        return
    sign = 1.0 if pos.side == "BUY" else -1.0
    pnl = sign * spec.contract_size * pos.volume * (px - pos.entry)
    r_mult = pnl / pos.planned_risk if pos.planned_risk else 0.0
    book.equity += pnl
    book.daily_pnl += pnl
    book.peak = max(book.peak, book.equity)
    book.trades.append({
        "entry_time": pos.opened_utc,
        "exit_time": iso(ts.to_pydatetime()),
        "side": pos.side,
        "volume": pos.volume,
        "entry": pos.entry,
        "exit": px,
        "stop": pos.stop,
        "pnl": round(pnl, 4),
        "r": round(r_mult, 4),
        "reason": reason,
        "exit_index": idx,
    })
    if reason == "STOP_HIT" and book.strategy_id == T3_ID:
        book.stop_blocked = True
    book.position = None


def _entry_blocked(now: datetime, instrument: str, events, cal_err: str | None, hold_weekend: bool, skip_hour_blackout: bool) -> str:
    if cal_err:
        return "DATA_MISSING"
    if (not skip_hour_blackout) and in_entry_blackout(now):
        return "WINDOW_CLOSED"
    if near_high_impact(now, instrument, events):
        return "WINDOW_CLOSED"
    if (not hold_weekend) and no_new_entry_near_weekend(now):
        return "WINDOW_CLOSED"
    return ""


def _event_id(strategy_id: str, ts: pd.Timestamp, kind: str) -> str:
    return f"{strategy_id}-{ts.strftime('%Y%m%dT%H%M%SZ')}-{kind}"


def _log_decision(book: Book, ticket: Ticket, extra: dict | None = None) -> None:
    row = ticket.to_json_dict()
    if extra:
        row.update(extra)
    book.decisions.append(row)


def replay(
    df: pd.DataFrame,
    *,
    strategy_id: str,
    spec: SymbolSpec,
    instrument: str,
    experimental: bool = False,
    hold_weekend: bool | None = None,
    audit: bool = False,
    source: str = "",
    calendar_ok_if_empty: bool = True,
) -> Book:
    if strategy_id == T1_ID:
        hold = T1_HOLD_WEEKEND if hold_weekend is None else hold_weekend
    elif strategy_id == T2_ID:
        hold = T2_HOLD_WEEKEND if hold_weekend is None else hold_weekend
    else:
        hold = T3_HOLD_WEEKEND if hold_weekend is None else hold_weekend

    events, cal_err = load_calendar()
    if calendar_ok_if_empty and cal_err is None:
        cal_err = None
    elif not calendar_ok_if_empty and cal_err:
        pass

    o = df.open.to_numpy(dtype=float)
    h = df.high.to_numpy(dtype=float)
    l = df.low.to_numpy(dtype=float)
    c = df.close.to_numpy(dtype=float)
    idx = df.index
    gaps = pd.Series(idx).diff().dt.total_seconds() / 3600.0
    skip_hour_blackout = bool(np.nanmedian(gaps.to_numpy()) >= 12)
    atr10_pre = atr20_pre = None
    if strategy_id == T2_ID:
        from deepseek_trial.indicators import wilder_atr
        from deepseek_trial.t2_gold_compression.strategy import ATR_QUIET_N, ATR_STOP_N
        atr10_pre = wilder_atr(h, l, c, ATR_QUIET_N)
        atr20_pre = wilder_atr(h, l, c, ATR_STOP_N)
    book = Book(strategy_id=strategy_id, book_name="supervised" if audit else "reference",
                source=source, experimental=experimental)

    for t in range(len(df) - 1):
        ts = pd.Timestamp(idx[t])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        now = ts.to_pydatetime()
        _reset_day(book, ts)
        nxt = pd.Timestamp(idx[t + 1])
        if nxt.tzinfo is None:
            nxt = nxt.tz_localize("UTC")

        if book.kill:
            if book.position is not None:
                px = _exit_fill(book.position.side, float(c[t]), spec, False, book.position.stop, float(o[t]), float(h[t]), float(l[t]))
                _close_position(book, spec, ts, px, "KILL_SWITCH", t)
            ticket = idle_ticket(strategy_id, _event_id(strategy_id, ts, "KILL"), iso(now), "KILL_SWITCH")
            _log_decision(book, ticket)
            continue

        # Protective path: stop, time exit, weekend — never ask the model.
        if book.position is not None:
            pos = book.position
            if _stop_hit(pos.side, float(h[t]), float(l[t]), pos.stop):
                px = _exit_fill(pos.side, float(o[t]), spec, True, pos.stop, float(o[t]), float(h[t]), float(l[t]))
                _close_position(book, spec, ts, px, "STOP_HIT", t)
            elif strategy_id == T2_ID and (t - pos.fill_index) >= TIME_EXIT_BARS:
                px = _exit_fill(pos.side, float(c[t]), spec, False, pos.stop, float(o[t]), float(h[t]), float(l[t]))
                _close_position(book, spec, ts, px, "TIME_EXIT", t)
            elif weekend_flatten_due(now, mandatory=not hold) and book.position is not None:
                px = _exit_fill(pos.side, float(c[t]), spec, False, pos.stop, float(o[t]), float(h[t]), float(l[t]))
                _close_position(book, spec, ts, px, "WEEKEND_EXIT", t)

        if _kill(book):
            book.kill = True
            if book.position is not None:
                px = _exit_fill(book.position.side, float(c[t]), spec, False, book.position.stop, float(o[t]), float(h[t]), float(l[t]))
                _close_position(book, spec, ts, px, "KILL_SWITCH", t)
            continue

        floating = 0.0
        if book.position is not None:
            floating = _mark(book.position.side, float(c[t]), book.position.entry, book.position.volume, spec)
        mtm = book.equity + floating
        book.peak = max(book.peak, mtm)

        features = {"close": float(c[t]), "index": t}
        action, side, reason = "NO_TRADE", "NONE", "NO_SIGNAL"
        stop_px = None
        atr_val = None

        if strategy_id == T1_ID:
            view = t1_view(c, h, l, t)
            in_long = book.position is not None and book.position.side == "BUY"
            action, reason = t1_signal(view, in_long)
            side = "BUY" if action == "OPEN" else ("BUY" if action in ("CLOSE", "HOLD", "AMEND_STOP") and in_long else "NONE")
            atr_val = view.atr
            features.update({"allow_long": view.allow_long, "ret_12_1": view.ret_12_1, "sma200": view.sma200, "atr": view.atr})
            if action == "HOLD" and in_long and np.isfinite(view.close) and np.isfinite(view.atr):
                candidate = ratchet_stop(view.close, view.atr, book.position.stop, spec.tick)
                if candidate is not None:
                    rounded = round_away(candidate, book.position.entry, spec.tick)
                    if rounded > book.position.stop:
                        if rounded < float(c[t]):
                            book.position.stop = rounded
                            action, reason = "AMEND_STOP", "STOP_UPDATE"
                            stop_px = rounded
                        else:
                            px = _exit_fill("BUY", float(c[t]), spec, False, book.position.stop, float(o[t]), float(h[t]), float(l[t]))
                            _close_position(book, spec, ts, px, "STOP_HIT", t)
                            action, reason, side = "NO_TRADE", "STOP_HIT", "NONE"
        elif strategy_id == T2_ID:
            view = t2_view(c, h, l, t, atr10=atr10_pre, atr20=atr20_pre)
            in_pos = book.position is not None
            action, side, reason = t2_signal(view, in_pos)
            atr_val = view.atr
            features.update({"quiet": view.quiet, "upper20": view.upper20, "lower20": view.lower20, "atr": view.atr})
            if action == "HOLD":
                side = book.position.side if book.position else "NONE"
        else:
            view = t3_view(c, h, l, t)
            if view.state == "CASH":
                book.stop_blocked = False
            in_long = book.position is not None
            if view.reason != "DATA_MISSING" and not book.seeded:
                book.prev_state = view.state
                book.seeded = True
                action, reason, side = "NO_TRADE", "NO_SIGNAL", "NONE"
            else:
                action, reason = t3_signal(
                    view, book.prev_state, in_long, book.stop_blocked, book.entries_this_week,
                )
                side = "BUY" if action == "OPEN" else ("BUY" if in_long and action in ("CLOSE", "HOLD") else "NONE")
            atr_val = view.atr
            features.update({"state": view.state, "sma5": view.sma5, "sma100": view.sma100, "atr": view.atr})
            if view.reason != "DATA_MISSING":
                book.prev_state = view.state

        event_id = _event_id(strategy_id, ts, action)
        snap = snapshot_hash(features)
        expires = iso(nxt.to_pydatetime())

        if action == "CLOSE" and book.position is not None:
            pos = book.position
            ticket = Ticket(
                spec_version="2.0", strategy_id=strategy_id, event_id=event_id,
                snapshot_hash=snap, decision_time_utc=iso(now), action="CLOSE",
                order_type="NONE", side=pos.side, entry_reference=None, stop_loss=None,
                take_profit=None, volume_lots=pos.volume, expires_at_utc=None, reason_code=reason,
            )
            px = _exit_fill(pos.side, float(c[t]), spec, False, pos.stop, float(o[t]), float(h[t]), float(l[t]))
            _close_position(book, spec, ts, px, reason, t)
            _log_decision(book, ticket, {"features": features})
            continue

        if action == "AMEND_STOP" and book.position is not None and stop_px is not None:
            ticket = Ticket(
                spec_version="2.0", strategy_id=strategy_id, event_id=event_id,
                snapshot_hash=snap, decision_time_utc=iso(now), action="AMEND_STOP",
                order_type="NONE", side=book.position.side, entry_reference=None,
                stop_loss=stop_px, take_profit=None, volume_lots=0.0,
                expires_at_utc=None, reason_code=reason,
            )
            _log_decision(book, ticket, {"features": features})
            continue

        if action != "OPEN":
            hold_action = "HOLD" if book.position is not None else "NO_TRADE"
            ticket = idle_ticket(strategy_id, event_id, iso(now), reason, snap, action=hold_action if action == "HOLD" else "NO_TRADE")
            _log_decision(book, ticket, {"features": features})
            continue

        block = _entry_blocked(now, instrument, events, cal_err, hold, skip_hour_blackout)
        if block:
            ticket = idle_ticket(strategy_id, event_id, iso(now), block, snap)
            _log_decision(book, ticket, {"features": features})
            continue
        if atr_val is None or not np.isfinite(atr_val) or atr_val <= 0:
            ticket = idle_ticket(strategy_id, event_id, iso(now), "DATA_MISSING", snap)
            _log_decision(book, ticket, {"features": features})
            continue

        mid = float(o[t + 1])
        fill = _entry_fill(side, mid, spec)
        k = T1_STOP_K if strategy_id == T1_ID else T2_STOP_K if strategy_id == T2_ID else T3_STOP_K
        raw_stop = fill - k * atr_val if side == "BUY" else fill + k * atr_val
        stop = round_away(raw_stop, fill, spec.tick)
        sized = size_trade(spec, side, fill, stop, book.equity, book.daily_pnl, book.peak, book.open_risk)
        if sized.lots <= 0:
            ticket = idle_ticket(strategy_id, event_id, iso(now), sized.reason, snap)
            _log_decision(book, ticket, {"features": features, "expected_fill": fill, "stop": stop})
            continue

        engine = Ticket(
            spec_version="2.0", strategy_id=strategy_id, event_id=event_id,
            snapshot_hash=snap, decision_time_utc=iso(now), action="OPEN",
            order_type="MARKET", side=side, entry_reference=fill, stop_loss=stop,
            take_profit=None, volume_lots=sized.lots, expires_at_utc=expires,
            reason_code="VALID_SIGNAL",
        )
        if audit:
            _model, err, meta = audit_ticket(engine, spec.tick)
            if err:
                book.incidents.append({"event_id": event_id, "reason": err, "meta": meta})
                ticket = idle_ticket(strategy_id, event_id, iso(now), err, snap)
                _log_decision(book, ticket, {"features": features, "audit": meta})
                continue
        book.position = Position(
            side=side, volume=sized.lots, entry=fill, stop=stop,
            planned_risk=sized.planned_risk, fill_index=t + 1, opened_utc=iso(nxt.to_pydatetime()),
        )
        book.entries_this_week += 1
        _log_decision(book, engine, {"features": features, "fill_index": t + 1})

    if book.position is not None:
        last = pd.Timestamp(idx[-1])
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        px = _exit_fill(book.position.side, float(c[-1]), spec, False, book.position.stop, float(o[-1]), float(h[-1]), float(l[-1]))
        _close_position(book, spec, last, px, "TIME_EXIT", len(df) - 1)
    return book


def save_book(book: Book, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy_id": book.strategy_id,
        "book_name": book.book_name,
        "experimental": book.experimental,
        "source": book.source,
        "equity": book.equity,
        "peak": book.peak,
        "kill": book.kill,
        "trades": book.trades,
        "incidents": book.incidents,
        "n_decisions": len(book.decisions),
        "n_no_trade": sum(1 for d in book.decisions if d.get("action") == "NO_TRADE"),
        "n_open": sum(1 for d in book.decisions if d.get("action") == "OPEN"),
    }
    path.write_text(json.dumps(payload, indent=2))
    decisions_path = path.with_name(path.stem + "_decisions.jsonl")
    with decisions_path.open("w") as handle:
        for row in book.decisions:
            handle.write(json.dumps(row, default=str) + "\n")


def summarize(book: Book) -> dict:
    closed = book.trades
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    pf = None
    if losses:
        pf = sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)) if wins else 0.0
    elif wins:
        pf = None
    eq = STARTING_BALANCE
    peak = STARTING_BALANCE
    maxdd = 0.0
    for trade in closed:
        eq += trade["pnl"]
        peak = max(peak, eq)
        maxdd = max(maxdd, peak - eq)
    net = book.equity - STARTING_BALANCE
    return {
        "strategy_id": book.strategy_id,
        "book": book.book_name,
        "experimental": book.experimental,
        "source": book.source,
        "starting": STARTING_BALANCE,
        "equity": round(book.equity, 2),
        "net_return_pct": round(100 * net / STARTING_BALANCE, 4),
        "trades": len(closed),
        "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(closed), 2) if closed else None,
        "expectancy_r": round(float(np.mean([t["r"] for t in closed])), 4) if closed else None,
        "profit_factor": None if pf is None else round(pf, 3),
        "max_dd_usd": round(maxdd, 2),
        "kill": book.kill,
        "incidents": len(book.incidents),
        "decisions": len(book.decisions),
        "opens": sum(1 for d in book.decisions if d.get("action") == "OPEN"),
        "ohlc_note": (
            f"OHLC replay fills the next bar open; live paper still uses {SIGNAL_VALIDITY_SEC}s expiry."
        ),
    }
