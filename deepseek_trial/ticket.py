"""Version 2.0 decision contract. Engine computes tickets; the model may only copy them."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from deepseek_trial.config import SPEC_VERSION

ACTIONS = frozenset({"NO_TRADE", "HOLD", "OPEN", "CLOSE", "AMEND_STOP"})
ORDER_TYPES = frozenset({"NONE", "MARKET"})
SIDES = frozenset({"NONE", "BUY", "SELL"})
REASON_CODES = frozenset({
    "NO_SIGNAL", "VALID_SIGNAL", "SIGNAL_EXIT", "STOP_UPDATE", "DATA_MISSING",
    "STALE_QUOTE", "WINDOW_CLOSED", "COST_TOO_HIGH", "RISK_LIMIT", "MARGIN_LIMIT",
    "LOT_TOO_SMALL", "STATE_MISMATCH", "INVALID_OUTPUT", "TIMEOUT",
    "WEEKEND_CLIP", "NOT_QUIET", "STOP_HIT", "TIME_EXIT", "WEEKEND_EXIT",
    "KILL_SWITCH", "SPEC_VIOLATION",
})
STRATEGY_IDS = frozenset({"T1", "T2", "T3"})

TICKET_FIELDS = (
    "spec_version", "strategy_id", "event_id", "snapshot_hash",
    "decision_time_utc", "action", "order_type", "side", "entry_reference",
    "stop_loss", "take_profit", "volume_lots", "expires_at_utc", "reason_code",
)


@dataclass
class Ticket:
    spec_version: str
    strategy_id: str
    event_id: str
    snapshot_hash: str
    decision_time_utc: str
    action: str
    order_type: str
    side: str
    entry_reference: float | None
    stop_loss: float | None
    take_profit: float | None
    volume_lots: float
    expires_at_utc: str | None
    reason_code: str

    def to_json_dict(self) -> dict[str, Any]:
        return {k: asdict(self)[k] for k in TICKET_FIELDS}


def snapshot_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def idle_ticket(
    strategy_id: str,
    event_id: str,
    decision_time_utc: str,
    reason_code: str,
    snap: str = "",
    action: str = "NO_TRADE",
) -> Ticket:
    return Ticket(
        spec_version=SPEC_VERSION,
        strategy_id=strategy_id,
        event_id=event_id,
        snapshot_hash=snap or "NONE",
        decision_time_utc=decision_time_utc,
        action=action,
        order_type="NONE",
        side="NONE",
        entry_reference=None,
        stop_loss=None,
        take_profit=None,
        volume_lots=0.0,
        expires_at_utc=None,
        reason_code=reason_code,
    )


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np_isfinite(number):
        return None
    return number


def np_isfinite(number: float) -> bool:
    return number == number and number not in (float("inf"), float("-inf"))


def parse_ticket(raw: dict[str, Any]) -> tuple[Ticket | None, str]:
    if not isinstance(raw, dict):
        return None, "INVALID_OUTPUT"
    missing = [k for k in TICKET_FIELDS if k not in raw]
    if missing:
        return None, "INVALID_OUTPUT"
    try:
        ticket = Ticket(
            spec_version=str(raw["spec_version"]),
            strategy_id=str(raw["strategy_id"]),
            event_id=str(raw["event_id"]),
            snapshot_hash=str(raw["snapshot_hash"]),
            decision_time_utc=str(raw["decision_time_utc"]),
            action=str(raw["action"]),
            order_type=str(raw["order_type"]),
            side=str(raw["side"]),
            entry_reference=_finite_or_none(raw["entry_reference"]),
            stop_loss=_finite_or_none(raw["stop_loss"]),
            take_profit=_finite_or_none(raw["take_profit"]),
            volume_lots=float(raw["volume_lots"] or 0),
            expires_at_utc=None if raw["expires_at_utc"] is None else str(raw["expires_at_utc"]),
            reason_code=str(raw["reason_code"]),
        )
    except (TypeError, ValueError):
        return None, "INVALID_OUTPUT"
    err = validate_ticket(ticket)
    return (ticket, "") if not err else (None, err)


def validate_ticket(ticket: Ticket) -> str:
    if ticket.spec_version != SPEC_VERSION:
        return "INVALID_OUTPUT"
    if ticket.strategy_id not in STRATEGY_IDS:
        return "INVALID_OUTPUT"
    if ticket.action not in ACTIONS or ticket.order_type not in ORDER_TYPES:
        return "INVALID_OUTPUT"
    if ticket.side not in SIDES or ticket.reason_code not in REASON_CODES:
        return "INVALID_OUTPUT"
    if ticket.take_profit is not None:
        return "INVALID_OUTPUT"
    if ticket.action in ("NO_TRADE", "HOLD"):
        if ticket.order_type != "NONE" or ticket.side != "NONE" or ticket.volume_lots != 0:
            return "INVALID_OUTPUT"
        if ticket.stop_loss is not None or ticket.entry_reference is not None:
            return "INVALID_OUTPUT"
        return ""
    if ticket.action == "OPEN":
        if ticket.order_type != "MARKET" or ticket.side not in ("BUY", "SELL"):
            return "INVALID_OUTPUT"
        if ticket.strategy_id in ("T1", "T3") and ticket.side != "BUY":
            return "INVALID_OUTPUT"
        if ticket.volume_lots <= 0 or ticket.stop_loss is None:
            return "INVALID_OUTPUT"
        if ticket.expires_at_utc is None:
            return "INVALID_OUTPUT"
        return ""
    if ticket.action == "CLOSE":
        if ticket.side not in ("BUY", "SELL") or ticket.order_type != "NONE":
            return "INVALID_OUTPUT"
        if ticket.volume_lots <= 0:
            return "INVALID_OUTPUT"
        if ticket.entry_reference is not None or ticket.stop_loss is not None:
            return "INVALID_OUTPUT"
        return ""
    if ticket.action == "AMEND_STOP":
        if ticket.side not in ("BUY", "SELL") or ticket.stop_loss is None:
            return "INVALID_OUTPUT"
        if ticket.order_type != "NONE" or ticket.volume_lots != 0:
            return "INVALID_OUTPUT"
        return ""
    return "INVALID_OUTPUT"


def tickets_match(engine: Ticket, model: Ticket, tick: float) -> bool:
    if engine.action != model.action or engine.side != model.side:
        return False
    if engine.strategy_id != model.strategy_id:
        return False
    if engine.action == "OPEN":
        if engine.order_type != model.order_type:
            return False
        if abs(engine.volume_lots - model.volume_lots) > 1e-12:
            return False
        if engine.stop_loss is None or model.stop_loss is None:
            return False
        if abs(engine.stop_loss - model.stop_loss) > tick / 2:
            return False
    return True
