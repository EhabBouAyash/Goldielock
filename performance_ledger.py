"""Keep different kinds of results in different piles.

A directional guess, a model idea, a checked advisory, a paper fill, a handwritten
trade, and a broker-confirmed trade are not one win rate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

POPULATIONS = (
    "directional_comparison",
    "raw_proposal",
    "validated_advisory",
    "paper_execution",
    "manual_reported",
    "broker_verified",
)


def population_path(population: str, root: Path | None = None) -> Path:
    if population not in POPULATIONS:
        raise ValueError(f"Unknown result pile: {population}")
    return (root or ROOT) / "performance_ledger" / f"{population}.jsonl"


def append_population(population: str, record: dict[str, Any], root: Path | None = None) -> Path:
    path = population_path(population, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
    return path


def _money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace("$", "").replace(",", "").replace("+", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def review_historical_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Describe old handwritten trades without inventing a missing withdrawal or breach.

    The original backlog is not edited. Corrections belong in this review only.
    """
    findings: list[dict[str, Any]] = []
    by_id = {str(row.get("TRADE_ID") or ""): row for row in rows}

    trade_024 = by_id.get("TRADE_024")
    if trade_024:
        findings.append({
            "trade_id": "TRADE_024",
            "classification": "recorded_manual_win",
            "outcome": trade_024.get("OUTCOME"),
            "pnl_usd": _money(trade_024.get("PNL_USD")),
            "model_attribution": False,
            "note": "Kept as a handwritten winning trade. That does not prove the model caused it.",
        })

    trade_010 = by_id.get("TRADE_010")
    if trade_010:
        risk = _money(trade_010.get("RISK_USD"))
        entry = _money(trade_010.get("ENTRY"))
        stop = _money(trade_010.get("STOP_LOSS"))
        target = _money(trade_010.get("TAKE_PROFIT"))
        gross_r = None
        if None not in (entry, stop, target) and entry != stop:
            risk_distance = abs(entry - stop)
            reward_distance = abs(target - entry)
            if risk_distance:
                gross_r = round(reward_distance / risk_distance, 2)
        findings.append({
            "trade_id": "TRADE_010",
            "classification": "unreliable_risk_and_expired_use",
            "risk_usd_field": risk,
            "risk_field_unreliable": risk is not None and risk < 5,
            "gross_r": gross_r,
            "timely": False,
            "note": (
                "The notes say the idea was from the prior afternoon and the fill was the next morning. "
                "A gross reward near 2.1 times the risk does not make that fill on time. "
                "The risk field is not a trustworthy dollar amount."
            ),
        })

    jumps = []
    for previous, current in zip(rows, rows[1:]):
        previous_equity = _money(previous.get("EQUITY"))
        current_equity = _money(current.get("EQUITY"))
        pnl = _money(current.get("PNL_USD"))
        if None in (previous_equity, current_equity, pnl):
            continue
        residual = round((previous_equity + pnl) - current_equity, 2)
        if abs(residual) >= 1:
            jumps.append({
                "from_trade": previous.get("TRADE_ID"),
                "to_trade": current.get("TRADE_ID"),
                "previous_equity": previous_equity,
                "pnl_usd": pnl,
                "current_equity": current_equity,
                "unexplained_residual": residual,
                "classification": "unreconciled",
                "invented_cause": None,
            })
    findings.extend(jumps)
    return {
        "original_files_modified": False,
        "invented_breach": False,
        "breach_sequence": None,
        "findings": findings,
        "unreconciled_jumps": jumps,
    }


def write_historical_review(rows: list[dict[str, str]], root: Path | None = None) -> Path:
    review = review_historical_rows(rows)
    path = (root or ROOT) / "performance_ledger" / "historical_review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review, indent=2), encoding="utf-8")
    return path
