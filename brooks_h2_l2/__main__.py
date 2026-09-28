"""CLI: python3 -m brooks_h2_l2 {download,experiment,backtest}."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields

from . import data, experiment
from .backtest import Costs, Minutes, prop_check, r_stats, simulate
from .strategy import Config, generate_signals


def _override(cfg_pairs: list[str]) -> Config:
    types = {f.name: f.type for f in fields(Config)}
    values = {}
    for pair in cfg_pairs:
        key, _, raw = pair.partition("=")
        if key not in types:
            raise SystemExit(f"unknown Config field: {key}")
        if raw.lower() in ("none", "null"):
            values[key] = None
        elif "int" in str(types[key]):
            values[key] = int(raw)
        elif "float" in str(types[key]):
            values[key] = float(raw)
        else:
            values[key] = raw
    return Config(**values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brooks_h2_l2")
    sub = parser.add_subparsers(dest="cmd", required=True)
    dl = sub.add_parser("download", help="fetch XAUUSD 1-minute history")
    dl.add_argument("--source", choices=["histdata", "dukascopy"], default="histdata")
    dl.add_argument("--start", default="2021-01-01")
    dl.add_argument("--end")
    sub.add_parser("experiment", help="train/test grid search and report")
    bt = sub.add_parser("backtest", help="single config over all data")
    bt.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    bt.add_argument("--slippage", type=float, default=0.10)
    bt.add_argument("--commission", type=float, default=0.0)
    bt.add_argument("--trades-csv")
    args = parser.parse_args(argv)

    if args.cmd == "download":
        return data.main(["--source", args.source, "--start", args.start] + (["--end", args.end] if args.end else []))
    if args.cmd == "experiment":
        experiment.run()
        return 0
    cfg = _override(args.set)
    raw = data.load_minutes(start="2021-01-01")
    trades = simulate(Minutes(raw), generate_signals(data.mid_bars(raw, cfg.timeframe), cfg), cfg,
                      Costs(args.slippage, args.commission))
    months = (raw.index[-1] - raw.index[0]).days / 30.44
    print(json.dumps({"config": cfg.as_dict(), "stats": r_stats(trades["r"].to_numpy(), months),
                      "prop_0.5pct": prop_check(trades, 0.5)}, indent=2, default=str))
    if args.trades_csv:
        trades.to_csv(args.trades_csv, index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
