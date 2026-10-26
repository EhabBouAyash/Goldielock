"""Decide whether a proposed gold trade is allowed to be shown as actionable.

Claude picks a direction and price levels. This module checks the clock, the
prices, the distance to the target, and the account. It does not send orders.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from decision_contract import (
    DECISIONS,
    MACRO_ROLES,
    RELATIONSHIPS,
    SIDES,
    TRIGGERS,
    legacy_signal_label,
)

MAX_PLAN_VALIDITY_SECONDS = 15 * 60
QUOTE_MAX_AGE_SECONDS = 5
DEFAULT_COST_PRICE = 0.40
DEFAULT_RESERVE_PRICE = 0.20
MIN_NET_RR = 2.0

ACTIONABLE_BLOCKERS = {
    "DATA_UNAVAILABLE",
    "ACCOUNT_UNVERIFIED",
    "MODEL_OUTPUT_INVALID",
    "STRUCTURE_UNSUPPORTED",
    "NET_RR_BELOW_MINIMUM",
    "PLAN_EXPIRED",
    "ENTRY_OUTSIDE_ZONE",
    "EVENT_RESTRICTION",
    "RISK_HEADROOM_INSUFFICIENT",
    "DUPLICATE_OPPORTUNITY",
    "PERMISSION_DENIED",
    "SNAPSHOT_MISMATCH",
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def server_trading_day(now: datetime, schedule: dict[str, Any] | None) -> str:
    """The account day ends on the broker clock, not on this computer's midnight."""
    schedule = schedule or {"timezone": "Europe/Athens", "hour": 0, "minute": 0}
    local = _as_utc(now).astimezone(ZoneInfo(str(schedule.get("timezone") or "Europe/Athens")))
    start = local.replace(
        hour=int(schedule.get("hour") or 0),
        minute=int(schedule.get("minute") or 0),
        second=0,
        microsecond=0,
    )
    if local < start:
        start -= timedelta(days=1)
    return start.date().isoformat()


def net_reward_to_risk(
    side: str,
    entry: float,
    stop: float,
    target: float,
    *,
    cost_price: float = DEFAULT_COST_PRICE,
    reserve_price: float = DEFAULT_RESERVE_PRICE,
) -> float | None:
    """Reward divided by loss, after a small cost and a small reserve.

    A picture that looks like 2-to-1 before costs can fall short after them.
    """
    if side == "LONG":
        loss = (entry - stop) + cost_price + reserve_price
        reward = (target - entry) - cost_price
    elif side == "SHORT":
        loss = (stop - entry) + cost_price + reserve_price
        reward = (entry - target) - cost_price
    else:
        return None
    if loss <= 0 or reward <= 0:
        return None
    return reward / loss


def gross_entry_boundary(side: str, stop: float, target: float) -> float | None:
    """The entry price where the picture is still 2-to-1 before costs."""
    if side == "LONG":
        return (target + 2.0 * stop) / 3.0
    if side == "SHORT":
        return (target + 2.0 * stop) / 3.0
    return None


def adverse_entry(side: str, entry_low: float, entry_high: float) -> float:
    """The worst price inside the zone. Longs pay the high; shorts sell the low."""
    return entry_high if side == "LONG" else entry_low


def loss_headroom(account: dict[str, Any]) -> float | None:
    """How many more dollars can be lost from here.

    Equity already includes the open profit or loss. We only add the further
    loss if price travels from the current price to the existing stop. We do
    not subtract the open loss a second time.
    """
    if not account.get("known"):
        return None
    equity = _finite(account.get("equity"))
    daily_start = _finite(account.get("daily_start_equity"))
    floor = _finite(account.get("equity_floor"))
    daily_hard = _finite(account.get("daily_loss_hard"))
    if None in (equity, daily_start, floor, daily_hard):
        return None
    daily_room = daily_hard + (equity - daily_start)
    overall_room = equity - floor
    extra = _finite(account.get("additional_loss_to_stops"))
    if extra is None:
        return None
    reserve = _finite(account.get("internal_reserve")) or 0.0
    return min(daily_room, overall_room) - extra - reserve


def size_position(
    allowance_usd: float,
    stop_distance: float,
    spec: dict[str, Any],
    *,
    cost_price: float = DEFAULT_COST_PRICE,
    reserve_price: float = DEFAULT_RESERVE_PRICE,
) -> dict[str, Any]:
    """Round the size down. If the smallest allowed size is still too big, refuse."""
    contract = _finite(spec.get("contract_size"))
    step = _finite(spec.get("volume_step"))
    minimum = _finite(spec.get("volume_min"))
    if None in (contract, step, minimum) or contract <= 0 or step <= 0:
        return {"lots": None, "risk_usd": None, "reason": "DATA_UNAVAILABLE"}
    loss_per_lot = (stop_distance + cost_price + reserve_price) * contract
    if loss_per_lot <= 0 or allowance_usd <= 0:
        return {"lots": 0.0, "risk_usd": 0.0, "reason": "RISK_HEADROOM_INSUFFICIENT"}
    lots = math.floor((allowance_usd / loss_per_lot) / step) * step
    lots = round(lots, 8)
    if lots + 1e-12 < minimum:
        return {
            "lots": 0.0,
            "risk_usd": round(loss_per_lot * minimum, 2),
            "reason": "RISK_HEADROOM_INSUFFICIENT",
            "minimum_lot_risk_usd": round(loss_per_lot * minimum, 2),
        }
    return {
        "lots": lots,
        "risk_usd": round(lots * loss_per_lot, 2),
        "reason": None,
        "loss_per_lot_usd": round(loss_per_lot, 2),
    }


def _quote_is_actionable(quote: dict[str, Any] | None, now: datetime) -> bool:
    if not quote or not quote.get("execution_eligible") or quote.get("is_fallback"):
        return False
    if quote.get("role") == "external_reference":
        return False
    bid = _finite(quote.get("bid"))
    ask = _finite(quote.get("ask"))
    if bid is None or ask is None or ask < bid:
        return False
    observed = quote.get("received_time") or quote.get("observation_time")
    if not observed:
        return False
    try:
        stamp = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
    except ValueError:
        return False
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    age = (_as_utc(now) - stamp.astimezone(timezone.utc)).total_seconds()
    return 0 <= age <= QUOTE_MAX_AGE_SECONDS


def _cited_ids(proposal: dict[str, Any]) -> list[str]:
    ids = [
        proposal.get("stop_evidence_id"),
        proposal.get("target_evidence_id"),
    ]
    for key in (
        "relevant_event_ids",
        "observed_response_evidence_ids",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
    ):
        values = proposal.get(key) or []
        if isinstance(values, list):
            ids.extend(values)
    return [str(item) for item in ids if item]


def validate_proposal(
    proposal: dict[str, Any] | None,
    *,
    snapshot: dict[str, Any],
    chart: dict[str, Any],
    macro: dict[str, Any],
    account: dict[str, Any],
    quote: dict[str, Any] | None,
    now: datetime,
    mode: str,
    profile: dict[str, Any],
    known_plan_expiry: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a status. Only VALIDATED_ADVISORY may be treated as a live card."""
    reasons: list[str] = []
    proposal = proposal or {}
    decision = str(proposal.get("decision") or "")
    side = str(proposal.get("side") or "")
    if decision not in DECISIONS or side not in SIDES:
        reasons.append("MODEL_OUTPUT_INVALID")
    if decision == "PROPOSE_LONG" and side != "LONG":
        reasons.append("MODEL_OUTPUT_INVALID")
    if decision == "PROPOSE_SHORT" and side != "SHORT":
        reasons.append("MODEL_OUTPUT_INVALID")
    if decision in {"WAIT", "NO_TRADE"} and side not in {"NONE", ""}:
        reasons.append("MODEL_OUTPUT_INVALID")
    if proposal.get("snapshot_id") != snapshot.get("snapshot_id"):
        reasons.append("SNAPSHOT_MISMATCH")
    if proposal.get("chart_assessment_id") != chart.get("chart_assessment_id"):
        reasons.append("SNAPSHOT_MISMATCH")
    if proposal.get("macro_assessment_id") != macro.get("macro_assessment_id"):
        reasons.append("SNAPSHOT_MISMATCH")
    if proposal.get("macro_role") not in MACRO_ROLES:
        reasons.append("MODEL_OUTPUT_INVALID")
    if proposal.get("macro_chart_relationship") not in RELATIONSHIPS:
        reasons.append("MODEL_OUTPUT_INVALID")
    if not str(proposal.get("reconciliation_summary") or "").strip():
        reasons.append("MODEL_OUTPUT_INVALID")
    if not str(proposal.get("invalidation_conditions") or "").strip():
        reasons.append("MODEL_OUTPUT_INVALID")
    if proposal.get("trigger") not in TRIGGERS:
        reasons.append("MODEL_OUTPUT_INVALID")
    if proposal.get("_sizing_authoritative_attempt") or proposal.get("sizing_authoritative"):
        reasons.append("MODEL_OUTPUT_INVALID")

    allowed = set(chart.get("evidence_ids") or []) | set(macro.get("evidence_ids") or [])
    allowed.add("")
    for evidence_id in _cited_ids(proposal):
        if evidence_id not in allowed:
            reasons.append("MODEL_OUTPUT_INVALID")
            break
    forming = set(chart.get("forming_evidence_ids") or [])
    if proposal.get("stop_evidence_id") in forming or proposal.get("target_evidence_id") in forming:
        reasons.append("STRUCTURE_UNSUPPORTED")

    snapshot_time = snapshot.get("frozen_at")
    try:
        frozen_at = datetime.fromisoformat(str(snapshot_time).replace("Z", "+00:00"))
        frozen_at = frozen_at if frozen_at.tzinfo else frozen_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        frozen_at = _as_utc(now)
        reasons.append("DATA_UNAVAILABLE")

    requested = _finite(proposal.get("requested_validity_seconds")) or 0.0
    validity = int(min(max(requested, 0.0), MAX_PLAN_VALIDITY_SECONDS))
    if decision in {"PROPOSE_LONG", "PROPOSE_SHORT"} and validity <= 0:
        reasons.append("PLAN_EXPIRED")
    expires_at = frozen_at + timedelta(seconds=validity)
    plan_id = f"{snapshot.get('snapshot_id')}:{decision}:{proposal.get('setup_family')}"
    prior_expiry = (known_plan_expiry or {}).get(plan_id)
    if prior_expiry:
        try:
            prior_dt = datetime.fromisoformat(prior_expiry.replace("Z", "+00:00"))
            prior_dt = prior_dt if prior_dt.tzinfo else prior_dt.replace(tzinfo=timezone.utc)
            if prior_dt < expires_at:
                reasons.append("DUPLICATE_OPPORTUNITY")
            expires_at = min(expires_at, prior_dt)
        except ValueError:
            reasons.append("PLAN_EXPIRED")
    if decision in {"PROPOSE_LONG", "PROPOSE_SHORT"} and _as_utc(now) > expires_at:
        reasons.append("PLAN_EXPIRED")

    entry_low = _finite(proposal.get("entry_low"))
    entry_high = _finite(proposal.get("entry_high"))
    stop = _finite(proposal.get("stop_reference"))
    target = _finite(proposal.get("target_reference"))
    net_rr = None
    sizing = None
    proposing = decision in {"PROPOSE_LONG", "PROPOSE_SHORT"} and side in {"LONG", "SHORT"}

    if proposing:
        if None in (entry_low, entry_high, stop, target) or entry_low > entry_high:
            reasons.append("MODEL_OUTPUT_INVALID")
        else:
            entry = adverse_entry(side, entry_low, entry_high)
            ordered = (
                (stop < entry < target) if side == "LONG" else (target < entry < stop)
            )
            if not ordered:
                reasons.append("STRUCTURE_UNSUPPORTED")
            boundary = gross_entry_boundary(side, stop, target)
            if boundary is None or (side == "LONG" and entry > boundary) or (
                side == "SHORT" and entry < boundary
            ):
                reasons.append("NET_RR_BELOW_MINIMUM")
            net_rr = net_reward_to_risk(side, entry, stop, target)
            if net_rr is None or net_rr < MIN_NET_RR:
                reasons.append("NET_RR_BELOW_MINIMUM")
            if not proposal.get("stop_evidence_id") or not proposal.get("target_evidence_id"):
                reasons.append("STRUCTURE_UNSUPPORTED")
            reference = None
            if quote and _finite(quote.get("ask")) and side == "LONG":
                reference = _finite(quote.get("ask"))
            elif quote and _finite(quote.get("bid")) and side == "SHORT":
                reference = _finite(quote.get("bid"))
            if (
                proposal.get("trigger") == "MARKET_NOW"
                and reference is not None
                and not (entry_low <= reference <= entry_high)
            ):
                reasons.append("ENTRY_OUTSIDE_ZONE")
            if macro.get("execution_restriction"):
                reasons.append("EVENT_RESTRICTION")

            allowance = loss_headroom(account)
            if allowance is None:
                reasons.append("ACCOUNT_UNVERIFIED")
            else:
                distance = abs(entry - stop)
                sizing = size_position(allowance, distance, account.get("symbol_spec") or {})
                if sizing.get("reason"):
                    reasons.append(sizing["reason"])
                elif proposal.get("_model_sizing_ignored"):
                    # The model's numbers are already stripped. Record that the
                    # engine size is the only size that counts.
                    sizing = dict(sizing)
                    sizing["model_sizing_ignored"] = proposal.get("_model_sizing_ignored")

    if proposing and not _quote_is_actionable(quote, now):
        reasons.append("DATA_UNAVAILABLE")

    if mode != "APPROVED_MANUAL_ADVISORY" or not profile.get("programme"):
        if proposing:
            reasons.append("PERMISSION_DENIED")
    if profile.get("automation_permissions") not in (None, "prohibited", "manual_only"):
        if proposing:
            reasons.append("PERMISSION_DENIED")
    if profile.get("order_submission"):
        reasons.append("PERMISSION_DENIED")

    # Background macro and disagreement are not automatic vetoes.
    relationship = proposal.get("macro_chart_relationship")
    model_wait_reason = None
    if decision in {"WAIT", "NO_TRADE"} and relationship == "DIVERGE":
        model_wait_reason = "MODEL_WAIT_MACRO_CHART_DIVERGENCE"

    unique_reasons = []
    for reason in reasons:
        if reason not in unique_reasons:
            unique_reasons.append(reason)

    structure_blockers = [
        reason
        for reason in unique_reasons
        if reason
        in {
            "MODEL_OUTPUT_INVALID",
            "STRUCTURE_UNSUPPORTED",
            "NET_RR_BELOW_MINIMUM",
            "PLAN_EXPIRED",
            "ENTRY_OUTSIDE_ZONE",
            "EVENT_RESTRICTION",
            "SNAPSHOT_MISMATCH",
            "DUPLICATE_OPPORTUNITY",
            "RISK_HEADROOM_INSUFFICIENT",
        }
    ]
    if decision in {"WAIT", "NO_TRADE"} and not structure_blockers:
        status = "MODEL_WAIT" if decision == "WAIT" else "MODEL_NO_TRADE"
    elif structure_blockers:
        status = "REJECTED"
    elif any(reason in unique_reasons for reason in ("ACCOUNT_UNVERIFIED", "DATA_UNAVAILABLE", "PERMISSION_DENIED")):
        status = "RESEARCH_NOTE"
    elif proposing:
        status = "VALIDATED_ADVISORY"
    else:
        status = "NON_ACTIONABLE"

    actionable = status == "VALIDATED_ADVISORY"
    return {
        "status": status,
        "reasons": unique_reasons,
        "blocking_reasons": unique_reasons if status != "VALIDATED_ADVISORY" else [],
        "model_wait_reason": model_wait_reason,
        "net_rr": None if net_rr is None else round(net_rr, 4),
        "sizing": sizing,
        "expires_at": expires_at.isoformat() if proposing else None,
        "plan_id": plan_id if proposing else None,
        "directional_thesis_potentially_valid": proposing and "EVENT_RESTRICTION" in unique_reasons
        and "STRUCTURE_UNSUPPORTED" not in unique_reasons,
        "legacy_signal": legacy_signal_label(decision, actionable=actionable),
        "model_sizing_ignored": proposal.get("_model_sizing_ignored") or None,
        "trading_day": server_trading_day(now, profile.get("server_reset_schedule")),
        "macro_role": macro.get("macro_role"),
        "execution_restricted": bool(macro.get("execution_restriction")),
    }
