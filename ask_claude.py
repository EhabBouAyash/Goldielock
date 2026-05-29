#!/usr/bin/env python3
"""
Interactive Claude chat using existing project context — no agent run.

Loads day_trading_live_data.txt, trade_backlog.txt, day_trading_insights.txt,
and related snapshots, then opens a follow-up Q&A session immediately.

Usage:
    python3 ask_claude.py
    python3 ask_claude.py --insights path/to/insights.txt
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from generate_insights import (
        MANUAL_TRADE_BACKLOG_TXT_PATH,
        MODEL_TRADE_LOG_TXT_PATH,
        _read_recent_text,
    )
from prop_account_tracker import refresh_prop_account_state
from token_tracker import calculate_cost, log_token_usage

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent
DAY_TRADING_INSIGHTS_PATH = PROJECT_DIR / "day_trading_insights.txt"
DAY_TRADING_LIVE_DATA_PATH = PROJECT_DIR / "day_trading_live_data.txt"
AGENT_STATE_PATH = PROJECT_DIR / "gold_agent_state.json"
INTRADAY_SNAPSHOT_PATH = PROJECT_DIR / "day_trading_intraday_snapshot.json"
ASK_CLAUDE_CHAT_LOG_PATH = PROJECT_DIR / "ask_claude_chat_log.jsonl"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ASK_CLAUDE_MODEL", "claude-opus-4-8")

ASK_CLAUDE_SYSTEM_PROMPT = """You are the intraday XAUUSD spot gold analyst for a FundedNext
Stellar 1-Step $25K prop account. The user opened a chat session using EXISTING files on disk —
you did NOT just run a fresh agent cycle.

Session context (loaded from disk):
- day_trading_live_data.txt — last saved market snapshot / agent user message
- trade_backlog.txt — manual execution ground truth (last row EQUITY/CUMULATIVE_PNL authoritative)
- day_trading_insights.txt — latest generated signal and tiered analysis
- Prop account snapshot — derived read-only from trade_backlog.txt

Your role:
- Answer follow-ups about the latest signal, tier scores, sizing, daily room, leverage (TV + MT5),
  disruption catalysts, and what would change bias.
- Use numbers from the supplied context only — do not invent live prices or new headlines.
- Cite trade_backlog last-row EQUITY/CUMULATIVE_PNL exactly; never re-sum PnL.
- Be direct and educational. Q&A mode — do not repeat the full signal unless asked.
- If asked to revise the signal, explain what new evidence would be required.
"""


def estimate_prompt_tokens(text: str) -> int:
    return len(text) // 4


def _read_text(path: Path, *, max_chars: int | None = None, missing_label: str = "") -> str:
    if not path.exists():
        label = missing_label or path.name
        return f"({label} not found at {path})"
    text = path.read_text(encoding="utf-8")
    if max_chars and len(text) > max_chars:
        return f"...(truncated to last {max_chars} chars)...\n{text[-max_chars:]}"
    return text


def _parse_gold_signal(insights_text: str) -> dict[str, Any]:
    match = re.search(
        r"GOLD_SIGNAL\s*=\s*\[(LONG|SHORT|NEUTRAL)\]",
        insights_text,
        flags=re.IGNORECASE,
    )
    signal = match.group(1).upper() if match else None
    confidence_match = re.search(
        r"Confidence\s*:\s*(LOW|MEDIUM|HIGH)",
        insights_text,
        flags=re.IGNORECASE,
    )
    return {
        "signal": signal,
        "confidence": confidence_match.group(1).upper() if confidence_match else None,
    }


def build_session_context(
    *,
    insights_path: Path,
    live_data_path: Path,
    backlog_path: Path,
) -> dict[str, Any]:
    prop_state = refresh_prop_account_state(backlog_path, today=datetime.now().date().isoformat())
    snapshot = prop_state.get("snapshot", {})

    agent_state_excerpt = ""
    if AGENT_STATE_PATH.exists():
        try:
            state = json.loads(AGENT_STATE_PATH.read_text(encoding="utf-8"))
            agent_state_excerpt = json.dumps(
                {
                    "cycle": state.get("cycle"),
                    "timestamp_et": (state.get("market_data") or {}).get("timestamp_et"),
                    "scout_queries": state.get("scout_queries", [])[:5],
                },
                indent=2,
                default=str,
            )
        except (json.JSONDecodeError, OSError):
            agent_state_excerpt = "(gold_agent_state.json unreadable)"

    intraday_excerpt = ""
    if INTRADAY_SNAPSHOT_PATH.exists():
        try:
            snap = json.loads(INTRADAY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
            intervals = snap.get("intervals", {})
            intraday_excerpt = json.dumps(
                {
                    "requested_symbol": snap.get("requested_symbol"),
                    "price_basis": snap.get("price_basis"),
                    "confluence_score": snap.get("confluence_score"),
                    "intervals": {
                        tf: {
                            "current_price": data.get("current_price"),
                            "rsi": data.get("rsi"),
                            "bars_count": data.get("bars_count"),
                        }
                        for tf, data in intervals.items()
                    },
                },
                indent=2,
                default=str,
            )
        except (json.JSONDecodeError, OSError):
            intraday_excerpt = "(intraday snapshot unreadable)"

    live_data = _read_text(live_data_path, max_chars=120_000)
    insights_text = _read_text(insights_path)
    backlog_table = prop_state.get("table_text") or _read_text(backlog_path, max_chars=30_000)
    snapshot_text = prop_state.get("snapshot_text", "")
    model_log = _read_recent_text(MODEL_TRADE_LOG_TXT_PATH, max_chars=6000)

    context_block = f"""
===================================================================
ASK CLAUDE SESSION CONTEXT (from disk — no fresh agent run)
===================================================================
Generated at: {datetime.now(ZoneInfo("America/New_York")).isoformat()}

--- PROP ACCOUNT SNAPSHOT (trade_backlog.txt, read-only) ---
{snapshot_text}

--- TRADE BACKLOG ---
{backlog_table}

--- LAST MARKET SNAPSHOT (day_trading_live_data.txt) ---
{live_data}

--- MODEL SIGNAL HISTORY (recent, reference only) ---
{model_log}

--- AGENT STATE (summary) ---
{agent_state_excerpt or "(not available)"}

--- INTRADAY SNAPSHOT (summary) ---
{intraday_excerpt or "(not available)"}

Use the above as your working context. The latest full signal analysis is in the next message.
"""

    return {
        "context_block": context_block.strip(),
        "insights_text": insights_text,
        "signal_data": _parse_gold_signal(insights_text),
        "snapshot": snapshot,
        "paths": {
            "insights": str(insights_path),
            "live_data": str(live_data_path),
            "backlog": str(backlog_path),
        },
    }


def call_anthropic_chat(messages: list[dict[str, str]], mode: str = "ask_claude") -> str:
    if not ANTHROPIC_API_KEY:
        return "ERROR: Missing ANTHROPIC_API_KEY in environment."

    system_tokens = estimate_prompt_tokens(ASK_CLAUDE_SYSTEM_PROMPT)
    history_tokens = sum(estimate_prompt_tokens(m.get("content", "")) for m in messages)
    print("\n[ANTHROPIC CHAT]")
    print(f"  Model: {ANTHROPIC_MODEL}")
    print(f"  Estimated input tokens: ~{system_tokens + history_tokens}")

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 4096,
                "system": [
                    {
                        "type": "text",
                        "text": ASK_CLAUDE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": messages,
            },
            timeout=120,
        )
        response_json = response.json()
    except Exception as exc:
        return f"ERROR: Anthropic request failed: {exc}"

    if "error" in response_json:
        error_msg = response_json["error"].get("message", "Unknown error")
        return f"ERROR: {error_msg}"

    if "usage" in response_json:
        usage = response_json["usage"]
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cost = calculate_cost(input_tokens + cache_creation, output_tokens)
        print(f"  Tokens in/out: {input_tokens}/{output_tokens} | Cost: ${cost:.6f}")
        log_token_usage(
            event_type="actual",
            input_tokens=input_tokens + cache_creation,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens + cache_creation + cache_read,
            cost_usd=cost,
            details={
                "model": ANTHROPIC_MODEL,
                "endpoint": "ask_claude",
                "mode": mode,
                "cache_creation": cache_creation,
                "cache_read": cache_read,
            },
        )

    if response_json.get("content"):
        return response_json["content"][0].get("text", "ERROR: Empty response text")
    return "ERROR: No content in response"


def _build_seed_messages(context_block: str, insights_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": (
                "Here is the full context from disk for this session. "
                "Treat day_trading_insights.txt (below) as your latest analysis output.\n\n"
                f"{context_block}"
            ),
        },
        {
            "role": "assistant",
            "content": insights_text
            if insights_text and not insights_text.startswith("(")
            else "(No day_trading_insights.txt content available yet.)",
        },
    ]


def _append_chat_log(record: dict[str, Any]) -> None:
    with ASK_CLAUDE_CHAT_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def run_interactive_chat(session: dict[str, Any]) -> None:
    messages = _build_seed_messages(session["context_block"], session["insights_text"])
    signal_data = session.get("signal_data") or {}
    signal = signal_data.get("signal") or "unknown"
    confidence = signal_data.get("confidence") or "n/a"
    paths = session.get("paths", {})

    print("\n" + "=" * 60)
    print("ASK CLAUDE — chat using saved context (no agent run)")
    print("=" * 60)
    print(f"Insights:  {paths.get('insights', DAY_TRADING_INSIGHTS_PATH)}")
    print(f"Live data: {paths.get('live_data', DAY_TRADING_LIVE_DATA_PATH)}")
    print(f"Backlog:   {paths.get('backlog', MANUAL_TRADE_BACKLOG_TXT_PATH)}")
    print(f"Signal:    {signal} | Confidence: {confidence}")
    print("Commands: /help  /context  /signal  /quit")
    print("Type your question and press Enter.\n")

    while True:
        try:
            user_input = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[ask_claude] Ending session.")
            break

        if not user_input:
            continue

        lowered = user_input.lower()
        if lowered in {"/quit", "/exit", "/q"}:
            print("[ask_claude] Ending session.")
            break

        if lowered == "/help":
            print(
                "\nAsk about the latest signal, tiers, sizing, daily room, leverage, or catalysts.\n"
                "  /context — reprint loaded context summary paths\n"
                "  /signal  — reprint day_trading_insights.txt\n"
                "  /quit    — exit\n"
            )
            continue

        if lowered == "/context":
            print("\n" + session["context_block"] + "\n")
            continue

        if lowered == "/signal":
            print("\n" + (session["insights_text"] or "(empty)") + "\n")
            continue

        messages.append({"role": "user", "content": user_input})
        reply = call_anthropic_chat(messages)

        if reply.startswith("ERROR:"):
            print(f"\nClaude> {reply}\n")
            messages.pop()
            continue

        messages.append({"role": "assistant", "content": reply})
        print(f"\nClaude> {reply}\n")

        _append_chat_log(
            {
                "timestamp": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                "user": user_input,
                "assistant": reply,
                "signal": signal,
            }
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chat with Claude using saved day_trading context (no agent run)."
    )
    parser.add_argument(
        "--insights",
        type=Path,
        default=DAY_TRADING_INSIGHTS_PATH,
        help="Path to latest insights file (default: day_trading_insights.txt)",
    )
    parser.add_argument(
        "--live-data",
        type=Path,
        default=DAY_TRADING_LIVE_DATA_PATH,
        help="Path to market snapshot file (default: day_trading_live_data.txt)",
    )
    parser.add_argument(
        "--backlog",
        type=Path,
        default=MANUAL_TRADE_BACKLOG_TXT_PATH,
        help="Path to trade backlog (default: trade_backlog.txt)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY in .env", file=sys.stderr)
        return 1

    print("[ask_claude] Loading context from disk...")
    session = build_session_context(
        insights_path=args.insights,
        live_data_path=args.live_data,
        backlog_path=args.backlog,
    )

    if session["insights_text"].startswith("("):
        print(
            f"WARNING: {args.insights} missing or empty — chat will still open.",
            file=sys.stderr,
        )

    run_interactive_chat(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
