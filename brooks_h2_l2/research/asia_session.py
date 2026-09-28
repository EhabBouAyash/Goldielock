"""Asia-session XAUUSD long: buy the 18:00 ET reopen, sell at 03:00 ET.

The long-Asia drift was first measured on the full 2009-2026 sample, so this
split does not make that discovery clean. What is chosen only on 2009-2017 is
the volatility-stop multiple. 2018-2026 is the check.

Costs: HistData's 1.6bp model, plus the Dukascopy Sep 2023 reopen premium
(18:00-18:30 ET median $0.362 vs New York cash $0.30) and $0.10 stop slippage.
A stress run uses the reopen 90th percentile ($0.467).

Run from the repo root: python3 -m brooks_h2_l2.research.asia_session
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from brooks_h2_l2.backtest import r_stats
from brooks_h2_l2.data import load_minutes

RESULTS = Path(__file__).resolve().parents[1] / "results"
TRAIN_END = pd.Timestamp("2018-01-01", tz="UTC")
SLIPPAGE = 0.10
# Dukascopy Sep 2023: reopen median / NY-cash median, and reopen p90 / NY-cash median.
REOPEN_MULT = 0.362 / 0.30
STRESS_MULT = 0.467 / 0.30
STOPS = (None, 0.50, 0.75, 1.00, 1.50)
# FundedNext Stellar 1-Step, from day_trading_insights.py (initial-balance limits).
DAILY_SOFT, DAILY_HARD = 2.5, 3.0
MAX_SOFT, MAX_HARD = 5.0, 6.0


def _sessions(minutes: pd.DataFrame) -> pd.DataFrame:
    """One row per Sun-Thu 18:00 ET reopen that has a print within 30 minutes."""
    ny = minutes.tz_convert("America/New_York")
    clock = ny.index.hour + ny.index.minute / 60.0
    # Session date is the 18:00 ET date. Bars from 18:00 through 03:00 belong to it.
    session_day = ny.index.normalize()
    after_midnight = clock < 3
    session_day = session_day.where(~after_midnight, session_day - pd.Timedelta(days=1))
    in_session = (clock >= 18) | after_midnight
    frame = pd.DataFrame({
        "bid_open": minutes["bid_open"].to_numpy(),
        "bid_high": minutes["bid_high"].to_numpy(),
        "bid_low": minutes["bid_low"].to_numpy(),
        "bid_close": minutes["bid_close"].to_numpy(),
        "ask_open": minutes["ask_open"].to_numpy(),
        "spread": (minutes["ask_close"] - minutes["bid_close"]).to_numpy(),
        "ny": ny.index,
        "session": session_day,
    }, index=minutes.index)
    frame = frame[in_session & session_day.dayofweek.isin([6, 0, 1, 2, 3])]
    rows = []
    for day, g in frame.groupby("session", sort=True):
        start = day + pd.Timedelta(hours=18)
        end = day + pd.Timedelta(days=1, hours=3)
        window = g[(g["ny"] >= start) & (g["ny"] < start + pd.Timedelta(minutes=30))]
        if window.empty:
            continue
        held = g[(g["ny"] >= window["ny"].iloc[0]) & (g["ny"] < end)]
        if len(held) < 30:
            continue
        rows.append({
            "session": pd.Timestamp(day),
            "entry_ny": window["ny"].iloc[0],
            "ask_open": float(window["ask_open"].iloc[0]),
            "spread": float(window["spread"].iloc[0]),
            "bid_open": held["bid_open"].to_numpy(),
            "bid_low": held["bid_low"].to_numpy(),
            "bid_close": held["bid_close"].to_numpy(),
            "session_range": float(held["bid_high"].max() - held["bid_low"].min()),
        })
    return pd.DataFrame(rows)


def _simulate(sessions: pd.DataFrame, stop_k: float | None, spread_mult: float) -> pd.DataFrame:
    ranges = sessions["session_range"].to_numpy()
    atr = pd.Series(ranges).rolling(20).mean().shift(1).to_numpy()
    trades = []
    for i, row in enumerate(sessions.itertuples(index=False)):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        extra = row.spread * (spread_mult - 1.0)
        fill = row.ask_open + extra + SLIPPAGE
        stop = None if stop_k is None else fill - stop_k * atr[i]
        exit_px, reason = float(row.bid_close[-1]), "TIME"
        if stop is not None:
            for open_, low in zip(row.bid_open, row.bid_low):
                if low <= stop:
                    exit_px = min(stop, open_) - SLIPPAGE
                    reason = "STOP"
                    break
        risk = atr[i] if stop is None else fill - stop
        trades.append({
            "session": row.session.tz_convert("UTC"),
            "exit_reason": reason,
            "risk": risk,
            "r": (exit_px - fill) / risk,
            "stop_k": stop_k if stop_k is not None else 0.0,
        })
    return pd.DataFrame(trades)


def _prop(trades: pd.DataFrame, risk_pct: float) -> dict:
    pct = trades["r"].to_numpy() * risk_pct
    equity = np.cumsum(pct)
    dd = np.maximum.accumulate(np.maximum(equity, 0)) - equity
    return {
        "risk_pct": risk_pct,
        "return_pct": round(float(equity[-1]), 2),
        "max_dd_pct": round(float(dd.max()), 2),
        "worst_trade_pct": round(float(pct.min()), 2),
        "daily_soft_breaches": int((pct <= -DAILY_SOFT).sum()),
        "daily_hard_breaches": int((pct <= -DAILY_HARD).sum()),
        "max_soft_breached": bool(dd.max() >= MAX_SOFT),
        "max_hard_breached": bool(dd.max() >= MAX_HARD),
    }


def _line(trades: pd.DataFrame) -> str:
    st = r_stats(trades["r"].to_numpy())
    if not st.get("trades"):
        return "no trades"
    return (f"n={st['trades']} win={st['win_rate_pct']}% exp={st['expectancy_r']:+.3f}R "
            f"CI95={st.get('expectancy_ci95')} t={st.get('t_stat')}")


def main() -> None:
    sessions = _sessions(load_minutes())
    print(f"sessions {sessions['session'].iloc[0]} -> {sessions['session'].iloc[-1]} ({len(sessions)})")
    print(f"reopen spread x{REOPEN_MULT:.2f} (stress x{STRESS_MULT:.2f}), slippage ${SLIPPAGE}")
    rows = []
    chosen = None
    for mult, tag in ((REOPEN_MULT, "reopen"), (STRESS_MULT, "stress")):
        print(f"\n== spread {tag}")
        for k in STOPS:
            trades = _simulate(sessions, k, mult)
            t = pd.to_datetime(trades["session"])
            train, test = trades[t < TRAIN_END], trades[t >= TRAIN_END]
            label = "time exit only" if k is None else f"stop {k:.2f} x 20-session range"
            print(f"  {label:28s} train {_line(train)}")
            print(f"  {'':28s} test  {_line(test)} stops={100*(test.exit_reason=='STOP').mean():.0f}%")
            rows.append({"spread": tag, "stop_k": k if k is not None else 0, "train": _line(train), "test": _line(test)})
            if tag == "reopen" and k is not None and len(train) >= 100:
                exp = train["r"].mean()
                if chosen is None or exp > chosen[0]:
                    chosen = (exp, k, test)
        if tag == "reopen" and chosen:
            print(f"  chosen on 2009-2017: stop {chosen[1]:.2f}")
            for risk in (0.25, 0.50):
                print(f"  test prop {risk}%: {_prop(chosen[2], risk)}")
    pd.DataFrame(rows).to_csv(RESULTS / "asia_session.csv", index=False)


if __name__ == "__main__":
    main()
