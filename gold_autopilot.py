"""
Gold autopilot: run the pipeline, then wait for the right moment to run it again.

Phases (saved in gold_autopilot_state.json, so a restart carries on):
- RUN      call GPT-6.1 Sol through the normal pipeline once
- WATCH    after WAIT / NO TRADE: check each finished 15M candle against the two
           recheck prices the model gave; a close above the upper or below the
           lower one starts a new RUN
- PENDING  after LONG / SHORT: a paper order waits for price to reach the entry
           zone before the card expires
- OPEN     one paper trade is live until spot reaches the stop or the target;
           the pipeline does not run while it is open

Paper only. No broker is contacted and no order is sent.

Usage:
    python3 gold_autopilot.py            # run continuously
    python3 gold_autopilot.py --status   # show the current phase
    python3 gold_autopilot.py --reset    # forget a watch or a pending order (refuses with an open trade)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "gold_autopilot_state.json"
LOCK_PATH = ROOT / ".gold_autopilot.lock"
NEW_YORK = ZoneInfo("America/New_York")


def _minutes_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


POLL_SECONDS = _minutes_env("GOLD_PAPER_POLL_SECONDS", 15)
MAX_WAIT_WITH_LEVELS_MIN = _minutes_env("GOLD_RECHECK_MAX_MINUTES", 240)
WAIT_WITHOUT_LEVELS_MIN = _minutes_env("GOLD_RECHECK_FALLBACK_MINUTES", 60)
RETRY_AFTER_ERROR_MIN = 15


def log(message: str) -> None:
    stamp = datetime.now(NEW_YORK).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"[autopilot {stamp}] {message}", flush=True)


def market_open(now: datetime) -> bool:
    """Spot gold trades Sunday 6pm to Friday 5pm New York time, with a daily 5-6pm break."""
    local = now.astimezone(NEW_YORK)
    weekday, hour = local.weekday(), local.hour
    if weekday == 5:
        return False
    if weekday == 6:
        return hour >= 18
    if weekday == 4 and hour >= 17:
        return False
    return hour != 17


def next_quarter_hour(now: datetime) -> datetime:
    base = now.replace(second=0, microsecond=0)
    minutes = 15 - (base.minute % 15)
    return base + timedelta(minutes=minutes)


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"phase": "RUN"}


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(temp, STATE_PATH)


def _ledger(event: str, payload: dict[str, Any]) -> None:
    from performance_ledger import append_population

    append_population("paper_execution", {
        "event": event,
        "at": datetime.now(timezone.utc).isoformat(),
        **payload,
    })


def watch_state(levels: dict[str, float | None], reason: str, now: datetime, wait_minutes: float) -> dict[str, Any]:
    return {
        "phase": "WATCH",
        "above": levels.get("above"),
        "below": levels.get("below"),
        "reason": reason,
        "since": now.isoformat(),
        "fallback_at": (now + timedelta(minutes=wait_minutes)).isoformat(),
    }


def after_signal(result: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Decide the next phase from one pipeline result."""
    from decision_contract import recheck_levels, screen_signal_name
    from paper_trader import pending_from_publication

    publication = result.get("publication") or {}
    signal = screen_signal_name(publication)
    status = publication.get("status")

    if signal in {"LONG", "SHORT"}:
        pending = pending_from_publication(publication)
        if pending and pending.get("expires_at"):
            log(
                f"{signal} signal. Paper order waits for {pending['entry_low']}-{pending['entry_high']} "
                f"(stop {pending['stop']}, target {pending['target']}) until {pending['expires_at']}."
            )
            _ledger("signal", {"signal": signal, "pending": pending})
            return {"phase": "PENDING", "pending": pending}
        log(f"{signal} signal had unusable levels; treating it as a wait.")

    if status in {None, "NON_ACTIONABLE"} and signal == "NO TRADE":
        log(f"No usable answer this run ({publication.get('blocking_reasons')}). Retrying in {RETRY_AFTER_ERROR_MIN} minutes.")
        return watch_state({}, "retry after a failed run", now, RETRY_AFTER_ERROR_MIN)

    levels = recheck_levels(publication.get("proposal") or {})
    if levels["above"] or levels["below"]:
        log(
            f"{signal}. Watching 15M closes: above {levels['above'] or 'n/a'} or below {levels['below'] or 'n/a'} "
            f"(re-run anyway after {MAX_WAIT_WITH_LEVELS_MIN:.0f} min if neither is reached)."
        )
        return watch_state(levels, f"{signal} with recheck levels", now, MAX_WAIT_WITH_LEVELS_MIN)
    log(f"{signal} without recheck levels. Re-running in {WAIT_WITHOUT_LEVELS_MIN:.0f} minutes.")
    return watch_state(levels, f"{signal} without levels", now, WAIT_WITHOUT_LEVELS_MIN)


def run_pipeline_once() -> dict[str, Any]:
    from day_trading_insights import print_cycle_trade_details, run_agent_cycle

    result = run_agent_cycle(trading_log_enabled=True, cycle=1)
    print_cycle_trade_details(1, result.get("insights_text", ""))
    return result


def step(state: dict[str, Any]) -> dict[str, Any]:
    """Advance the autopilot by one tick and return the new state."""
    from paper_trader import check_exit, close_trade, fill_price, next_trade_id, open_trade, record_missed, spot_price

    now = datetime.now(timezone.utc)
    phase = state.get("phase", "RUN")

    if phase == "RUN":
        if not market_open(now):
            log("Gold market is closed. Waiting for it to reopen before calling the model.")
            time.sleep(300)
            return state
        log("Running the pipeline.")
        try:
            result = run_pipeline_once()
        except Exception as exc:
            log(f"Pipeline run failed: {exc}")
            return watch_state({}, f"pipeline error: {exc}", now, RETRY_AFTER_ERROR_MIN)
        return after_signal(result, datetime.now(timezone.utc))

    if phase == "WATCH":
        boundary = next_quarter_hour(now)
        time.sleep(max((boundary - now).total_seconds() + 5, 1))
        now = datetime.now(timezone.utc)
        if not market_open(now):
            return state
        quote = spot_price(now)
        if not quote:
            log("No fresh spot price at this 15M close.")
            return state
        close = quote["price"]
        above, below = state.get("above"), state.get("below")
        if above and close > above:
            log(f"15M close {close} is above {above}. Re-running the pipeline.")
            return {"phase": "RUN", "trigger": f"close {close} above {above}"}
        if below and close < below:
            log(f"15M close {close} is below {below}. Re-running the pipeline.")
            return {"phase": "RUN", "trigger": f"close {close} below {below}"}
        fallback_at = datetime.fromisoformat(state["fallback_at"])
        if now >= fallback_at:
            log("Recheck levels were not reached in time. Re-running to refresh them.")
            return {"phase": "RUN", "trigger": "timer"}
        log(f"15M close {close}. Still between {below or 'n/a'} and {above or 'n/a'}.")
        state["last_close"] = close
        return state

    if phase == "PENDING":
        pending = state["pending"]
        expires = datetime.fromisoformat(str(pending["expires_at"]).replace("Z", "+00:00"))
        quote = spot_price(now)
        price = quote["price"] if quote else None
        if price is not None:
            filled = fill_price(pending, price)
            if filled is not None:
                trade = open_trade(pending, filled, now, next_trade_id())
                log(
                    f"Paper {trade['side']} {trade['trade_id']} filled at {trade['entry']} "
                    f"({trade['ounces']:.0f} oz, risk ${trade['risk_usd']}). Stop {trade['stop']}, target {trade['target']}."
                )
                _ledger("fill", {"trade": trade})
                return {"phase": "OPEN", "trade": trade}
        if now >= expires:
            row = record_missed(pending, now, price)
            log(f"Card expired before price reached the entry zone. Logged {row['TRADE_ID']} as MISSED_UNFILLED.")
            _ledger("missed", {"pending": pending, "last_price": price})
            return {"phase": "RUN", "trigger": "entry expired"}
        time.sleep(POLL_SECONDS)
        return state

    if phase == "OPEN":
        trade = state["trade"]
        quote = spot_price(now)
        if quote:
            hit = check_exit(trade, quote["price"])
            if hit:
                reason, exit_price = hit
                row = close_trade(trade, reason, exit_price, now)
                log(
                    f"Paper {trade['trade_id']} closed: {reason} at {exit_price} -> {row['OUTCOME']} "
                    f"${row['PNL_USD']} ({row['R_MULTIPLE']}R). Written to trade_backlog.csv."
                )
                _ledger("close", {"trade": trade, "row": row})
                return {"phase": "RUN", "trigger": f"trade closed {reason}"}
            if int(now.timestamp()) % 300 < POLL_SECONDS:
                log(f"{trade['trade_id']} open: spot {quote['price']} (stop {trade['stop']}, target {trade['target']}).")
        time.sleep(POLL_SECONDS)
        return state

    log(f"Unknown phase {phase!r}; starting over.")
    return {"phase": "RUN"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-trading autopilot for the gold pipeline.")
    parser.add_argument("--status", action="store_true", help="Print the saved phase and exit.")
    parser.add_argument("--reset", action="store_true", help="Clear a watch or pending order. Refuses while a trade is open.")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(load_state(), indent=2))
        return
    if args.reset:
        state = load_state()
        if state.get("phase") == "OPEN":
            print("A paper trade is open. Let it reach its stop or target first.")
            sys.exit(1)
        save_state({"phase": "RUN"})
        print("Autopilot reset. The next start runs the pipeline.")
        return

    lock = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another autopilot is already running.")
        sys.exit(1)

    state = load_state()
    log(f"Starting in phase {state.get('phase')}. Paper only; no broker is contacted.")
    while True:
        try:
            new_state = step(state)
        except KeyboardInterrupt:
            log("Stopped. State is saved; the next start carries on.")
            return
        if new_state is not state or new_state.get("phase") != state.get("phase"):
            save_state(new_state)
        elif new_state.get("last_close") is not None:
            save_state(new_state)
        state = new_state


if __name__ == "__main__":
    main()
