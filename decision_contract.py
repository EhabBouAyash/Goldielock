"""Structured trade proposal. Claude may fill this in. It is not a permission to trade."""

from __future__ import annotations

import json
import re
from typing import Any

SCHEMA_VERSION = "gold-decision-1"

DECISIONS = ("PROPOSE_LONG", "PROPOSE_SHORT", "WAIT", "NO_TRADE")
SIDES = ("LONG", "SHORT", "NONE")
TRIGGERS = ("MARKET_NOW", "LIMIT_PULLBACK", "STOP_BREAKOUT", "NONE")
MACRO_ROLES = (
    "BACKGROUND_ONLY",
    "DIRECTIONAL_UNCONFIRMED",
    "ACTIVE_REPRICING",
    "EXECUTION_RISK",
    "INSUFFICIENT_EVIDENCE",
)
RELATIONSHIPS = ("AGREE", "DIVERGE", "INDEPENDENT", "UNKNOWN")
SETUP_FAMILIES = (
    "trend_pullback_continuation",
    "compression_breakout_retest",
    "failed_breakout_range_rejection",
    "post_event_continuation",
    "none",
)

SIZING_FIELDS = (
    "risk_usd",
    "risk_dollars",
    "ounces",
    "xau_ounces",
    "lots",
    "leverage",
    "tradingview_leverage",
    "position_size",
)

# Sent to Anthropic as output_config.format.schema. Local checks still decide
# whether the idea is financially allowed. A schema-valid reply is not a trade.
DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "enum": [SCHEMA_VERSION]},
        "snapshot_id": {"type": "string"},
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "side": {"type": "string", "enum": list(SIDES)},
        "setup_family": {"type": "string", "enum": list(SETUP_FAMILIES)},
        "proposed_holding_horizon": {"type": "string"},
        "entry_low": {"type": "number"},
        "entry_high": {"type": "number"},
        "trigger": {"type": "string", "enum": list(TRIGGERS)},
        "stop_reference": {"type": "number"},
        "target_reference": {"type": "number"},
        "stop_evidence_id": {"type": "string"},
        "target_evidence_id": {"type": "string"},
        "requested_validity_seconds": {"type": "number"},
        "invalidation_conditions": {"type": "string"},
        "chart_assessment_id": {"type": "string"},
        "macro_assessment_id": {"type": "string"},
        "macro_role": {"type": "string", "enum": list(MACRO_ROLES)},
        "relevant_event_ids": {"type": "array", "items": {"type": "string"}},
        "observed_response_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "contradicting_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "macro_chart_relationship": {"type": "string", "enum": list(RELATIONSHIPS)},
        "reconciliation_summary": {"type": "string"},
        "confidence_label": {"type": "string"},
        "recheck_above": {"type": "number"},
        "recheck_below": {"type": "number"},
    },
    "required": [
        "schema_version",
        "snapshot_id",
        "decision",
        "side",
        "setup_family",
        "proposed_holding_horizon",
        "entry_low",
        "entry_high",
        "trigger",
        "stop_reference",
        "target_reference",
        "stop_evidence_id",
        "target_evidence_id",
        "requested_validity_seconds",
        "invalidation_conditions",
        "chart_assessment_id",
        "macro_assessment_id",
        "macro_role",
        "relevant_event_ids",
        "observed_response_evidence_ids",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "macro_chart_relationship",
        "reconciliation_summary",
        "confidence_label",
        "recheck_above",
        "recheck_below",
    ],
}

_ABOVE_RE = re.compile(r"\babove\s+\$?(\d{1,2},?\d{3}(?:\.\d+)?)", re.IGNORECASE)
_BELOW_RE = re.compile(r"\bbelow\s+\$?(\d{1,2},?\d{3}(?:\.\d+)?)", re.IGNORECASE)


def recheck_levels(proposal: dict[str, Any]) -> dict[str, float | None]:
    """The two spot prices that should wake the pipeline up again.

    Structured fields win. Older replies only wrote them in prose, so the
    "close above $X or below $Y" wording is read as a fallback.
    """
    def _positive(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    above = _positive(proposal.get("recheck_above"))
    below = _positive(proposal.get("recheck_below"))
    if above is None or below is None:
        text = " ".join(
            str(proposal.get(key) or "")
            for key in ("invalidation_conditions", "reconciliation_summary")
        )
        if above is None:
            match = _ABOVE_RE.search(text)
            above = float(match.group(1).replace(",", "")) if match else None
        if below is None:
            match = _BELOW_RE.search(text)
            below = float(match.group(1).replace(",", "")) if match else None
    if above is not None and below is not None and above <= below:
        above, below = None, None
    return {"above": above, "below": below}


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the last JSON object out of a model reply. Prose around it is ignored."""
    if not text or text.startswith("ERROR:"):
        return None
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    matches = re.findall(r"\{[\s\S]*\}", stripped)
    for chunk in reversed(matches):
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def model_failure_reason(text: str) -> str | None:
    """Refusals, cut-off replies, and API errors are not trade ideas."""
    if not text:
        return "MODEL_OUTPUT_INVALID"
    if text.startswith("ERROR:"):
        return "MODEL_OUTPUT_INVALID"
    lowered = text.lower()
    if "stop_reason: refusal" in lowered or '"type": "refusal"' in lowered:
        return "MODEL_OUTPUT_INVALID"
    return None


def strip_model_sizing(proposal: dict[str, Any]) -> dict[str, Any]:
    """Drop any size, dollars, or leverage the model tried to attach.

    Code calculates size later. A true `sizing_authoritative` flag is kept so
    validation can reject the attempt instead of quietly obeying it.
    """
    cleaned = dict(proposal)
    ignored = {}
    for field in SIZING_FIELDS:
        if field in cleaned and cleaned[field] not in (None, "", 0):
            ignored[field] = cleaned.pop(field)
        else:
            cleaned.pop(field, None)
    cleaned["_model_sizing_ignored"] = ignored
    cleaned["_sizing_authoritative_attempt"] = bool(proposal.get("sizing_authoritative"))
    cleaned.pop("sizing_authoritative", None)
    return cleaned


def proposal_from_model_text(text: str) -> dict[str, Any]:
    failure = model_failure_reason(text)
    if failure:
        return {"ok": False, "reason": failure, "detail": (text or "")[:500]}
    parsed = extract_json_object(text or "")
    if not parsed:
        return {"ok": False, "reason": "MODEL_OUTPUT_INVALID", "detail": "no JSON object"}
    if parsed.get("schema_version") not in (None, SCHEMA_VERSION):
        if parsed.get("decision") not in DECISIONS:
            return {
                "ok": False,
                "reason": "MODEL_OUTPUT_INVALID",
                "detail": "schema_version mismatch",
                "proposal": parsed,
            }
        parsed["schema_version"] = SCHEMA_VERSION
    return {"ok": True, "proposal": strip_model_sizing(parsed)}


def legacy_signal_label(decision: str | None, *, actionable: bool) -> str:
    """Old GOLD_SIGNAL footer, derived only after validation."""
    if not actionable:
        return "NEUTRAL"
    if decision == "PROPOSE_LONG":
        return "LONG"
    if decision == "PROPOSE_SHORT":
        return "SHORT"
    return "NEUTRAL"


def _watch_lines(proposal: dict[str, Any]) -> list[str]:
    """Pull the prices a person can act on out of a long model note."""
    blob = "\n".join(
        str(proposal.get(key) or "")
        for key in ("invalidation_conditions", "reconciliation_summary")
    )
    kept: list[str] = []
    for raw in blob.splitlines():
        line = raw.strip()
        if line.startswith(("Live spot", "LONG recheck", "SHORT recheck", "Between those")):
            kept.append(line)
    if kept:
        return kept
    note = str(proposal.get("invalidation_conditions") or "").strip()
    if note and len(note) <= 400:
        return [note]
    return []


def screen_signal_name(publication: dict[str, Any]) -> str:
    """The signal to print on a Python run. A missing broker does not turn it into no-trade."""
    status = publication.get("status") or ""
    decision = str((publication.get("proposal") or {}).get("decision") or "")
    if status in {"REJECTED", "NON_ACTIONABLE", "MODEL_NO_TRADE"}:
        return "NO TRADE"
    if status == "MODEL_WAIT" or decision == "WAIT":
        return "WAIT"
    if decision == "PROPOSE_LONG":
        return "LONG"
    if decision == "PROPOSE_SHORT":
        return "SHORT"
    if decision == "NO_TRADE":
        return "NO TRADE"
    return "NO TRADE"


def _reason_lines(proposal: dict[str, Any], signal_name: str) -> list[str]:
    """The model's own write-up, shown when the card is neutral."""
    if signal_name not in {"WAIT", "NO TRADE"}:
        return []
    thought = str(proposal.get("reconciliation_summary") or "").strip()
    confidence = str(proposal.get("confidence_label") or "").strip()
    lines = ["", "REASON"]
    if thought:
        lines.append(thought)
    else:
        lines.append("The model did not write out why it stayed neutral.")
    if confidence:
        lines.append(f"Confidence: {confidence}")
    return lines


def render_published_text(publication: dict[str, Any]) -> str:
    """Human-readable card. This is the text that gets saved and printed."""
    status = publication.get("status") or "NON_ACTIONABLE"
    active = status == "VALIDATED_ADVISORY"
    proposal = publication.get("proposal") or {}
    sizing = publication.get("sizing") or {}
    reasons = publication.get("blocking_reasons") or []
    signal_name = screen_signal_name(publication)
    entry_low = proposal.get("entry_low")
    entry_high = proposal.get("entry_high")
    stop = proposal.get("stop_reference")
    target = proposal.get("target_reference")
    has_levels = all(isinstance(value, (int, float)) and value > 0 for value in (entry_low, entry_high, stop, target))
    lines = [
        "SCREEN SIGNAL",
        f"SIGNAL: {signal_name}",
    ]
    if has_levels:
        lines.extend([
            f"Entry: {entry_low} to {entry_high}",
            f"Stop: {stop}",
            f"Target: {target}",
            f"Setup: {proposal.get('setup_family') or 'none'}",
            f"Trigger: {proposal.get('trigger') or 'none'}",
        ])
    lines.extend(_watch_lines(proposal))
    if signal_name in {"WAIT", "NO TRADE"}:
        levels = recheck_levels(proposal)
        if levels["above"] or levels["below"]:
            lines.append(
                "RECHECK: run again when a 15M candle closes "
                f"above {levels['above'] or 'n/a'} or below {levels['below'] or 'n/a'}"
            )
    lines.extend(_reason_lines(proposal, signal_name))
    lines.extend([
        "No broker is connected. No order was sent.",
        "",
        "PUBLISHED ADVISORY",
        f"Plan ID: {publication.get('plan_id') or 'none'}",
        f"Snapshot: {publication.get('snapshot_id') or 'none'}",
        f"Snapshot time: {publication.get('snapshot_time') or 'unknown'}",
        f"Expires: {publication.get('expires_at') or 'not an active plan'}",
        f"Validation status: {status}",
        f"Decision: {proposal.get('decision') or 'none'}",
        f"Side: {proposal.get('side') or 'NONE'}",
        f"Setup: {proposal.get('setup_family') or 'none'}",
        f"Permitted entry zone: {proposal.get('entry_low')} to {proposal.get('entry_high')}",
        f"Stop reference: {proposal.get('stop_reference')}",
        f"Target reference: {proposal.get('target_reference')}",
        f"Macro role: {proposal.get('macro_role') or 'unknown'}",
        f"Macro/chart relationship: {proposal.get('macro_chart_relationship') or 'unknown'}",
        f"Disagreement / reconciliation: {' | '.join(_watch_lines(proposal)) or 'none supplied'}",
        f"What would invalidate it: {proposal.get('invalidation_conditions') or 'none supplied'}",
        f"Code size (ounces-lots): {sizing.get('lots') if sizing else 'not calculated'}",
        f"Code risk allowance USD: {sizing.get('risk_usd') if sizing else 'not calculated'}",
        "Revalidation: a fresh card needs a new snapshot and a new decision. "
        "Email, restart, or retry does not extend this one.",
    ])
    if reasons:
        lines.append("Blocking reasons: " + ", ".join(str(item) for item in reasons))
    if publication.get("detail"):
        lines.append(f"Detail: {publication['detail']}")
    if status == "RESEARCH_NOTE":
        lines.append(
            "Screen signal only. The price checks passed, and the missing broker "
            "did not erase the idea. This is not an order."
        )
    elif not active:
        lines.append(
            "This is not an active recommendation. Do not place an order from this card."
        )
    else:
        lines.append(
            "Manual advisory only. The software does not send orders. "
            "Place nothing unless you re-check the price, the clock, and the account yourself."
        )
    lines.append(f"GOLD_SIGNAL={publication.get('legacy_signal') or 'NEUTRAL'}")
    return "\n".join(lines)
