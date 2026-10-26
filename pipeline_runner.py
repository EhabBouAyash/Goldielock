"""Glue between the market snapshot, Claude's idea, and the published card.

Chat and the scheduled monitor both have to come through here. Neither one
gets its own shortcut to an order card.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from copy import deepcopy

from broker_account_feed import read_broker_state, unknown_account
from decision_contract import proposal_from_model_text, render_published_text
from macro_relevance import admit_observation, assess_macro, label_series
from performance_ledger import append_population
from trade_plan_state import known_expiries, publish_plan
from trade_validation import server_trading_day, validate_proposal

ROOT = Path(__file__).resolve().parent
CHAT_CAN_PUBLISH = False
LEGACY_WEIGHTS = {
    "note": "Retired from the live decision. Kept only so old cycles can be compared.",
    "weights": {"macro": 0.50, "h4": 0.20, "h1": 0.12, "m15": 0.10, "m5": 0.08},
}


def pipeline_mode() -> str:
    mode = (os.getenv("GOLD_PIPELINE_MODE") or "RESEARCH").strip().upper()
    if mode not in {"RESEARCH", "PAPER", "APPROVED_MANUAL_ADVISORY"}:
        return "RESEARCH"
    return mode


def load_profile(name: str | None = None) -> dict[str, Any]:
    profile_name = name or os.getenv("GOLD_ACCOUNT_PROFILE") or "research_unverified"
    path = ROOT / "account_profiles" / f"{profile_name}.json"
    if not path.exists():
        path = ROOT / "account_profiles" / "research_unverified.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["profile_path"] = str(path)
    data["order_submission"] = False
    return data


def account_context_from_broker(broker: dict[str, Any], profile: dict[str, Any], manual_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Broker numbers are the live account. The handwritten equity column is only a note."""
    daily = (profile.get("daily_loss_rule") or {}) if profile else {}
    overall = (profile.get("overall_loss_rule") or {}) if profile else {}
    manual_equity = None if not manual_snapshot else manual_snapshot.get("current_equity_usd")
    if not broker.get("known"):
        return {
            "known": False,
            "floating_pnl": None,
            "positions": None,
            "additional_loss_to_stops": None,
            "equity": None,
            "daily_start_equity": None,
            "equity_floor": overall.get("equity_floor_usd"),
            "daily_loss_hard": daily.get("hard_usd"),
            "internal_reserve": profile.get("internal_reserves_usd"),
            "symbol_spec": None,
            "manual_equity_annotation": manual_equity,
            "manual_equity_is_broker_equity": False,
            "reason": broker.get("reason"),
            "order_submission": False,
        }
    extra = 0.0
    for position in broker.get("positions") or []:
        further = position.get("additional_loss_to_stop_usd")
        if further is None:
            return {
                "known": False,
                "floating_pnl": broker.get("floating_pnl"),
                "positions": broker.get("positions"),
                "additional_loss_to_stops": None,
                "equity": broker.get("equity"),
                "daily_start_equity": broker.get("daily_start_equity"),
                "equity_floor": overall.get("equity_floor_usd"),
                "daily_loss_hard": daily.get("hard_usd"),
                "internal_reserve": profile.get("internal_reserves_usd"),
                "symbol_spec": broker.get("symbol_spec"),
                "manual_equity_annotation": manual_equity,
                "manual_equity_is_broker_equity": False,
                "reason": "Open-trade stop distance is unknown, so exposure was not assumed to be zero.",
                "order_submission": False,
            }
        extra += float(further)
    return {
        "known": True,
        "floating_pnl": broker.get("floating_pnl"),
        "positions": broker.get("positions"),
        "additional_loss_to_stops": extra,
        "equity": broker.get("equity"),
        "daily_start_equity": broker.get("daily_start_equity"),
        "equity_floor": overall.get("equity_floor_usd"),
        "daily_loss_hard": daily.get("hard_usd"),
        "internal_reserve": profile.get("internal_reserves_usd") or 0.0,
        "symbol_spec": broker.get("symbol_spec"),
        "manual_equity_annotation": manual_equity,
        "manual_equity_is_broker_equity": False,
        "reason": None,
        "order_submission": False,
    }


def collect_account_context(manual_snapshot: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    profile = load_profile()
    use_mt5 = (os.getenv("GOLD_USE_MT5") or "").strip().lower() in {"1", "true", "yes"}
    if use_mt5:
        broker = read_broker_state(now=now)
    else:
        broker = unknown_account(
            "MT5 is left disconnected. This run prints a screen signal and does not read the broker."
        )
    context = account_context_from_broker(broker, profile, manual_snapshot)
    context["profile"] = profile
    context["broker"] = broker
    context["mode"] = pipeline_mode()
    context["trading_day"] = server_trading_day(now or datetime.now(timezone.utc), profile.get("server_reset_schedule"))
    return context


def freeze_snapshot(
    market_data: dict[str, Any],
    intraday: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    now: datetime,
    sequence: int,
) -> dict[str, Any]:
    """Copy the inputs and refuse anything timestamped after this moment."""
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    kept_events = [dict(event) for event in events if isinstance(event, dict) and admit_observation(moment, event)]
    series = []
    for name in ("DFII10", "T10YIE", "DXY", "WALCL", "WTREGEN", "DGS10", "DGS2"):
        series.append(label_series(name if name != "DXY" else "DXY"))
    digest = hashlib.sha1(f"{moment.isoformat()}|{sequence}".encode()).hexdigest()[:12]
    return {
        "snapshot_id": f"snap-{digest}",
        "frozen_at": moment.isoformat(),
        "sequence": sequence,
        "market_data": deepcopy(market_data),
        "intraday": deepcopy(intraday),
        "events": kept_events,
        "series_labels": series,
        "legacy_comparison_only": LEGACY_WEIGHTS,
        "immutable": True,
    }


def decision_addendum(snapshot: dict[str, Any], chart: dict[str, Any], macro: dict[str, Any]) -> str:
    evidence = sorted(set(chart.get("evidence_ids") or []) | set(macro.get("evidence_ids") or []))
    return "\n".join([
        "===================================================================",
        "DECISION CONTRACT INPUTS (CODE — BINDING)",
        "===================================================================",
        f"snapshot_id: {snapshot.get('snapshot_id')}",
        f"chart_assessment_id: {chart.get('chart_assessment_id')}",
        f"macro_assessment_id: {macro.get('macro_assessment_id')}",
        "Volatility regime is context. It does not by itself forbid a range or reversal idea.",
        "Do not apply Macro 50% / 4H 20% / 1H 12% / 15M 10% / 5M 8%. Those weights are retired.",
        "Do not calculate dollar risk, ounces, lots, or leverage.",
        "DFII10 is daily backdrop, not a five-minute yield change.",
        "Missing market response stays unknown. Do not invent surprise or consensus.",
        "Disagreement between the chart and the macro story needs an explanation. It is not an automatic no-trade.",
        "Background-only macro cannot by itself kill a valid chart setup.",
        "If execution risk is active, say so even when you still like the direction.",
        "schema_version must be gold-decision-1.",
        "The broker is not connected. That is not a reason for WAIT or NO_TRADE.",
        "State the chart idea: PROPOSE_LONG, PROPOSE_SHORT, WAIT, or NO_TRADE.",
        "reconciliation_summary is two or three sentences. Do not paste an order card into it.",
        "For WAIT or NO_TRADE, set recheck_above and recheck_below to the XAUUSD spot prices where a",
        "completed 15M close should trigger a fresh look. recheck_above must be above the current spot",
        "and recheck_below under it. For PROPOSE_LONG or PROPOSE_SHORT set both to 0.",
        "For PROPOSE_LONG or PROPOSE_SHORT, the idea is taken as a paper trade: entry inside the zone,",
        "then held until the stop or target is hit. Pick levels you would stand behind until one is hit.",
        "Use the supplied spot and closed-candle levels for entry, stop, and target.",
        "WAIT means the setup is unclear. NO_TRADE means the idea itself is invalid.",
        "Code prints this as a screen signal and does not send an order.",
        "Copy snapshot_id, chart_assessment_id, and macro_assessment_id exactly.",
        "Cite only these evidence ids: " + (", ".join(evidence) if evidence else "(none — then WAIT or NO_TRADE)"),
        "CHART ASSESSMENT:",
        json.dumps(chart, default=str),
        "MACRO RELEVANCE:",
        json.dumps(macro, default=str),
        "Reply with one JSON object matching the decision schema. Put the explanation in reconciliation_summary.",
    ])


def evaluate_proposal(
    model_text: str,
    *,
    snapshot: dict[str, Any],
    chart: dict[str, Any],
    macro: dict[str, Any],
    account: dict[str, Any],
    quote: dict[str, Any] | None,
    now: datetime,
    mode: str | None = None,
    profile: dict[str, Any] | None = None,
    expiry_registry: dict[str, str] | None = None,
) -> dict[str, Any]:
    parsed = proposal_from_model_text(model_text)
    if not parsed.get("ok"):
        validation = {
            "status": "NON_ACTIONABLE",
            "reasons": [parsed.get("reason") or "MODEL_OUTPUT_INVALID"],
            "blocking_reasons": [parsed.get("reason") or "MODEL_OUTPUT_INVALID"],
            "legacy_signal": "NEUTRAL",
            "sizing": None,
            "net_rr": None,
            "expires_at": None,
            "plan_id": None,
            "model_wait_reason": None,
        }
        proposal = {}
    else:
        proposal = parsed["proposal"]
        validation = validate_proposal(
            proposal,
            snapshot=snapshot,
            chart=chart,
            macro=macro,
            account=account,
            quote=quote,
            now=now,
            mode=mode or pipeline_mode(),
            profile=profile or load_profile(),
            known_plan_expiry=expiry_registry,
        )
    return {"proposal": proposal, "validation": validation, "parse": parsed}


def build_publication(
    evaluation: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    sequence: int,
    timings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = evaluation.get("validation") or {}
    proposal = evaluation.get("proposal") or {}
    publication = {
        "sequence": sequence,
        "status": validation.get("status"),
        "plan_id": validation.get("plan_id"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_time": snapshot.get("frozen_at"),
        "expires_at": validation.get("expires_at"),
        "proposal": proposal,
        "sizing": validation.get("sizing"),
        "blocking_reasons": validation.get("blocking_reasons") or [],
        "reasons": validation.get("reasons") or [],
        "net_rr": validation.get("net_rr"),
        "legacy_signal": validation.get("legacy_signal") or "NEUTRAL",
        "model_wait_reason": validation.get("model_wait_reason"),
        "macro_role": proposal.get("macro_role") or validation.get("macro_role"),
        "detail": validation.get("model_wait_reason"),
        "stage_timing": timings or {},
        "order_submission": False,
        "expiry_registry": {},
    }
    if publication["plan_id"] and publication["expires_at"]:
        publication["expiry_registry"] = {publication["plan_id"]: publication["expires_at"]}
    publication["text"] = render_published_text(publication)
    return publication


def publish_evaluation(
    publication: dict[str, Any],
    *,
    root: Path | None = None,
    prior_registry: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Save the card and file the idea in the correct result pile."""
    registry = dict(prior_registry or {})
    plan_id = publication.get("plan_id")
    if plan_id and plan_id in registry:
        publication = dict(publication)
        publication["expires_at"] = registry[plan_id]
        expiry_registry = dict(publication.get("expiry_registry") or {})
        expiry_registry[plan_id] = registry[plan_id]
        publication["expiry_registry"] = expiry_registry
        publication["text"] = render_published_text(publication)
    merged = dict(known_expiries(root))
    merged.update(publication.get("expiry_registry") or {})
    publication = dict(publication)
    publication["expiry_registry"] = merged
    result = publish_plan(publication, root=root)
    population = "raw_proposal"
    if publication.get("status") == "VALIDATED_ADVISORY":
        population = "validated_advisory"
    append_population(
        "raw_proposal",
        {"snapshot_id": publication.get("snapshot_id"), "proposal": publication.get("proposal"), "status": publication.get("status")},
        root=root,
    )
    if population == "validated_advisory":
        append_population("validated_advisory", publication, root=root)
    result["population"] = population
    result["text"] = publication.get("text")
    return result
