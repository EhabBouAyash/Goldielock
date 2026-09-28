"""Session, weekend, and high-impact windows. Calendar absence blocks entries."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deepseek_trial.config import (
    ENTRY_BLOCK_END_HOUR,
    ENTRY_BLOCK_START_HOUR,
    NEWS_BUFFER_MIN,
    NO_ENTRY_BEFORE_WEEKEND_MINUTES,
    PACKAGE_DIR,
    WEEKEND_FLATTEN_MINUTES,
)

DEFAULT_CALENDAR = PACKAGE_DIR / "calendars" / "events.json"


@dataclass(frozen=True)
class CalendarEvent:
    start_utc: datetime
    end_utc: datetime
    instrument: str
    title: str


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_calendar(path: Path | None = None) -> tuple[list[CalendarEvent], str | None]:
    """Return events and an error code if the calendar is missing or malformed."""
    cal_path = path or DEFAULT_CALENDAR
    if not cal_path.is_file():
        return [], "DATA_MISSING"
    try:
        raw = json.loads(cal_path.read_text())
        rows = raw.get("events") if isinstance(raw, dict) else raw
        events = []
        for row in rows or []:
            start = parse_iso(row["start_utc"])
            end = parse_iso(row.get("end_utc") or row["start_utc"])
            events.append(CalendarEvent(
                start_utc=start,
                end_utc=end,
                instrument=str(row.get("instrument", "*")).upper(),
                title=str(row.get("title", "")),
            ))
        return events, None
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return [], "DATA_MISSING"


def in_entry_blackout(now: datetime) -> bool:
    hour = now.astimezone(timezone.utc).hour
    return ENTRY_BLOCK_START_HOUR <= hour < ENTRY_BLOCK_END_HOUR


def near_high_impact(now: datetime, instrument: str, events: list[CalendarEvent]) -> bool:
    buf = timedelta(minutes=NEWS_BUFFER_MIN)
    name = instrument.upper()
    for event in events:
        inst = event.instrument.upper()
        if inst not in ("*", name):
            continue
        if event.start_utc - buf <= now <= event.end_utc + buf:
            return True
    return False


def friday_session_end(now: datetime) -> datetime | None:
    """Approximate FX/crypto Friday close as 21:00 UTC (17:00 New York standard)."""
    local = now.astimezone(timezone.utc)
    weekday = local.weekday()
    if weekday != 4:
        return None
    return local.replace(hour=21, minute=0, second=0, microsecond=0)


def weekend_flatten_due(now: datetime, mandatory: bool) -> bool:
    if not mandatory:
        return False
    close = friday_session_end(now)
    if close is None:
        return local_is_weekend(now)
    return now >= close - timedelta(minutes=WEEKEND_FLATTEN_MINUTES)


def local_is_weekend(now: datetime) -> bool:
    wd = now.astimezone(timezone.utc).weekday()
    hour = now.astimezone(timezone.utc).hour
    if wd == 5:
        return True
    if wd == 6 and hour < 21:
        return True
    if wd == 4 and hour >= 21:
        return True
    return False


def no_new_entry_near_weekend(now: datetime) -> bool:
    close = friday_session_end(now)
    if close is None:
        return local_is_weekend(now)
    return now >= close - timedelta(minutes=NO_ENTRY_BEFORE_WEEKEND_MINUTES)


def iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
