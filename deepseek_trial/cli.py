"""CLI for GOLD-OCT26-v1 agents A, B, C."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from deepseek_trial.config import MODEL_NAME, PACKAGE_DIR, SPEC_VERSION, env_status
from deepseek_trial.data import gold_h4_history, gold_m5
from deepseek_trial.engine import replay_m5, save_books, summarize

AGENTS = {
    "A": {"folder": "a_h4_m15_pullback", "label": "H4 trend, M15 pullback and reclaim"},
    "B": {"folder": "b_london_sweep", "label": "London overnight sweep, reclaim, range midpoint"},
    "C": {"folder": "c_ny_range_retest", "label": "NY 09:30 range break, acceptance, retest"},
}


def run_agent(strategy_id: str, argv: list[str] | None = None) -> dict:
    meta = AGENTS[strategy_id]
    parser = argparse.ArgumentParser(description=f"{strategy_id} — {meta['label']}")
    parser.add_argument("command", nargs="?", default="replay", choices=("replay", "paper", "status"))
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--reference-only", action="store_true")
    args = parser.parse_args(argv)
    status = env_status()
    if args.command == "status":
        print(json.dumps({**status, "strategy": strategy_id}, indent=2))
        return status
    audit = (args.command == "paper" or args.audit) and not args.reference_only
    df, source = gold_m5()
    h4_hist, h4_source = gold_h4_history()
    result = replay_m5(
        df, audit=audit, source=source, strategy_filter=strategy_id,
        h4_hist=h4_hist, h4_source=h4_source,
    )
    folder = PACKAGE_DIR / meta["folder"] / "books"
    save_books(result, folder)
    books = {k: summarize(v) for k, v in result["books"].items() if k.startswith(strategy_id) or k == "combined_mech"}
    report = {
        "spec": SPEC_VERSION, "model": MODEL_NAME, "audit": audit,
        "source": source, "h4_source": h4_source,
        "n_h4": result.get("n_h4"), "warmup_h4": result.get("warmup_h4"),
        "diagnostics": result.get("diagnostics"),
        "books": books,
    }
    print(json.dumps(report, indent=2))
    return report


def run_all(audit: bool = False) -> dict:
    df, source = gold_m5()
    h4_hist, h4_source = gold_h4_history()
    result = replay_m5(
        df, audit=audit, source=source, strategy_filter=None,
        h4_hist=h4_hist, h4_source=h4_source,
    )
    save_books(result, PACKAGE_DIR / "books")
    report = {
        "spec": SPEC_VERSION,
        "source": source,
        "h4_source": h4_source,
        "audit": audit,
        "diagnostics": result.get("diagnostics"),
        "books": {k: summarize(v) for k, v in result["books"].items()},
    }
    print(json.dumps(report, indent=2))
    return report
