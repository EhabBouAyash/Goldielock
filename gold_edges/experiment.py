"""Run the edges on XAUUSD 2009-2026. Parameters chosen on 2009-2017 only.

Run from the repo root: python3 -m gold_edges
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from brooks_h2_l2.data import load_minutes
from gold_edges.core import (TRAIN_END, daily_bars, summarize, walk_positions,
                             walk_setups)
from gold_edges import sessions as sess
from gold_edges import strategies as S

RESULTS = Path(__file__).resolve().parent / "results"
STOPS = (None, 1.0, 1.5)


def _years(first, last) -> tuple[float, float]:
    return (TRAIN_END - first).days / 365.25, (last - TRAIN_END).days / 365.25


def _rows(name: str, trades: pd.DataFrame, yrs_train: float, yrs_test: float,
          extra: dict | None = None) -> list[dict]:
    if trades is None or trades.empty:
        return []
    train_m, test_m = trades["time"] < TRAIN_END, trades["time"] >= TRAIN_END
    out = []
    for period, mask, yrs in (("train", train_m, yrs_train), ("test", test_m, yrs_test)):
        st = summarize(trades[mask], yrs)
        row = {"strategy": name, "period": period, **{k: v for k, v in st.items() if k != "prop_0.25" and k != "prop_0.5"}}
        if extra:
            row.update(extra)
        for risk, key in ((0.25, "prop_0.25"), (0.50, "prop_0.5")):
            if key in st:
                p = st[key]
                row[f"ret%{risk}"] = p["return_pct"]
                row[f"dd%{risk}"] = p["max_dd_pct"]
                row[f"breach_daily%{risk}"] = p["daily_soft_breaches"]
                row[f"breach_max%{risk}"] = p["max_hard_breached"]
        out.append(row)
    return out


def daily_drift(df: pd.DataFrame) -> pd.DataFrame:
    """Open-to-close vs close-to-open, sized by prior ATR. Diagnostic, not a system."""
    from gold_edges.core import SLIPPAGE, SPREAD_BPS, atr
    O, C = df.open.to_numpy(), df.close.to_numpy()
    a = atr(df)
    rows = []
    for i in range(20, len(df) - 1):
        if not np.isfinite(a[i]) or a[i] <= 0:
            continue
        cost = C[i] * SPREAD_BPS / 1e4 + 2 * SLIPPAGE
        otc = ((C[i] - O[i]) - cost) / a[i]
        cto = ((O[i + 1] - C[i]) - cost) / a[i]
        t = pd.Timestamp(df.index[i]).tz_convert("UTC")
        rows.append({"time": t, "kind": "open_to_close", "dir": 1, "r": otc})
        rows.append({"time": t, "kind": "close_to_open", "dir": 1, "r": cto})
    return pd.DataFrame(rows)


def run_daily(df: pd.DataFrame, yrs_train: float, yrs_test: float) -> pd.DataFrame:
    rows = []
    rows += _rows("always long 21-day clips", walk_setups(df, S.clip_long(df, 21), max_hold=21),
                  yrs_train, yrs_test, {"family": "trend", "side": "long"})
    always = {
        "TSMOM 12-1": S.tsmom(df, 252, 21),
        "TSMOM 6-1": S.tsmom(df, 126, 21),
        "TSMOM 3-0": S.tsmom(df, 63, 0),
        "SMA 50/200": S.dual_ma(df, 50, 200),
        "SMA 20/50": S.dual_ma(df, 20, 50),
    }
    for name, pos in always.items():
        pos = np.nan_to_num(pos, nan=0.0)
        both = walk_positions(df, pos, stop_k=2.0)
        rows += _rows(name, both, yrs_train, yrs_test, {"family": "trend", "side": "both"})
        long_only = walk_positions(df, np.where(pos > 0, 1, 0), stop_k=2.0)
        rows += _rows(name + " long only", long_only, yrs_train, yrs_test, {"family": "trend", "side": "long"})

    setups = [
        ("Connors RSI2<5 exit>65", S.connors_rsi2(df, 5.0, 65.0), 15),
        ("Connors RSI2<10 exit>70", S.connors_rsi2(df, 10.0, 70.0), 15),
        ("5-day down streak", S.down_streak(df, 5), 5),
        ("3-day down streak", S.down_streak(df, 3), 5),
        ("Bollinger dip in uptrend", S.bollinger_reversion(df), 10),
        ("ATR dip in uptrend 1.5", S.dip_in_uptrend(df, 1.5), 10),
        ("ATR dip in uptrend 2.0", S.dip_in_uptrend(df, 2.0), 10),
        ("NR7 breakout", S.nr7_breakout(df), 2),
        ("Quiet Donchian 20", S.atr_compression_breakout(df), 20),
    ]
    for name, sig, hold in setups:
        trades = walk_setups(df, sig, valid=1, max_hold=hold)
        family = "breakout" if "NR7" in name or "Donchian" in name else "reversion"
        rows += _rows(name, trades, yrs_train, yrs_test, {"family": family, "side": "rules"})

    drift = daily_drift(df)
    for kind, g in drift.groupby("kind"):
        rows += _rows("drift " + kind, g.rename(columns={"kind": "exit_reason"}), yrs_train, yrs_test,
                      {"family": "drift", "side": "long"})
    return pd.DataFrame(rows)


def run_sessions(minutes: pd.DataFrame, yrs_train: float, yrs_test: float) -> pd.DataFrame:
    print("collecting sessions...", flush=True)
    bags = sess.all_sessions(minutes)
    rows, chosen = [], []
    for name, (_start, _end, _days, mult) in sess.SESSIONS.items():
        bag = bags[name]
        print(f"  {name}: {len(bag)} sessions", flush=True)
        for direction, side in ((1, "long"), (-1, "short")):
            best = None
            if bag.empty:
                print(f"  {name} {side}: no sessions", flush=True)
                continue
            for k in STOPS:
                trades = sess.simulate(bag, direction, k, mult)
                label = f"{name} {side} stop={k if k else 'none'}"
                rows += _rows(label, trades, yrs_train, yrs_test,
                              {"family": "session", "side": side, "session": name, "stop_k": k or 0})
                if trades.empty:
                    continue
                train = trades[trades["time"] < TRAIN_END]
                if len(train) < 80:
                    continue
                exp = float(train["r"].mean())
                if best is None or exp > best[0]:
                    best = (exp, k, trades, label)
            if best and best[0] > 0:
                chosen.append({"session": name, "side": side, "stop_k": best[1] or 0,
                               "train_exp": round(best[0], 3), "picked": best[3]})
    week = bags.get("weekend_fri16_sun18")
    if week is not None and not week.empty:
        w = sess.simulate(week, 1, None, sess.REOPEN_MULT)
        w[w.time >= TRAIN_END].to_csv(RESULTS / "trades_weekend_long_test.csv", index=False)
    return pd.DataFrame(rows), pd.DataFrame(chosen)


def select(daily: pd.DataFrame, session: pd.DataFrame, picked: pd.DataFrame) -> pd.DataFrame:
    """One test-period row per strategy, using the train-chosen session stop."""
    out = []
    d = daily[daily.period == "train"]
    for name, g in d.groupby("strategy"):
        te = daily[(daily.strategy == name) & (daily.period == "test")]
        if te.empty:
            continue
        row = te.iloc[0].to_dict()
        row["train_exp"] = g.iloc[0]["exp"]
        row["train_n"] = g.iloc[0]["n"]
        out.append(row)
    if not picked.empty:
        for rec in picked.itertuples():
            te = session[(session.strategy == rec.picked) & (session.period == "test")]
            tr = session[(session.strategy == rec.picked) & (session.period == "train")]
            if te.empty:
                continue
            row = te.iloc[0].to_dict()
            row["train_exp"] = tr.iloc[0]["exp"] if not tr.empty else rec.train_exp
            row["train_n"] = tr.iloc[0]["n"] if not tr.empty else None
            out.append(row)
    frame = pd.DataFrame(out)
    if not frame.empty:
        frame = frame.sort_values(["family", "exp"], ascending=[True, False])
    return frame


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    minutes = load_minutes()
    first, last = minutes.index[0], minutes.index[-1]
    yrs_train, yrs_test = _years(first, last)
    print(f"data {first:%Y-%m-%d} -> {last:%Y-%m-%d}; train {yrs_train:.1f}y test {yrs_test:.1f}y", flush=True)

    df = daily_bars(minutes)
    print(f"daily bars {len(df)}", flush=True)
    daily = run_daily(df, yrs_train, yrs_test)
    session, picked = run_sessions(minutes, yrs_train, yrs_test)
    daily.to_csv(RESULTS / "daily.csv", index=False)
    session.to_csv(RESULTS / "sessions.csv", index=False)
    picked.to_csv(RESULTS / "sessions_picked.csv", index=False)

    summary = select(daily, session, picked)
    cols = [c for c in ("family", "strategy", "train_n", "train_exp", "n", "per_year", "win",
                         "exp", "t", "pf", "tot_r", "maxdd_r", "long_exp", "short_exp",
                         "ret%0.25", "dd%0.25", "breach_daily%0.25", "breach_max%0.25")
            if c in summary.columns]
    summary[cols].to_csv(RESULTS / "selected.csv", index=False)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_rows", 200)
    print("\nTrain-chosen rules -> 2018-2026")
    print(summary[cols].to_string(index=False))

    # Keep the two new candidates' test trades for inspection.
    connors = walk_setups(df, S.connors_rsi2(df, 5.0, 65.0), max_hold=15)
    connors[connors.time >= TRAIN_END].to_csv(RESULTS / "trades_connors_rsi2_test.csv", index=False)


if __name__ == "__main__":
    main()
