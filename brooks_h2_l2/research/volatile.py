"""High-volatility regimes for H2/L2, on gold history the 2021+ study never saw.

HistData 1-minute XAUUSD from 2009 includes the 2011 spike, the 2013 crash and
the 2020 selloff. A day is "volatile" only when its trailing 20-day average
range, known at the prior close, sits in the top of the trailing year. The
cutoff is chosen on 2009-2014 and judged on 2015-2020.

Run from the repo root: python3 -m brooks_h2_l2.research.volatile
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from brooks_h2_l2.backtest import Minutes, r_stats, simulate
from brooks_h2_l2.data import load_minutes, mid_bars
from brooks_h2_l2.experiment import MAX_HOLD
from brooks_h2_l2.strategy import Config, generate_signals

RESULTS = Path(__file__).resolve().parents[1] / "results"
TRAIN_END = pd.Timestamp("2015-01-01", tz="UTC")
FRESH_END = pd.Timestamp("2021-01-01", tz="UTC")
RANKS = (0.50, 0.67, 0.75, 0.90)
# Absolute trailing range, as a percent of price. 1.3% is the 2011/2013/2020 level.
RANGES = (1.0, 1.2, 1.4, 1.6)
CONFIGS = {
    "5m + 1h filter, 1.5R": Config(timeframe="5min", htf="1h", rr=1.5, max_hold_bars=MAX_HOLD["5min"]),
    "15m London/NY, 1R": Config(timeframe="15min", htf=None, rr=1.0, session="london_ny", max_hold_bars=MAX_HOLD["15min"]),
    "1h, 1.5R": Config(timeframe="1h", htf=None, rr=1.5, max_hold_bars=MAX_HOLD["1h"]),
}


def daily_vol(minutes: pd.DataFrame) -> pd.DataFrame:
    """Trailing volatility known before each UTC day opens. No same-day prices."""
    daily = minutes.resample("1D").agg(
        bid_open=("bid_open", "first"), bid_high=("bid_high", "max"),
        bid_low=("bid_low", "min"), bid_close=("bid_close", "last"),
    ).dropna()
    prev = daily["bid_close"].shift()
    tr = pd.concat([
        daily["bid_high"] - daily["bid_low"],
        (daily["bid_high"] - prev).abs(),
        (daily["bid_low"] - prev).abs(),
    ], axis=1).max(axis=1)
    atr20 = tr.rolling(20).mean()
    atr_pct = atr20 / daily["bid_close"]
    rank = atr_pct.rolling(252, min_periods=120).rank(pct=True).shift(1)
    out = pd.DataFrame({"atr20_pct": atr_pct.shift(1), "vol_rank": rank})
    return out.dropna()


def slice_stats(trades: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    t = pd.to_datetime(trades["signal_time"])
    return r_stats(trades.loc[(t >= start) & (t < end), "r"].to_numpy())


def line(st: dict) -> str:
    if not st.get("trades"):
        return "no trades"
    return f"n={st['trades']:>4} win={st['win_rate_pct']:>5}% exp={st['expectancy_r']:+.3f}R"


def main() -> None:
    raw = load_minutes()
    vol = daily_vol(raw)
    vol.to_csv(RESULTS / "volatile_days.csv")
    yearly = (vol["atr20_pct"] * 100).groupby(vol.index.year).median().round(2)
    print("median trailing 20-day range, % of price, by year:")
    print("  " + ", ".join(f"{y}:{v}" for y, v in yearly.items()))

    minutes = Minutes(raw)
    rows = []
    for label, cfg in CONFIGS.items():
        bars = mid_bars(raw, cfg.timeframe)
        sig = generate_signals(bars, cfg)
        decision = pd.DatetimeIndex(sig["decision_time"])
        aligned = vol.reindex(vol.index.union(decision)).ffill().reindex(decision)
        rank = aligned["vol_rank"].to_numpy()
        atr = aligned["atr20_pct"].to_numpy() * 100
        base = simulate(minutes, sig, cfg)
        horizon = raw.index[-1] + pd.Timedelta(days=1)
        print(f"\n=== {label}")
        print(f"  all 2009-2014 {line(slice_stats(base, vol.index[0], TRAIN_END))}")
        print(f"  all 2015-2020 {line(slice_stats(base, TRAIN_END, FRESH_END))}")
        print(f"  all 2021-2026 {line(slice_stats(base, FRESH_END, horizon))}")
        best = None
        for kind, values, mask_of in (
            ("rank>=", RANKS, lambda v: rank >= v),
            ("range>=", RANGES, lambda v: atr >= v),
        ):
            for cut in values:
                kept = simulate(minutes, sig[mask_of(cut)], cfg)
                train = slice_stats(kept, vol.index[0], TRAIN_END)
                test = slice_stats(kept, TRAIN_END, FRESH_END)
                seen = slice_stats(kept, FRESH_END, horizon)
                print(f"  {kind}{cut:<4}  train {line(train)} | fresh {line(test)} | 2021+ {line(seen)}")
                rows.append({"config": label, "rule": f"{kind}{cut}", "train_n": train.get("trades", 0),
                             "train_win": train.get("win_rate_pct"), "train_exp": train.get("expectancy_r"),
                             "fresh_n": test.get("trades", 0), "fresh_win": test.get("win_rate_pct"),
                             "fresh_exp": test.get("expectancy_r"), "seen_n": seen.get("trades", 0),
                             "seen_win": seen.get("win_rate_pct"), "seen_exp": seen.get("expectancy_r")})
                if kind == "range>=" and train.get("trades", 0) >= 80 and (
                        best is None or train["expectancy_r"] > best[0]):
                    best = (train["expectancy_r"], cut)
        if best:
            print(f"  absolute cutoff chosen on 2009-2014: trailing range>={best[1]}%")
    pd.DataFrame(rows).to_csv(RESULTS / "volatile_grid.csv", index=False)
    pre = vol[vol.index < FRESH_END]
    wide = pre[pre["atr20_pct"] >= 0.013]
    print(f"\n2009-2020 days with trailing range >= 1.3%: {len(wide)} "
          f"({wide.index.min():%Y-%m} to {wide.index.max():%Y-%m})")


if __name__ == "__main__":
    main()
