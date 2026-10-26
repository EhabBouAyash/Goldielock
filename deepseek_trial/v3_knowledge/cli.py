"""CLI for GOLD-OCT26-v3 knowledge agents."""
from __future__ import annotations

import argparse
import json

from deepseek_trial.config import PACKAGE_DIR, V3_MODEL_NAME, env_status
from deepseek_trial.data import gold_h4_history, gold_m5
from deepseek_trial.engine import save_books, summarize
from deepseek_trial.v3_knowledge.cost import budget
from deepseek_trial.v3_knowledge.engine import V3_SPEC, replay_v3

AGENTS = {
    "A": {"folder": "v3_a_knowledge", "label": "v3 DeepSeek knowledge — H4/M15 pullback"},
    "B": {"folder": "v3_b_knowledge", "label": "v3 DeepSeek knowledge — London sweep"},
    "C": {"folder": "v3_c_knowledge", "label": "v3 DeepSeek knowledge — NY range retest"},
}


def run_agent(strategy_id: str, argv: list[str] | None = None) -> dict:
    meta = AGENTS[strategy_id]
    parser = argparse.ArgumentParser(description=meta["label"])
    parser.add_argument("command", nargs="?", default="cost", choices=("cost", "replay", "paper", "status"))
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "cost":
        report = budget()
        print(json.dumps({"agent": strategy_id, **report}, indent=2))
        return report
    if args.command == "status":
        report = {**env_status(), "strategy": strategy_id, "spec": V3_SPEC}
        print(json.dumps(report, indent=2))
        return report
    audit = args.command == "paper" or args.audit
    df, source = gold_m5()
    h4_hist, h4_source = gold_h4_history()
    result = replay_v3(
        df, audit=audit, source=source, strategy_filter=strategy_id,
        h4_hist=h4_hist, h4_source=h4_source,
    )
    folder = PACKAGE_DIR / meta["folder"] / "books"
    save_books(result, folder)
    books = {k: summarize(v) for k, v in result["books"].items()}
    report = {
        "spec": V3_SPEC,
        "model": V3_MODEL_NAME,
        "audit": audit,
        "source": source,
        "h4_source": h4_source,
        "diagnostics": result.get("diagnostics"),
        "books": books,
        "cost_note": "Run `python3 -m deepseek_trial.v3_knowledge cost` for the 30-day API table.",
    }
    print(json.dumps(report, indent=2))
    return report


def run_all(audit: bool = False) -> dict:
    df, source = gold_m5()
    h4_hist, h4_source = gold_h4_history()
    result = replay_v3(
        df, audit=audit, source=source, strategy_filter=None,
        h4_hist=h4_hist, h4_source=h4_source,
    )
    save_books(result, PACKAGE_DIR / "v3_knowledge" / "books")
    report = {
        "spec": V3_SPEC,
        "model": V3_MODEL_NAME,
        "source": source,
        "h4_source": h4_source,
        "audit": audit,
        "diagnostics": result.get("diagnostics"),
        "books": {k: summarize(v) for k, v in result["books"].items()},
    }
    print(json.dumps(report, indent=2))
    return report
