"""Train/test experiment: choose settings on the first half, report the untouched second half."""
from __future__ import annotations

import itertools
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import Costs, Minutes, breakdown, prop_check, r_stats, simulate
from .data import load_minutes, mid_bars
from .strategy import Config, generate_signals

RESULTS_DIR = Path(__file__).resolve().parent / "results"
HTF_FOR = {"5min": "1h", "15min": "1h", "1h": "4h"}
MAX_HOLD = {"5min": 48, "15min": 32, "1h": 24}  # 4h, 8h, 24h
GRID = {
    "timeframe": ["5min", "15min", "1h"],
    "use_htf": [True, False],
    "stop_mode": ["pullback", "signal_bar"],
    "rr": [1.0, 1.5, 2.0],
    "session": ["all", "london_ny"],
}
MIN_TRAIN_TRADES = 100


def grid_configs() -> list[Config]:
    configs = []
    for tf, use_htf, stop_mode, rr, session in itertools.product(*GRID.values()):
        configs.append(Config(
            timeframe=tf, htf=HTF_FOR[tf] if use_htf else None, stop_mode=stop_mode,
            rr=rr, session=session, max_hold_bars=MAX_HOLD[tf],
        ))
    return configs


def label(cfg: Config) -> str:
    return f"{cfg.timeframe} htf={cfg.htf or '-'} stop={cfg.stop_mode} {cfg.rr}R session={cfg.session}"


def _months(start: pd.Timestamp, end: pd.Timestamp) -> float:
    return max((end - start).days / 30.44, 1e-9)


def run_config(minutes: Minutes, bars: pd.DataFrame, cfg: Config, costs: Costs) -> pd.DataFrame:
    return simulate(minutes, generate_signals(bars, cfg), cfg, costs)


def evaluate(trades: pd.DataFrame, split: pd.Timestamp, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    if trades.empty:
        return {"train": {"trades": 0}, "test": {"trades": 0}}
    t = pd.to_datetime(trades["signal_time"])
    return {
        "train": r_stats(trades.loc[t < split, "r"].to_numpy(), _months(start, split)),
        "test": r_stats(trades.loc[t >= split, "r"].to_numpy(), _months(split, end)),
    }


def detail(trades: pd.DataFrame, split: pd.Timestamp, start, end) -> dict:
    t = pd.to_datetime(trades["signal_time"])
    test = trades[t >= split]
    return {
        **evaluate(trades, split, start, end),
        "test_by_direction": breakdown(test, test["direction"]),
        "test_exit_reasons": test["exit_reason"].value_counts().to_dict(),
        "by_half_year": breakdown(trades, t.dt.year.astype(str) + "H" + ((t.dt.month > 6) + 1).astype(str)),
        "test_prop_0.5pct": prop_check(test, 0.5),
        "test_prop_0.25pct": prop_check(test, 0.25),
        "median_risk_usd": round(float(trades["risk"].median()), 2),
        "median_spread_usd": round(float(trades["spread_at_fill"].median()), 3),
    }


def run(output_dir: Path = RESULTS_DIR, costs: Costs = Costs()) -> dict:
    raw = load_minutes(start="2021-01-01")
    minutes = Minutes(raw)
    start, end = raw.index[0], raw.index[-1]
    split = (start + (end - start) / 2).normalize().replace(day=1)
    bars = {tf: mid_bars(raw, tf) for tf in GRID["timeframe"]}
    print(f"data {start} -> {end}, split at {split}; bars: " + ", ".join(f"{k}={len(v)}" for k, v in bars.items()))

    rows, trades_by_label = [], {}
    for cfg in grid_configs():
        trades = run_config(minutes, bars[cfg.timeframe], cfg, costs)
        trades_by_label[label(cfg)] = (cfg, trades)
        ev = evaluate(trades, split, start, end)
        row = {"config": label(cfg), **{k: v for k, v in cfg.as_dict().items() if k in
               ("timeframe", "htf", "stop_mode", "rr", "session")}}
        for period in ("train", "test"):
            for key in ("trades", "win_rate_pct", "expectancy_r", "profit_factor", "max_drawdown_r", "trades_per_month"):
                row[f"{period}_{key}"] = ev[period].get(key)
        rows.append(row)
        print(f"{label(cfg):55s} train n={row['train_trades']:>4} win={row['train_win_rate_pct']} "
              f"exp={row['train_expectancy_r']} | test n={row['test_trades']:>4} win={row['test_win_rate_pct']} "
              f"exp={row['test_expectancy_r']}", flush=True)
    grid = pd.DataFrame(rows)

    eligible = grid[grid["train_trades"] >= MIN_TRAIN_TRADES].sort_values("train_expectancy_r", ascending=False)
    picks = {"best_train_expectancy": eligible.iloc[0]["config"]}
    wr = eligible[eligible["train_win_rate_pct"] >= 45]
    if not wr.empty:
        picks["best_train_expectancy_with_45pct_win"] = wr.iloc[0]["config"]
    picks["planned_5m_1h_filter"] = label(Config(timeframe="5min", htf="1h", max_hold_bars=MAX_HOLD["5min"]))
    picks["baseline_1h_no_filter"] = label(Config(timeframe="1h", htf=None, max_hold_bars=MAX_HOLD["1h"]))

    summary = {
        "data": {"start": str(start), "end": str(end), "split": str(split),
                 "source": "HistData XAUUSD 1m bid (spread modelled at 1.6bp of price)"},
        "costs": {"stop_slippage_usd": costs.slippage, "commission_usd": costs.commission},
        "selection_rule": f"highest train expectancy among configs with >= {MIN_TRAIN_TRADES} train trades",
        "top5_by_train": eligible.head(5)[["config", "train_trades", "train_win_rate_pct", "train_expectancy_r",
                                           "test_trades", "test_win_rate_pct", "test_expectancy_r"]].to_dict("records"),
        "test_positive_share": round(float((grid["test_expectancy_r"] > 0).mean()), 2),
        "rank_corr_train_test": round(float(grid[["train_expectancy_r", "test_expectancy_r"]]
                                            .corr(method="spearman").iloc[0, 1]), 2),
        "picks": {},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, cfg_label in picks.items():
        cfg, trades = trades_by_label[cfg_label]
        info = {"config": cfg.as_dict(), **detail(trades, split, start, end)}
        sens = {}
        for extra in (0.2, 0.4):
            alt = run_config(minutes, bars[cfg.timeframe], cfg, replace(costs, commission=extra))
            sens[f"+${extra}/oz"] = evaluate(alt, split, start, end)["test"]
        info["test_cost_sensitivity"] = sens
        summary["picks"][name] = info
        trades.to_csv(output_dir / f"trades_{name}.csv", index=False)

    grid.to_csv(output_dir / "grid.csv", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    report = format_report(summary)
    (output_dir / "report.txt").write_text(report)
    print(report)
    return summary


def _line(stats: dict) -> str:
    if not stats.get("trades"):
        return "no trades"
    ci = stats.get("expectancy_ci95")
    freq = f" ({stats['trades_per_month']}/mo)" if "trades_per_month" in stats else ""
    return (f"{stats['trades']} trades{freq} | win {stats['win_rate_pct']}% | "
            f"exp {stats['expectancy_r']:+.3f}R" + (f" CI95 [{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else "") +
            f" | PF {stats['profit_factor']} | max DD {stats['max_drawdown_r']}R | t={stats.get('t_stat')}")


def format_report(s: dict) -> str:
    out = [
        "BROOKS HIGH 2 / LOW 2 - XAUUSD",
        f"Data: {s['data']['source']}, {s['data']['start'][:10]} -> {s['data']['end'][:10]}",
        f"Train < {s['data']['split'][:10]} <= Test. Costs: spread + ${s['costs']['stop_slippage_usd']} stop slippage",
        f"Selection: {s['selection_rule']}",
        f"Grid: {s['test_positive_share']:.0%} of configs positive on test; "
        f"train/test rank correlation {s['rank_corr_train_test']}",
        "",
        "Top 5 configs by TRAIN expectancy (test shown for honesty, not used to choose):",
    ]
    for row in s["top5_by_train"]:
        out.append(f"  {row['config']}")
        out.append(f"      train n={row['train_trades']} win={row['train_win_rate_pct']}% exp={row['train_expectancy_r']:+.3f}R"
                   f" | test n={row['test_trades']} win={row['test_win_rate_pct']}% exp={row['test_expectancy_r']:+.3f}R")
    for name, info in s["picks"].items():
        cfg = info["config"]
        out += ["", f"== {name}: {cfg['timeframe']} htf={cfg['htf']} stop={cfg['stop_mode']} {cfg['rr']}R session={cfg['session']}",
                f"  train: {_line(info['train'])}",
                f"  test:  {_line(info['test'])}"]
        for d, st in info["test_by_direction"].items():
            out.append(f"  test {d:5s}: {_line(st)}")
        out.append(f"  test exits: {info['test_exit_reasons']}")
        out.append("  by half-year: " + ", ".join(
            f"{k} {v['trades']}/{v.get('win_rate_pct')}%/{v.get('expectancy_r', 0):+.2f}R" for k, v in info["by_half_year"].items()))
        for key in ("test_prop_0.5pct", "test_prop_0.25pct"):
            out.append(f"  {key}: {info[key]}")
        out.append(f"  median risk ${info['median_risk_usd']} | median spread ${info['median_spread_usd']}")
        for k, v in info["test_cost_sensitivity"].items():
            out.append(f"  test with extra commission {k}: {_line(v)}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    run()
