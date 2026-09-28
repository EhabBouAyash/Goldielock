"""Shared CLI for the three paper agents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from deepseek_trial.config import BTCUSD, MODEL_NAME, XAUUSD, env_status
from deepseek_trial.data import btc_daily_utc, gold_daily_et, gold_hourly
from deepseek_trial.paper import replay, save_book, summarize
from deepseek_trial.t1_gold_trend.strategy import EXPERIMENTAL as T1_EXP
from deepseek_trial.t1_gold_trend.strategy import STRATEGY_ID as T1
from deepseek_trial.t2_gold_compression.strategy import EXPERIMENTAL as T2_EXP
from deepseek_trial.t2_gold_compression.strategy import STRATEGY_ID as T2
from deepseek_trial.t3_btc_ma.strategy import EXPERIMENTAL as T3_EXP
from deepseek_trial.t3_btc_ma.strategy import STRATEGY_ID as T3

AGENTS = {
    T1: {
        "folder": "t1_gold_trend",
        "instrument": "XAUUSD",
        "spec": XAUUSD,
        "experimental": T1_EXP,
        "load": gold_daily_et,
        "label": "Gold daily 12-1 + 200-day, long or cash",
    },
    T2: {
        "folder": "t2_gold_compression",
        "instrument": "XAUUSD",
        "spec": XAUUSD,
        "experimental": T2_EXP,
        "load": gold_hourly,
        "label": "Gold H1 compression breakout (EXPERIMENTAL)",
    },
    T3: {
        "folder": "t3_btc_ma",
        "instrument": "BTCUSD",
        "spec": BTCUSD,
        "experimental": T3_EXP,
        "load": btc_daily_utc,
        "label": "Bitcoin daily 5/100, long or cash",
    },
}


def _books_dir(strategy_id: str) -> Path:
    return Path(__file__).resolve().parent / AGENTS[strategy_id]["folder"] / "books"


def run_agent(strategy_id: str, argv: list[str] | None = None) -> dict:
    meta = AGENTS[strategy_id]
    parser = argparse.ArgumentParser(description=f"{strategy_id} paper agent — {meta['label']}")
    parser.add_argument("command", nargs="?", default="replay", choices=("replay", "paper", "status"))
    parser.add_argument("--audit", action="store_true", help="DeepSeek-supervised book (entries only)")
    parser.add_argument("--reference-only", action="store_true", help="Skip the model even if --audit")
    args = parser.parse_args(argv)

    status = env_status()
    if args.command == "status":
        print(json.dumps({**status, "strategy_id": strategy_id, "model": MODEL_NAME}, indent=2))
        return status

    audit = bool(args.audit and not args.reference_only and args.command in ("replay", "paper"))
    if args.command == "paper":
        audit = True and not args.reference_only

    df, source = meta["load"]()
    book = replay(
        df,
        strategy_id=strategy_id,
        spec=meta["spec"],
        instrument=meta["instrument"],
        experimental=meta["experimental"],
        audit=audit,
        source=source,
    )
    out = _books_dir(strategy_id) / f"{book.book_name}.json"
    save_book(book, out)
    summary = summarize(book)
    summary["env"] = {
        "env_path": status["env_path"],
        "key_present": status["key_present"],
        "model": MODEL_NAME,
        "audit": audit,
    }
    summary_path = _books_dir(strategy_id) / f"{book.book_name}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


def run_all(audit: bool = False) -> dict:
    reports = {}
    for sid in (T1, T2, T3):
        argv = ["replay"]
        if audit:
            argv.append("--audit")
        reports[sid] = run_agent(sid, argv)
    return reports
