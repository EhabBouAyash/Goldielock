"""News blackouts, shock pause, session clocks. LLM must not invent event times."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from deepseek_trial.config import PACKAGE_DIR

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
DEFAULT_CALENDAR = PACKAGE_DIR / "calendars" / "events.json"


@dataclass(frozen=True)
class CalendarEvent:
    start_utc: datetime
    end_utc: datetime
    title: str
    block_afternoon: bool = False
    tag_only: bool = False


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def load_calendar(path: Path | None = None) -> tuple[list[CalendarEvent], str | None]:
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
                title=str(row.get("title", "")),
                block_afternoon=bool(row.get("block_afternoon", False)),
                tag_only=bool(row.get("tag_only", False)),
            ))
        return events, None
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return [], "DATA_MISSING"


def in_window(now: datetime, tz, start_hm: tuple[int, int], end_hm: tuple[int, int]) -> bool:
    """Half-open [start, end) in local tz. Weekdays only."""
    local = now.astimezone(tz)
    if local.weekday() >= 5:
        return False
    sh, sm = start_hm
    eh, em = end_hm
    t = local.hour * 60 + local.minute
    return sh * 60 + sm <= t < eh * 60 + em


def london_a(now: datetime) -> bool:
    return in_window(now, LONDON, (8, 0), (11, 0))


def ny_a(now: datetime) -> bool:
    return in_window(now, NEW_YORK, (8, 0), (11, 0))


def london_b(now: datetime) -> bool:
    return in_window(now, LONDON, (8, 0), (10, 30))


def ny_c_entry(now: datetime) -> bool:
    return in_window(now, NEW_YORK, (9, 45), (11, 15))


def ny_c_breakout_deadline(now: datetime) -> bool:
    """Breakout must confirm before 10:45 NY."""
    return in_window(now, NEW_YORK, (9, 45), (10, 45))


def ny_1545(now: datetime) -> datetime:
    local = now.astimezone(NEW_YORK)
    return local.replace(hour=15, minute=45, second=0, microsecond=0).astimezone(UTC)


def london_1200(now: datetime) -> datetime:
    local = now.astimezone(LONDON)
    return local.replace(hour=12, minute=0, second=0, microsecond=0).astimezone(UTC)


def merged_blackout(now: datetime, events: list[CalendarEvent]) -> bool:
    """Entries disallowed E-15 through E+15. Overlapping windows merge."""
    for event in events:
        if event.tag_only:
            continue
        start = event.start_utc - timedelta(minutes=15)
        end = event.end_utc + timedelta(minutes=15)
        if event.block_afternoon:
            ny = event.start_utc.astimezone(NEW_YORK)
            end = ny.replace(hour=21, minute=0, second=0, microsecond=0).astimezone(UTC)
        if start <= now <= end:
            return True
    return False


def news_flatten(now: datetime, events: list[CalendarEvent]) -> bool:
    """Close existing positions at E-10."""
    for event in events:
        if event.tag_only:
            continue
        t = event.start_utc - timedelta(minutes=10)
        if t <= now < event.start_utc:
            return True
        if event.block_afternoon and now >= t:
            ny = event.start_utc.astimezone(NEW_YORK)
            end = ny.replace(hour=21, minute=0, second=0, microsecond=0).astimezone(UTC)
            if now <= end:
                return True
    return False


def reference_overlaps_blackout(ref_start: datetime, ref_end: datetime, events: list[CalendarEvent]) -> bool:
    for event in events:
        if event.tag_only:
            continue
        b0 = event.start_utc - timedelta(minutes=15)
        b1 = event.end_utc + timedelta(minutes=15)
        if event.block_afternoon:
            ny = event.start_utc.astimezone(NEW_YORK)
            b1 = ny.replace(hour=21, minute=0, second=0, microsecond=0).astimezone(UTC)
        if b0 < ref_end and b1 > ref_start:
            return True
    return False


def iso(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
