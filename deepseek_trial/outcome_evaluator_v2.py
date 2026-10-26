"""Signals and trade results are stored apart. Comparing two signals is not a result.

A signal row says a setup was born, rejected, or left to expire. An outcome row
is written once, after a fill has a real exit. Rows are append-only.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


TERMINAL = {"SIGNALLED", "REJECTED", "EXPIRED"}


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(value.tzinfo).isoformat()
    return "" if value is None else str(value)


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.signals: list[dict] = []
        self.outcomes: list[dict] = []
        if path is not None and path.is_file():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("record_type") == "outcome":
                    self.outcomes.append(row)
                else:
                    self.signals.append(row)

    def get(self, setup_id: str) -> dict | None:
        found = [row for row in self.signals if row.get("setup_id") == setup_id]
        return found[-1] if found else None

    def has_setup(self, setup_id: str) -> bool:
        return self.get(setup_id) is not None

    def append_signal(self, row: dict) -> dict:
        setup_id = row.get("setup_id") or ""
        existing = self.get(setup_id) if setup_id else None
        if existing and existing.get("state") in TERMINAL:
            raise ValueError(f"setup already closed: {setup_id}")
        stored = {"record_type": "signal", **row}
        self.signals.append(stored)
        self._write(stored)
        return stored

    def append_outcome(self, row: dict) -> dict:
        setup_id = row["setup_id"]
        if any(item.get("setup_id") == setup_id for item in self.outcomes):
            raise ValueError(f"outcome already recorded: {setup_id}")
        stored = {"record_type": "outcome", **row}
        self.outcomes.append(stored)
        self._write(stored)
        return stored

    def _write(self, row: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(row, default=_iso) + "\n")


def compare_signals(_left: dict, _right: dict) -> dict:
    """Two signal notes do not tell you whether a trade made money."""
    return {
        "valid_trade_evaluation": False,
        "reason": "Comparing this signal with the previous signal does not measure profit, cost, or drawdown.",
    }


def payout_eligible(pnl: float, violations: list[str]) -> float:
    """Losses always count. Gains inside a restricted window do not count as payout profit."""
    if pnl <= 0:
        return pnl
    blocked = {"NEWS_WINDOW_PROFIT", "QUICK_STRIKE"}
    if blocked.intersection(violations):
        return 0.0
    return pnl
