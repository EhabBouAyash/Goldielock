#!/usr/bin/env python3
"""
General-purpose Claude Opus 5 Q&A, grounded in aggregated_prompt.txt.

Usage:
    python3 ask_claude_general.py
    python3 ask_claude_general.py "Explain the consolidation gate"

Type a question at the prompt. Enter blank / quit / exit to stop.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.getenv("ASK_CLAUDE_MAX_TOKENS", "8192"))
EFFORT = os.getenv("ASK_CLAUDE_EFFORT", "medium")
TIMEOUT_SEC = int(os.getenv("ASK_CLAUDE_TIMEOUT", "180"))
MAX_RETRIES = 4

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_AGGREGATED_PROMPT_PATH = SCRIPT_DIR / "aggregated_prompt.txt"

WRAPPER = """You are answering interactive questions grounded in the Goldielock
day-trading system prompts below (from aggregated_prompt.txt).

Use those prompts as the source of truth for rules, sizing, confluence, regime
gates, calendar handling, and signal format. Explain clearly when asked about
them. If the user asks something outside that scope, answer helpfully but note
when you are going beyond the linked prompts.

Do not invent live market prices or scheduled event times that are not in the
user message. This is Q&A, not a live trade-signal cycle — do not emit a fresh
GOLD_SIGNAL unless the user explicitly asks you to draft one from supplied data.
"""


def load_system_prompt(prompt_path: Path) -> str:
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Missing linked prompt file: {prompt_path}. "
            "Regenerate with the prompts from day_trading_insights.py."
        )
    body = prompt_path.read_text(encoding="utf-8").strip()
    if not body:
        raise ValueError(f"Linked prompt file is empty: {prompt_path}")
    return (
        f"{WRAPPER.strip()}\n\n"
        f"===== LINKED PROMPTS ({prompt_path.name}) =====\n"
        f"{body}\n"
        f"===== END LINKED PROMPTS ====="
    )


def _retry_wait_seconds(attempt: int, response: requests.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                pass
    return min(2 ** attempt, 30) + random.uniform(0.0, 1.0)


def _is_transient(status_code: int | None, body: dict) -> bool:
    if status_code in (429, 529, 503):
        return True
    err = body.get("error") or {}
    err_type = str(err.get("type") or "").lower()
    err_msg = str(err.get("message") or "").lower()
    return err_type in {"overloaded_error", "rate_limit_error", "api_error"} or (
        "overloaded" in err_msg or "rate limit" in err_msg
    )


def _extract_text(body: dict) -> str:
    blocks = body.get("content") or []
    texts = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    joined = "\n".join(t for t in texts if t).strip()
    return joined or "(Empty response text)"


def ask_opus(question: str, system_prompt: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "ERROR: Missing ANTHROPIC_API_KEY in environment (.env)."

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": MAX_TOKENS,
        "output_config": {"effort": EFFORT},
        "system": [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": question}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31",
    }

    last_error = "Unknown error"
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=TIMEOUT_SEC,
            )
            body = response.json()
        except Exception as exc:
            last_error = str(exc)
            if attempt >= MAX_RETRIES - 1:
                return f"ERROR: request failed: {exc}"
            wait_s = _retry_wait_seconds(attempt, None)
            print(f"  (retry {attempt + 1}/{MAX_RETRIES - 1} in {wait_s:.1f}s: {exc})")
            time.sleep(wait_s)
            continue

        if "error" not in body:
            usage = body.get("usage") or {}
            if usage:
                print(
                    f"  [{ANTHROPIC_MODEL}] in={usage.get('input_tokens', '?')} "
                    f"out={usage.get('output_tokens', '?')} "
                    f"stop={body.get('stop_reason', '?')}"
                )
            return _extract_text(body)

        last_error = (body.get("error") or {}).get("message", "Unknown error")
        if _is_transient(response.status_code, body) and attempt < MAX_RETRIES - 1:
            wait_s = _retry_wait_seconds(attempt, response)
            print(
                f"  (Anthropic {response.status_code}: {last_error} — "
                f"retry {attempt + 1}/{MAX_RETRIES - 1} in {wait_s:.1f}s)"
            )
            time.sleep(wait_s)
            continue
        return f"ERROR: {last_error}"

    return f"ERROR: {last_error}"


def interactive_loop(system_prompt: str, prompt_path: Path) -> None:
    print(
        f"ask_claude_general — model={ANTHROPIC_MODEL} effort={EFFORT}\n"
        f"Linked prompts: {prompt_path} "
        f"({len(system_prompt):,} system chars)"
    )
    print("Enter a question (blank / quit / exit to stop).\n")
    while True:
        try:
            question = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return
        if not question or question.lower() in {"quit", "exit", "q"}:
            print("Bye.")
            return
        print("\nClaude>")
        print(ask_opus(question, system_prompt))
        print()


def main() -> int:
    default_prompt = Path(
        os.getenv("ASK_CLAUDE_PROMPT_PATH", str(SCRIPT_DIR / "aggregated_prompt.txt"))
    )
    parser = argparse.ArgumentParser(
        description="Ask Claude Opus 5, grounded by aggregated_prompt.txt."
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="Optional one-shot question (otherwise interactive prompt)",
    )
    parser.add_argument(
        "--prompt-file",
        default=str(default_prompt),
        help="Path to aggregated prompt file (default: ./aggregated_prompt.txt)",
    )
    args = parser.parse_args()

    prompt_path = Path(args.prompt_file).expanduser().resolve()

    try:
        system_prompt = load_system_prompt(prompt_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.question:
        answer = ask_opus(" ".join(args.question), system_prompt)
        print(answer)
        return 0 if not answer.startswith("ERROR:") else 1

    interactive_loop(system_prompt, prompt_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
