"""Executable prices come from the MT5 account. Other feeds are background only.

Yahoo's gold chart is a proxy. A daily Treasury yield is background. Neither
one can approve an entry, a stop, or a size. If the MT5 quote or the last
closed bar is missing or old, the result is DATA_INVALID, which means no trade.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from deepseek_trial.research_types_v2 import QUOTE_MAX_AGE, UTC, Bar, MarketData, Quote


PROXY_SYMBOLS = {"GC=F", "XAUUSD=X", "GOLD"}


def _invalid(reason: str, source: str, now: datetime | None) -> MarketData:
    return MarketData(
        source=source,
        tradable=False,
        reason=reason,
        m5=[],
        h1=[],
        as_of=now,
        display_only=False,
    )


def label_real_yield(value: float, as_of: datetime) -> dict:
    """DFII10 is a daily series. Name the day it belongs to and do not treat it as live."""
    return {
        "series": "DFII10",
        "value": value,
        "as_of": as_of.isoformat(),
        "frequency": "daily",
        "role": "context_only",
        "may_confirm_five_minute_entry": False,
        "plain": "This is a daily real yield. It is background for the day, not a fresh five-minute confirmation.",
    }


def label_context_feed(symbol: str, note: str = "") -> dict:
    return {
        "symbol": symbol,
        "role": "context_only",
        "may_authorize_trade": False,
        "plain": note or "This feed is not the account's bid and ask. It cannot approve a trade.",
    }


def _load_mt5(module):
    if module is not None:
        return module
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None
    return mt5


def bars_from_rates(rates, timeframe_minutes: int, now: datetime) -> tuple[list[Bar], str]:
    """Drop the candle that has not closed yet. A partial candle cannot be a signal."""
    if now.tzinfo is None:
        return [], "DATA_INVALID"
    bars: list[Bar] = []
    for row in rates or []:
        try:
            start = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc).astimezone(UTC)
            end = start + timedelta(minutes=timeframe_minutes)
            if end > now:
                continue
            bar = Bar(
                start=start,
                end=end,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
        except (KeyError, TypeError, ValueError):
            return [], "DATA_INVALID"
        if not bar.ok():
            return [], "DATA_INVALID"
        bars.append(bar)
    if not bars:
        return [], "DATA_INVALID"
    return bars, ""


def fetch_executable(
    symbol: str,
    now: datetime,
    mt5=None,
    m5_count: int = 500,
    h1_count: int = 400,
) -> MarketData:
    """Read closed M5 and H1 bars plus the current bid/ask from MT5.

    On a Mac the official MetaTrader5 package is usually unavailable. That
    correctly returns no trade. It does not fall back to Yahoo.
    """
    if not symbol or symbol.upper() in PROXY_SYMBOLS:
        return _invalid("DATA_INVALID", "proxy_rejected", now)
    module = _load_mt5(mt5)
    if module is None:
        return _invalid("DATA_INVALID", "mt5_unavailable", now)
    if not module.initialize():
        return _invalid("DATA_INVALID", "mt5", now)
    try:
        m5_rates = module.copy_rates_from_pos(symbol, module.TIMEFRAME_M5, 0, m5_count)
        h1_rates = module.copy_rates_from_pos(symbol, module.TIMEFRAME_H1, 0, h1_count)
        tick = module.symbol_info_tick(symbol)
    except Exception:
        return _invalid("DATA_INVALID", "mt5", now)
    m5, reason = bars_from_rates(m5_rates, 5, now)
    if reason:
        return _invalid(reason, "mt5", now)
    h1, reason = bars_from_rates(h1_rates, 60, now)
    if reason:
        return _invalid(reason, "mt5", now)
    if tick is None:
        return _invalid("DATA_INVALID", "mt5", now)
    try:
        quote_time = datetime.fromtimestamp(int(tick.time), tz=timezone.utc).astimezone(UTC)
        quote = Quote(time=quote_time, bid=float(tick.bid), ask=float(tick.ask), source="mt5")
    except (AttributeError, TypeError, ValueError, OSError):
        return _invalid("DATA_INVALID", "mt5", now)
    if quote.ask <= quote.bid:
        return _invalid("DATA_INVALID", "mt5", now)
    if now - quote.time > QUOTE_MAX_AGE or quote.time > now:
        data = _invalid("DATA_INVALID", "mt5", now)
        data.m5 = m5
        data.h1 = h1
        data.display_only = True
        return data
    if now - m5[-1].end > timedelta(minutes=6):
        data = _invalid("DATA_INVALID", "mt5", now)
        data.m5 = m5
        data.h1 = h1
        data.display_only = True
        return data
    return MarketData(
        source="mt5",
        tradable=True,
        reason="",
        m5=m5,
        h1=h1,
        as_of=now,
        quote=quote,
        display_only=False,
    )
