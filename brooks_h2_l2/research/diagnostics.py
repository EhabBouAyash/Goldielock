"""Is the H2/L2 signal predictive at all, before costs? Compare against reversed and random entries.

Run from the repo root: python3 -m brooks_h2_l2.research.diagnostics
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from brooks_h2_l2.backtest import Costs, Minutes, r_stats, simulate
from brooks_h2_l2.data import load_minutes, mid_bars
from brooks_h2_l2.experiment import MAX_HOLD
from brooks_h2_l2.strategy import Config, generate_signals

CONFIGS = [
    Config(timeframe="5min", htf="1h", stop_mode="pullback", rr=1.5, max_hold_bars=MAX_HOLD["5min"]),
    Config(timeframe="15min", htf=None, stop_mode="pullback", rr=1.0, session="london_ny", max_hold_bars=MAX_HOLD["15min"]),
    Config(timeframe="1h", htf=None, stop_mode="pullback", rr=1.5, max_hold_bars=MAX_HOLD["1h"]),
    Config(timeframe="1h", htf=None, stop_mode="pullback", rr=2.0, max_hold_bars=MAX_HOLD["1h"]),
]


def reversed_signals(sig: pd.DataFrame) -> pd.DataFrame:
    """Same bars and risk, opposite side: sell-stop at the H2 stop level, stop at the H2 entry."""
    out = sig.copy()
    out["direction"] = -sig["direction"]
    out["entry"], out["stop"] = sig["stop"], sig["entry"]
    rr = (sig["target"] - sig["entry"]).abs() / sig["risk"]
    out["target"] = out["entry"] + out["direction"] * rr * sig["risk"]
    return out


def random_signals(bars: pd.DataFrame, sig: pd.DataFrame, cfg: Config, seed: int) -> pd.DataFrame:
    """Random bars/directions with the same count and the same risk distribution (stop-entry at bar extreme)."""
    rng = np.random.default_rng(seed)
    pick = np.sort(rng.choice(np.arange(100, len(bars) - 1), size=len(sig), replace=False))
    d = rng.choice([1, -1], size=len(sig))
    risk = rng.choice(sig["risk"].to_numpy(), size=len(sig))
    hi, lo = bars["high"].to_numpy()[pick], bars["low"].to_numpy()[pick]
    entry = np.where(d == 1, hi + cfg.tick, lo - cfg.tick)
    return pd.DataFrame({
        "decision_time": bars.index[pick] + pd.Timedelta(cfg.timeframe), "direction": d,
        "entry": entry, "stop": entry - d * risk, "target": entry + d * cfg.rr * risk, "risk": risk,
    })


def main() -> None:
    raw = load_minutes(start="2021-01-01")
    minutes = Minutes(raw)
    free = Costs(slippage=0.0)
    raw0 = raw.copy()
    for col in ("open", "high", "low", "close"):
        raw0[f"ask_{col}"] = raw0[f"bid_{col}"]
    minutes0 = Minutes(raw0)
    for cfg in CONFIGS:
        bars = mid_bars(raw, cfg.timeframe)
        sig = generate_signals(bars, cfg)
        rows = {
            "H2/L2 with costs": simulate(minutes, sig, cfg),
            "H2/L2 zero cost": simulate(minutes0, sig, cfg, free),
            "reversed, zero cost": simulate(minutes0, reversed_signals(sig), cfg, free),
        }
        rand = [simulate(minutes0, random_signals(bars, sig, cfg, s), cfg, free)["r"].mean() for s in range(20)]
        print(f"\n{cfg.timeframe} htf={cfg.htf} stop={cfg.stop_mode} {cfg.rr}R session={cfg.session}")
        for name, t in rows.items():
            st = r_stats(t["r"].to_numpy())
            print(f"  {name:22s} n={st['trades']:>5} win={st['win_rate_pct']:>5}% exp={st['expectancy_r']:+.3f}R "
                  f"CI95={st.get('expectancy_ci95')}")
        print(f"  {'random, zero cost':22s} mean exp over 20 seeds {np.mean(rand):+.3f}R "
              f"(range {min(rand):+.3f} .. {max(rand):+.3f})")


if __name__ == "__main__":
    main()
