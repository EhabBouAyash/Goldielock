"""One published card per account. An older run cannot replace a newer one.

The expiry clock starts when the snapshot is frozen. Sending an email, restarting
the program, or retrying the model call does not give the same idea more time.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl

ROOT = Path(__file__).resolve().parent
PLAN_NAME = "gold_authoritative_plan.json"
LOCK_NAME = ".gold_cycle.lock"


class CycleBusy(RuntimeError):
    """Another cycle is already running. This one must not publish over it."""


def _paths(root: Path | None) -> tuple[Path, Path, Path]:
    base = root or ROOT
    ledger = base / "performance_ledger"
    return base / PLAN_NAME, base / LOCK_NAME, ledger / "runs.jsonl"


@contextmanager
def cycle_lock(root: Path | None = None, blocking: bool = True):
    """Keep the agent loop, chat, and the scheduled monitor from publishing together."""
    _, lock_path, _ = _paths(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError as exc:
        handle.close()
        raise CycleBusy("A gold pipeline cycle is already running.") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def cycle_busy(root: Path | None = None) -> bool:
    try:
        with cycle_lock(root, blocking=False):
            return False
    except CycleBusy:
        return True


def load_plan(root: Path | None = None) -> dict[str, Any] | None:
    path, _, _ = _paths(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _append_run(root: Path | None, record: dict[str, Any]) -> None:
    _, _, run_log = _paths(root)
    run_log.parent.mkdir(parents=True, exist_ok=True)
    with run_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def publish_plan(record: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Write the card only if this run is newer than what is already saved."""
    path, _, _ = _paths(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_plan(root)
    sequence = int(record.get("sequence") or 0)
    now = datetime.now(timezone.utc).isoformat()
    if current and int(current.get("sequence") or 0) >= sequence:
        rejected = {
            "published": False,
            "reason": "OLDER_RUN",
            "kept_sequence": current.get("sequence"),
            "attempted_sequence": sequence,
            "status": current.get("status"),
        }
        _append_run(root, {"timestamp": now, "result": "blocked_older_run", **rejected})
        return rejected

    payload = dict(record)
    payload["written_at"] = now
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)
    _append_run(
        root,
        {
            "timestamp": now,
            "result": "published",
            "sequence": sequence,
            "status": payload.get("status"),
            "plan_id": payload.get("plan_id"),
            "snapshot_id": payload.get("snapshot_id"),
        },
    )
    return {"published": True, "path": str(path), "sequence": sequence, "status": payload.get("status")}


def known_expiries(root: Path | None = None) -> dict[str, str]:
    """Plan id -> original expiry. Used so a reprint cannot move the deadline later."""
    plan = load_plan(root) or {}
    registry = dict(plan.get("expiry_registry") or {})
    plan_id = plan.get("plan_id")
    expires = plan.get("expires_at")
    if plan_id and expires:
        registry.setdefault(str(plan_id), str(expires))
    return registry
