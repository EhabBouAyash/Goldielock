"""Sort macro news into background, direction, and 'do not trade right now'.

Headlines do not get a vote that forces a buy or a sell. Repeating the same
story does not make it more convincing. A daily yield print is not a 5-minute signal.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

DAILY_SERIES = ("DFII10", "T10YIE", "WALCL", "WTREGEN", "DGS10", "DGS2")
YIELD_COMPLEX = ("DFII10", "T10YIE", "DGS10", "DGS2")
SLOW_CONTEXT = ("WALCL", "WTREGEN")

TRANSMISSION_TERMS = (
    "fed",
    "fomc",
    "powell",
    "yield",
    "dollar",
    "dxy",
    "inflation",
    "cpi",
    "pce",
    "nfp",
    "payroll",
    "war",
    "sanction",
    "gold",
    "xau",
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def event_id_for(title: str, day: str) -> str:
    digest = hashlib.sha1(f"{day}|{_norm(title)}".encode()).hexdigest()[:12]
    return f"evt-{digest}"


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # Forex Factory feed: "10-16-2026 3:30am", published in UTC.
        try:
            parsed = datetime.strptime(text.lower().replace(" ", ""), "%m-%d-%Y%I:%M%p")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def label_series(name: str) -> dict[str, Any]:
    """Say what a data series is allowed to mean. DFII10 stays a daily backdrop."""
    upper = (name or "").upper()
    if upper == "DFII10":
        role = "BACKGROUND_ONLY"
        frequency = "daily"
        note = "Dated real-yield backdrop. Not a live five-minute yield change."
    elif upper in YIELD_COMPLEX or upper == "DXY":
        role = "BACKGROUND_ONLY"
        frequency = "daily" if upper in DAILY_SERIES else "as_supplied"
        note = "Use only at the frequency that was actually supplied."
    elif upper in SLOW_CONTEXT:
        role = "BACKGROUND_ONLY"
        frequency = "weekly_or_slower"
        note = "Slow balance-sheet context. Not an intraday trigger by itself."
    else:
        role = "INSUFFICIENT_EVIDENCE"
        frequency = "unknown"
        note = "Frequency was not labelled."
    return {
        "series": upper,
        "intended_role": role,
        "frequency": frequency,
        "may_confirm_five_minute_entry": False,
        "note": note,
    }


def canonicalize_articles(articles: list[dict[str, Any]], received_time: str) -> list[dict[str, Any]]:
    """Give each story one id. A reprint points at the original and adds no weight."""
    seen: dict[str, str] = {}
    output: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        published = str(article.get("published_at") or article.get("observation_time") or "")
        day = published[:10]
        title = str(article.get("title") or "")
        eid = str(article.get("event_id") or event_id_for(title, day))
        copy = dict(article)
        copy["event_id"] = eid
        copy["observation_time"] = published or None
        copy["publication_time"] = published or None
        copy["received_time"] = received_time
        copy["frequency"] = copy.get("frequency") or "headline"
        copy["source"] = copy.get("source") or "unknown"
        copy["revision_status"] = copy.get("revision_status") or "unverified"
        copy["observed_response"] = copy.get("observed_response") or "unknown"
        text = _norm(f"{title} {copy.get('description') or ''} {copy.get('category') or ''}")
        copy["has_transmission_path"] = any(term in text for term in TRANSMISSION_TERMS)
        copy["can_force_direction"] = False
        if eid in seen:
            copy["duplicate_of"] = seen[eid]
            copy["counts_as_new_evidence"] = False
        else:
            seen[eid] = eid
            copy["duplicate_of"] = None
            copy["counts_as_new_evidence"] = True
        output.append(copy)
    return output


def admit_observation(snapshot_time: datetime, observation: dict[str, Any]) -> bool:
    """A later revision or a future print cannot be written into an earlier snapshot."""
    for key in ("observation_time", "publication_time", "received_time"):
        stamp = _parse_time(observation.get(key))
        if stamp is not None and stamp > snapshot_time:
            return False
    return True


def reactivation_allowed(prior: dict[str, Any] | None, new_item: dict[str, Any]) -> bool:
    """The same headline printed again is not new evidence."""
    if not prior:
        return True
    if not new_item.get("counts_as_new_evidence", True):
        return False
    prior_time = _parse_time(prior.get("observation_time"))
    new_time = _parse_time(new_item.get("observation_time"))
    if prior_time and new_time and new_time <= prior_time:
        return False
    if new_item.get("event_id") == prior.get("event_id") and not new_item.get("new_evidence_note"):
        return False
    return bool(new_item.get("new_evidence_note"))


def assess_macro(
    headlines: list[dict[str, Any]],
    calendar_events: list[dict[str, Any]] | None,
    series_names: list[str],
    *,
    now: datetime,
    received_time: str,
    restriction_window_minutes: int = 30,
) -> dict[str, Any]:
    """Classify evidence. This function never picks long or short."""
    kept = [
        item
        for item in canonicalize_articles(headlines, received_time)
        if admit_observation(now, item)
    ]
    unique = [item for item in kept if item.get("counts_as_new_evidence")]
    classified = []
    for item in kept:
        if item.get("observed_response") in (None, "", "unknown"):
            role = "INSUFFICIENT_EVIDENCE"
            response = "unknown"
        elif item.get("has_transmission_path"):
            role = "DIRECTIONAL_UNCONFIRMED"
            response = item.get("observed_response")
        else:
            role = "BACKGROUND_ONLY"
            response = "unknown"
        classified.append({
            "event_id": item.get("event_id"),
            "title": item.get("title"),
            "role": role,
            "observed_response": response,
            "counts_as_new_evidence": bool(item.get("counts_as_new_evidence")),
            "duplicate_of": item.get("duplicate_of"),
            "can_force_direction": False,
            "source": item.get("source"),
            "observation_time": item.get("observation_time"),
        })

    execution_events = []
    for event in calendar_events or []:
        if not isinstance(event, dict):
            continue
        if not admit_observation(now, event):
            continue
        impact = str(event.get("impact") or event.get("importance") or "").lower()
        when = _parse_time(event.get("event_time") or event.get("published_at"))
        minutes = None if when is None else abs((when - now).total_seconds()) / 60.0
        near = minutes is not None and minutes <= restriction_window_minutes
        high = impact in {"high", "red"} or bool(event.get("high_impact"))
        if high and near:
            execution_events.append(event.get("event_id") or event.get("title") or "calendar-event")

    series = [label_series(name) for name in series_names]
    yield_ids = [item["series"] for item in series if item["series"] in YIELD_COMPLEX]
    execution_restriction = bool(execution_events)
    if execution_restriction:
        headline_role = "EXECUTION_RISK"
    elif any(item["role"] == "DIRECTIONAL_UNCONFIRMED" for item in classified if item["counts_as_new_evidence"]):
        headline_role = "DIRECTIONAL_UNCONFIRMED"
    elif classified or series:
        headline_role = "BACKGROUND_ONLY"
    else:
        headline_role = "INSUFFICIENT_EVIDENCE"

    digest = hashlib.sha1(received_time.encode()).hexdigest()[:10]
    return {
        "macro_assessment_id": f"macro-{digest}",
        "assessed_at": now.isoformat(),
        "direction": None,
        "macro_role": headline_role,
        "execution_restriction": execution_restriction,
        "execution_event_ids": execution_events,
        "items": classified,
        "unique_event_count": len(unique),
        "repeated_event_count": len(kept) - len(unique),
        "series": series,
        "related_groups": [
            {
                "group": "yield_complex",
                "series": yield_ids,
                "counts_as_independent_votes": False,
                "note": "Real yield, breakeven, and Treasury yields are one family, not separate votes.",
            }
        ] if len(yield_ids) > 1 else [],
        "missing_response_is": "unknown",
        "background_cannot_veto_setup": headline_role == "BACKGROUND_ONLY",
    }
