"""A timestamped calendar is required. Headlines are notes, not a signal.

If the calendar file is missing, the day is a no-trade day. Two copies of the
same headline are kept once. A headline with no time is shown as unusable and
cannot be treated as a timed event.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from deepseek_trial.calendar_rules import parse_iso
from deepseek_trial.research_types_v2 import CalendarState, NewsEvent


def _normalize(title: str) -> str:
    text = title.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_headlines(items: list[dict]) -> list[dict]:
    seen = set()
    kept = []
    for item in items:
        title = str(item.get("title") or "").strip()
        key = _normalize(title)
        if not key or key in seen:
            continue
        seen.add(key)
        published = item.get("published_at")
        usable = isinstance(published, datetime) and published.tzinfo is not None
        kept.append({
            "title": title,
            "source": item.get("source") or "",
            "published_at": published.isoformat() if usable else None,
            "usable_for_research": usable,
            "reason": "" if usable else "MISSING_TIMESTAMP",
            "may_authorize_trade": False,
        })
    return kept


def load_calendar(path: Path | None) -> CalendarState:
    if path is None or not Path(path).is_file():
        return CalendarState(loaded=False, events=[], reason="CALENDAR_UNAVAILABLE")
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return CalendarState(loaded=False, events=[], reason="CALENDAR_UNAVAILABLE")
    rows = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return CalendarState(loaded=False, events=[], reason="CALENDAR_UNAVAILABLE")
    events = []
    try:
        for row in rows:
            start = parse_iso(row["start_utc"])
            end = parse_iso(row.get("end_utc") or row["start_utc"])
            events.append(NewsEvent(
                start=start,
                end=end,
                title=str(row.get("title") or ""),
                tag_only=bool(row.get("tag_only", False)),
                block_afternoon=bool(row.get("block_afternoon", False)),
            ))
    except (KeyError, TypeError, ValueError):
        return CalendarState(loaded=False, events=[], reason="CALENDAR_UNAVAILABLE")
    as_of = None
    if isinstance(raw, dict) and raw.get("as_of_utc"):
        try:
            as_of = parse_iso(raw["as_of_utc"])
        except (TypeError, ValueError):
            as_of = None
    return CalendarState(loaded=True, events=events, reason="", as_of=as_of)
