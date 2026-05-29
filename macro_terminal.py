#!/usr/bin/env python3
"""
Bloomberg-style macro terminal for gold day-trading context.

Polls macro + news on a 10-minute cadence (two ticks per 15m candle), runs a
classification pass, and maintains a tiered priority store (P1/P2/P3/expired).

Usage:
  python3 macro_terminal.py              # live terminal, refresh every 10 min
  python3 macro_terminal.py --once       # single cycle
  python3 macro_terminal.py --plain        # text output (cron-friendly)
  python3 macro_terminal.py --interval 10  # minutes between cycles
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from macro_terminal.display import print_snapshot, render_plain, render_rich
from macro_terminal.engine import MacroTerminalEngine, seconds_until_next_interval

ET = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gold macro Bloomberg-style terminal")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--plain", action="store_true", help="Plain text output (no Rich TUI)")
    parser.add_argument("--interval", type=int, default=10, help="Minutes between refresh cycles")
    parser.add_argument("--no-wait", action="store_true", help="Do not align to clock boundaries")
    return parser.parse_args()


def run_loop(engine: MacroTerminalEngine, *, interval_min: int, plain: bool, align: bool) -> None:
    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError:
        plain = True

    if plain:
        while True:
            snapshot = engine.run_cycle()
            print(render_plain(snapshot))
            print(f"\n[next refresh in {interval_min}m | {datetime.now(ET).strftime('%H:%M:%S')} ET]\n")
            if align:
                time.sleep(seconds_until_next_interval(interval_min))
            else:
                time.sleep(interval_min * 60)
        return

    console = Console()
    snapshot = engine.run_cycle()
    with Live(render_rich(snapshot), console=console, refresh_per_second=2) as live:
        while True:
            if align:
                wait = seconds_until_next_interval(interval_min)
                time.sleep(wait)
            else:
                time.sleep(interval_min * 60)
            snapshot = engine.run_cycle()
            live.update(render_rich(snapshot))


def main() -> int:
    args = parse_args()
    engine = MacroTerminalEngine()

    if args.once:
        snapshot = engine.run_cycle()
        print_snapshot(snapshot, use_rich=not args.plain)
        return 0

    if args.plain:
        try:
            run_loop(engine, interval_min=args.interval, plain=True, align=not args.no_wait)
        except KeyboardInterrupt:
            print("\n[macro_terminal] stopped")
        return 0

    try:
        run_loop(engine, interval_min=args.interval, plain=False, align=not args.no_wait)
    except KeyboardInterrupt:
        print("\n[macro_terminal] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
