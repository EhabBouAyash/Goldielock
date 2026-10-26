"""Checks that are allowed to veto a trade. A write-up cannot undo them.

A failed check becomes NO-TRADE. Cached charts, a missing calendar, a second
idea, or a quote that is not from the trading account all fail closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from deepseek_trial.research_types_v2 import (
    DATA_MAX_AGE,
    FLAT_BY,
    LONDON,
    NEW_YORK,
    NEWS_AFTER,
    NEWS_BEFORE,
    NEWS_FLATTEN_BEFORE,
    QUOTE_MAX_AGE,
    AccountSnapshot,
    Bar,
    CalendarState,
    MarketData,
    NewsEvent,
    shift_time,
)


def validate_bar_list(bars: list[Bar], expected_minutes: int) -> tuple[bool, str]:
    if not bars:
        return False, "DATA_INVALID"
    delta = timedelta(minutes=expected_minutes)
    for bar in bars:
        if not bar.ok():
            return False, "DATA_INVALID"
        if bar.end - bar.start != delta:
            return False, "DATA_INVALID"
    return True, ""


def validate_freshness(bars: list[Bar], now: datetime, max_age: timedelta = DATA_MAX_AGE) -> tuple[bool, str]:
    if now.tzinfo is None or not bars:
        return False, "DATA_INVALID"
    last = bars[-1]
    if last.end > now:
        return False, "DATA_INVALID"
    if now - last.end > max_age:
        return False, "DATA_INVALID"
    return True, ""


def market_can_authorize(market: MarketData, now: datetime) -> tuple[bool, str]:
    """Only a fresh MT5 bundle may approve a live decision."""
    if market.display_only or not market.tradable:
        return False, market.reason or "DATA_INVALID"
    if market.source != "mt5":
        return False, "DATA_INVALID"
    ok, reason = validate_bar_list(market.m5, 5)
    if not ok:
        return False, reason
    ok, reason = validate_bar_list(market.h1, 60)
    if not ok and market.h1:
        return False, reason
    return validate_freshness(market.m5, now)


def quote_is_fresh(quote_time: datetime, now: datetime) -> bool:
    if quote_time.tzinfo is None or now.tzinfo is None:
        return False
    if quote_time > now:
        return False
    return now - quote_time <= QUOTE_MAX_AGE


def _afternoon_end(event: NewsEvent) -> datetime:
    local = event.start.astimezone(NEW_YORK)
    return local.replace(hour=21, minute=0, second=0, microsecond=0).astimezone(event.start.tzinfo)


def news_blocks_entry(when: datetime, calendar: CalendarState) -> bool:
    if not calendar.loaded:
        return True
    for event in calendar.events:
        if event.tag_only:
            continue
        start = event.start - NEWS_BEFORE
        end = event.end + NEWS_AFTER
        if event.block_afternoon:
            end = max(end, _afternoon_end(event))
        if start <= when <= end:
            return True
    return False


def news_flatten_due(when: datetime, calendar: CalendarState) -> bool:
    if not calendar.loaded:
        return False
    for event in calendar.events:
        if event.tag_only:
            continue
        flatten_at = event.start - NEWS_FLATTEN_BEFORE
        end = event.end
        if event.block_afternoon:
            end = _afternoon_end(event)
        if flatten_at <= when <= end:
            return True
    return False


def news_overlaps(opened: datetime, closed: datetime, calendar: CalendarState) -> bool:
    """True when the hold touches the restricted window, even if we exit early."""
    if not calendar.loaded:
        return False
    for event in calendar.events:
        if event.tag_only:
            continue
        start = event.start - NEWS_BEFORE
        end = event.end + NEWS_AFTER
        if event.block_afternoon:
            end = max(end, _afternoon_end(event))
        if opened <= end and closed >= start:
            return True
    return False


def account_can_trade(account: AccountSnapshot, mode: str) -> tuple[bool, str]:
    if account is None or not account.reconciliation_ok or account.missing_stop:
        return False, "ACCOUNT_UNAVAILABLE"
    if mode == "research":
        if not (account.research_only and account.authoritative):
            return False, "ACCOUNT_UNAVAILABLE"
        return True, ""
    if account.source != "mt5" or not account.authoritative or account.research_only:
        return False, "ACCOUNT_UNAVAILABLE"
    return True, ""


def screen(
    *,
    mode: str,
    calendar: CalendarState,
    account: AccountSnapshot,
    when: datetime,
    event_filter: bool,
    ideas_open: int,
) -> tuple[bool, str]:
    if calendar is None or not calendar.loaded:
        return False, "CALENDAR_UNAVAILABLE"
    ok, reason = account_can_trade(account, mode)
    if not ok:
        return False, reason
    if ideas_open > 0 or account.open_positions > 0 or account.pending_orders > 0:
        return False, "EXPOSURE_LIMIT"
    if event_filter and news_blocks_entry(when, calendar):
        return False, "NEWS_BLACKOUT"
    return True, ""


def london_flat_deadline(when: datetime, shift_minutes: int = 0) -> bool:
    local = when.astimezone(LONDON)
    if local.weekday() >= 5:
        return True
    return local.time() >= shift_time(FLAT_BY, shift_minutes)


def execution_workflow(account: AccountSnapshot | None) -> str:
    """This package never sends an order. Larger or unclear accounts stay manual."""
    if account is None or not account.authoritative:
        return "NO_TRADE"
    if account.automation_permitted:
        return "MANUAL_REVIEW"
    return "MANUAL_REVIEW"
