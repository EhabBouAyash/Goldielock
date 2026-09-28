"""Session-inventory drifts: buy a clock time, sell another.

Dealers and funds have preferred hours. That can leave a small drift in
between, the same way the Asia-session long showed up in brooks_h2_l2.
These are not candlestick patterns. Direction is tested both ways; the
multiple on the volatility stop is chosen on 2009-2017 only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from gold_edges.core import SLIPPAGE

# Dukascopy Sep 2023: Asia reopen median / NY-cash median.
REOPEN_MULT = 0.362 / 0.30

# name -> (start_et_hours, end_et_hours, weekdays of the start, spread_mult)
# weekdays: Mon=0. Sunday=6. Weekend is special-cased.
SESSIONS = {
    "asia_18_03": (18.0, 3.0, (6, 0, 1, 2, 3), REOPEN_MULT),
    "london_03_08": (3.0, 8.0, (0, 1, 2, 3, 4), 1.0),
    "ny_am_0820_12": (8.0 + 20 / 60, 12.0, (0, 1, 2, 3, 4), 1.0),
    # HistData has no 17:00 ET hour (daily break). The last print is 16:xx.
    "overnight_16_08": (16.0, 8.0, (0, 1, 2, 3), 1.0),
    "gap_16_18": (16.0, 18.0, (0, 1, 2, 3), REOPEN_MULT),
    "weekend_fri16_sun18": (16.0, 18.0, (4,), REOPEN_MULT),
}


def collect(minutes: pd.DataFrame, start_h: float, end_h: float, weekdays: tuple,
            weekend: bool = False, gap: bool = False) -> pd.DataFrame:
    """One row per session with the bid path and the first ask print."""
    ny = minutes.tz_convert("America/New_York")
    clock = ny.index.hour + ny.index.minute / 60.0
    day = ny.index.normalize()
    dow = ny.index.dayofweek
    wraps = (not weekend) and (not gap) and (end_h <= start_h)
    if weekend:
        # Friday 17:00 ET through Sunday 18:00 ET.
        in_sess = ((dow == 4) & (clock >= start_h)) | (dow == 5) | ((dow == 6) & (clock < end_h))
        session_day = day.where(dow != 6, day - pd.Timedelta(days=2))
        session_day = session_day.where(dow != 5, day - pd.Timedelta(days=1))
        weekdays = (4,)
    elif wraps:
        after = clock < end_h
        session_day = day.where(~after, day - pd.Timedelta(days=1))
        in_sess = (clock >= start_h) | after
    else:
        session_day = day
        in_sess = (clock >= start_h) & (clock < end_h)
    frame = pd.DataFrame({
        "bid_open": minutes["bid_open"].to_numpy(),
        "bid_high": minutes["bid_high"].to_numpy(),
        "bid_low": minutes["bid_low"].to_numpy(),
        "bid_close": minutes["bid_close"].to_numpy(),
        "ask_open": minutes["ask_open"].to_numpy(),
        "ask_high": minutes["ask_high"].to_numpy(),
        "ask_low": minutes["ask_low"].to_numpy(),
        "spread": (minutes["ask_close"] - minutes["bid_close"]).to_numpy(),
        "ny": ny.index,
        "session": session_day,
        "dow": session_day.dayofweek,
    }, index=minutes.index)
    frame = frame[in_sess & frame["dow"].isin(weekdays)]
    start_delta = pd.Timedelta(hours=int(start_h), minutes=int(round((start_h % 1) * 60)))
    end_delta = pd.Timedelta(hours=int(end_h), minutes=int(round((end_h % 1) * 60)))
    rows = []
    for sess, g in frame.groupby("session", sort=True):
        start_ts = sess + start_delta
        if weekend:
            end_ts = sess + pd.Timedelta(days=2) + end_delta
        elif wraps or gap:
            end_ts = sess + pd.Timedelta(days=1) + end_delta
        else:
            end_ts = sess + end_delta
        window = g[(g["ny"] >= start_ts) & (g["ny"] < start_ts + pd.Timedelta(minutes=60))]
        if window.empty:
            continue
        held = g[(g["ny"] >= window["ny"].iloc[0]) & (g["ny"] < end_ts)]
        min_len = 2 if (weekend or gap) else 15
        if len(held) < min_len:
            continue
        rows.append({
            "session": pd.Timestamp(sess),
            "ask_open": float(window["ask_open"].iloc[0]),
            "bid_open_px": float(window["bid_open"].iloc[0]),
            "spread": float(window["spread"].iloc[0]),
            "bid_open": held["bid_open"].to_numpy(),
            "bid_low": held["bid_low"].to_numpy(),
            "bid_close": held["bid_close"].to_numpy(),
            "ask_open_path": held["ask_open"].to_numpy(),
            "ask_high": held["ask_high"].to_numpy(),
            "bid_exit": float(held["bid_close"].to_numpy()[-1]),
            "ask_exit": float(held["ask_open"].to_numpy()[-1]),
            "range": float(held["bid_high"].max() - held["bid_low"].min()),
        })
    return pd.DataFrame(rows)


def simulate(sessions: pd.DataFrame, direction: int, stop_k: float | None,
             spread_mult: float) -> pd.DataFrame:
    ranges = sessions["range"].to_numpy()
    vol = pd.Series(ranges).rolling(20).mean().shift(1).to_numpy()
    trades = []
    for i, row in enumerate(sessions.itertuples(index=False)):
        if not np.isfinite(vol[i]) or vol[i] <= 0:
            continue
        extra = row.spread * (spread_mult - 1.0)
        if direction == 1:
            fill = row.ask_open + extra + SLIPPAGE
            stop = None if stop_k is None else fill - stop_k * vol[i]
            exit_px, reason = row.bid_exit, "TIME"
            if stop is not None:
                for open_, low in zip(row.bid_open, row.bid_low):
                    if low <= stop:
                        exit_px = min(stop, open_) - SLIPPAGE
                        reason = "STOP"
                        break
            risk = vol[i] if stop is None else fill - stop
            pnl = exit_px - fill
        else:
            fill = row.bid_open_px - extra - SLIPPAGE
            stop = None if stop_k is None else fill + stop_k * vol[i]
            exit_px, reason = row.ask_exit, "TIME"
            if stop is not None:
                for open_, high in zip(row.ask_open_path, row.ask_high):
                    if high >= stop:
                        exit_px = max(stop, open_) + SLIPPAGE
                        reason = "STOP"
                        break
            risk = vol[i] if stop is None else stop - fill
            pnl = fill - exit_px
        if risk <= 0:
            continue
        sess = pd.Timestamp(row.session)
        if sess.tz is None:
            sess = sess.tz_localize("America/New_York")
        trades.append({
            "time": sess.tz_convert("UTC"),
            "exit_time": sess.tz_convert("UTC"),
            "dir": direction,
            "r": pnl / risk,
            "days": 0,
            "exit_reason": reason,
        })
    return pd.DataFrame(trades)


def collect_gap(minutes: pd.DataFrame, entry_h: float = 16.0, exit_h: float = 18.0,
                entry_days: tuple = (0, 1, 2, 3)) -> pd.DataFrame:
    """Last hour before the 17:00 ET break to the first half-hour after the 18:00 reopen.

    Mon-Thu entries exit at the next day's 18:00 (Tue-Fri). Friday is the weekend trade.
    """
    ny = minutes.tz_convert("America/New_York")
    clock = ny.index.hour + ny.index.minute / 60.0
    day = ny.index.normalize()
    frame = pd.DataFrame({
        "bid_open": minutes["bid_open"].to_numpy(),
        "bid_high": minutes["bid_high"].to_numpy(),
        "bid_low": minutes["bid_low"].to_numpy(),
        "bid_close": minutes["bid_close"].to_numpy(),
        "ask_open": minutes["ask_open"].to_numpy(),
        "ask_high": minutes["ask_high"].to_numpy(),
        "spread": (minutes["ask_close"] - minutes["bid_close"]).to_numpy(),
        "ny": ny.index,
        "day": day,
        "dow": day.dayofweek,
        "clock": clock,
    }, index=minutes.index)
    rows = []
    entries = frame[(frame["dow"].isin(entry_days)) & (frame["clock"] >= entry_h) & (frame["clock"] < 17.0)]
    for sess, g in entries.groupby("day", sort=True):
        exit_day = sess + pd.Timedelta(days=1)
        ex = frame[(frame["day"] == exit_day) & (frame["clock"] >= exit_h) & (frame["clock"] < exit_h + 0.5)]
        if g.empty or ex.empty:
            continue
        rows.append({
            "session": pd.Timestamp(sess),
            "ask_open": float(g["ask_open"].iloc[0]),
            "bid_open_px": float(g["bid_open"].iloc[0]),
            "spread": float(g["spread"].iloc[0]),
            "bid_open": np.array([float(g["bid_open"].iloc[0]), float(ex["bid_open"].iloc[0])]),
            "bid_low": np.array([float(g["bid_low"].min()), float(ex["bid_low"].min())]),
            "bid_close": g["bid_close"].to_numpy()[:1],
            "ask_open_path": np.array([float(g["ask_open"].iloc[0]), float(ex["ask_open"].iloc[0])]),
            "ask_high": np.array([float(g["ask_high"].max()), float(ex["ask_high"].max())]),
            "bid_exit": float(ex["bid_close"].iloc[0]),
            "ask_exit": float(ex["ask_open"].iloc[0]),
            "range": float(max(g["bid_high"].max(), ex["bid_high"].max()) - min(g["bid_low"].min(), ex["bid_low"].min())),
        })
    return pd.DataFrame(rows)


def collect_weekend(minutes: pd.DataFrame) -> pd.DataFrame:
    """Friday last hour (16:00-17:00 ET) to Sunday 18:00 reopen."""
    ny = minutes.tz_convert("America/New_York")
    clock = ny.index.hour + ny.index.minute / 60.0
    day = ny.index.normalize()
    frame = pd.DataFrame({
        "bid_open": minutes["bid_open"].to_numpy(),
        "bid_high": minutes["bid_high"].to_numpy(),
        "bid_low": minutes["bid_low"].to_numpy(),
        "bid_close": minutes["bid_close"].to_numpy(),
        "ask_open": minutes["ask_open"].to_numpy(),
        "ask_high": minutes["ask_high"].to_numpy(),
        "spread": (minutes["ask_close"] - minutes["bid_close"]).to_numpy(),
        "ny": ny.index,
        "day": day,
        "dow": day.dayofweek,
        "clock": clock,
    }, index=minutes.index)
    rows = []
    entries = frame[(frame["dow"] == 4) & (frame["clock"] >= 16.0) & (frame["clock"] < 17.0)]
    for sess, g in entries.groupby("day", sort=True):
        sunday = sess + pd.Timedelta(days=2)
        ex = frame[(frame["day"] == sunday) & (frame["clock"] >= 18.0) & (frame["clock"] < 18.5)]
        if g.empty or ex.empty:
            continue
        rows.append({
            "session": pd.Timestamp(sess),
            "ask_open": float(g["ask_open"].iloc[0]),
            "bid_open_px": float(g["bid_open"].iloc[0]),
            "spread": float(g["spread"].iloc[0]),
            "bid_open": np.array([float(g["bid_open"].iloc[0]), float(ex["bid_open"].iloc[0])]),
            "bid_low": np.array([float(g["bid_low"].min()), float(ex["bid_low"].min())]),
            "bid_close": g["bid_close"].to_numpy()[:1],
            "ask_open_path": np.array([float(g["ask_open"].iloc[0]), float(ex["ask_open"].iloc[0])]),
            "ask_high": np.array([float(g["ask_high"].max()), float(ex["ask_high"].max())]),
            "bid_exit": float(ex["bid_close"].iloc[0]),
            "ask_exit": float(ex["ask_open"].iloc[0]),
            "range": float(max(g["bid_high"].max(), ex["bid_high"].max()) -
                           min(g["bid_low"].min(), ex["bid_low"].min())),
        })
    return pd.DataFrame(rows)


def all_sessions(minutes: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    for name, (start, end, days, _mult) in SESSIONS.items():
        if name.startswith("gap"):
            out[name] = collect_gap(minutes, start, end, days)
        elif name.startswith("weekend"):
            out[name] = collect_weekend(minutes)
        else:
            out[name] = collect(minutes, start, end, days, weekend=False, gap=False)
    return out
