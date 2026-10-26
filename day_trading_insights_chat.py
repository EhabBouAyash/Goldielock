"""Chat about the latest checked gold card.

This file does not publish its own trade. It runs the same pipeline as
day_trading_insights.py, then lets you ask questions. A chat reply cannot
extend a plan, change the size, or turn a rejected idea into an order.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CHAT_CAN_PUBLISH = False
DAY_TRADING_CHAT_LOG_PATH = Path(__file__).resolve().parent / "day_trading_chat_log.jsonl"

CHAT_FOLLOWUP_SYSTEM_PROMPT = """You are explaining a gold advisory that was already checked by code.

You may explain the chart, the news, and why the card was approved or withheld.
You cannot publish a new order card.
You cannot change the size, the stop, the target, or the expiry.
You cannot tell the user the software will place the trade.
If they want a new idea, they need a new pipeline run, not a longer chat.
""".strip()


def _append_chat_log(record: dict) -> None:
    with DAY_TRADING_CHAT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def call_chat(messages: list[dict[str, str]]) -> str:
    from day_trading_insights import call_model

    return call_model(
        CHAT_FOLLOWUP_SYSTEM_PROMPT,
        "",
        mode="interactive_chat_followup",
        messages=messages,
    )


def run_interactive_chat(insights_text: str, publication: dict | None = None) -> None:
    status = (publication or {}).get("status") or "unknown"
    print("\n" + "=" * 60)
    print("CHAT — questions only. This chat cannot publish or extend a card.")
    print("=" * 60)
    print(f"Checked status: {status}")
    print("Commands: /card  /quit\n")
    messages = [{
        "role": "user",
        "content": (
            "Explain this checked card. Do not create a new one.\n\n"
            + (insights_text or "(No card text.)")
        ),
    }]
    while True:
        try:
            user_input = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[chat] Ending session.")
            break
        if not user_input:
            continue
        lowered = user_input.lower()
        if lowered in {"/quit", "/exit", "/q"}:
            print("[chat] Ending session.")
            break
        if lowered == "/card":
            print("\n" + (insights_text or "(No card text.)") + "\n")
            continue
        messages.append({
            "role": "user",
            "content": user_input + "\nRemember: you cannot publish a trade or extend the expiry.",
        })
        reply = call_chat(messages)
        if reply.startswith("ERROR:"):
            print(f"\nClaude> {reply}\n")
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": reply})
        print(f"\nClaude> {reply}\n")
        _append_chat_log({
            "timestamp": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "user": user_input,
            "assistant": reply,
            "status": status,
            "published_by_chat": False,
        })


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ask questions about a gold card that the main pipeline already checked."
    )
    parser.add_argument(
        "--option",
        choices=("default", "trading", "agent", "chat"),
        default="chat",
    )
    parser.add_argument("--no-chat", action="store_true")
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=5.0)
    parser.add_argument("--max-cycles", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    from day_trading_insights import run_agent_cycle, run_agent_loop

    args = parse_args()
    if args.option == "agent" or args.continuous:
        run_agent_loop(
            interval_minutes=args.interval_minutes,
            trading_log_enabled=True,
            run_once=args.once,
            max_cycles=args.max_cycles,
        )
        return
    trading = args.option == "trading"
    result = run_agent_cycle(trading_log_enabled=trading, cycle=1)
    text = result.get("insights_text", "")
    print("\n" + (text or "(No card.)"))
    if not args.no_chat and args.option != "agent":
        run_interactive_chat(text, result.get("publication"))


if __name__ == "__main__":
    main()
