"""Read-only broker snapshot.

A missing connection stays unknown. It is not stored as a flat account and it
is not stored as zero open profit or loss. This module never sends an order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

QUOTE_MAX_AGE_SECONDS = 5


def unknown_account(reason: str) -> dict[str, Any]:
    return {
        "known": False,
        "source": "unavailable",
        "reason": reason,
        "account_login": None,
        "currency": None,
        "balance": None,
        "equity": None,
        "floating_pnl": None,
        "positions": None,
        "pending_orders": None,
        "margin": None,
        "quote": None,
        "symbol_spec": None,
        "flat": None,
        "execution_eligible": False,
        "order_submission": False,
    }


def _quote_from_tick(symbol: str, tick: Any, now: datetime) -> dict[str, Any]:
    tick_epoch = getattr(tick, "time", None)
    if tick_epoch:
        observed = datetime.fromtimestamp(int(tick_epoch), tz=timezone.utc)
    else:
        observed = now
    age = (now - observed).total_seconds()
    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    fresh = bid and ask and 0 <= age <= QUOTE_MAX_AGE_SECONDS
    return {
        "source": "mt5",
        "instrument": symbol,
        "role": "broker_executable" if fresh else "broker_stale",
        "observation_time": observed.isoformat(),
        "received_time": now.isoformat(),
        "bid": bid,
        "ask": ask,
        "last": getattr(tick, "last", None),
        "execution_eligible": bool(fresh),
        "is_fallback": False,
    }


def read_broker_state(now: datetime | None = None, symbol: str = "XAUUSD") -> dict[str, Any]:
    """Ask MetaTrader for account, positions, and a quote. Do not place trades."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return unknown_account(
            "MetaTrader5 is not installed. Unknown account state was not treated as flat."
        )
    if not mt5.initialize():
        return unknown_account(f"MetaTrader5 did not connect: {mt5.last_error()}")
    try:
        info = mt5.account_info()
        if info is None:
            return unknown_account("Broker account info was not returned.")
        positions = mt5.positions_get()
        orders = mt5.orders_get()
        tick = mt5.symbol_info_tick(symbol)
        spec = mt5.symbol_info(symbol)
        quote = _quote_from_tick(symbol, tick, moment) if tick is not None else None
        symbol_spec = None
        if spec is not None:
            symbol_spec = {
                "contract_size": getattr(spec, "trade_contract_size", None),
                "volume_min": getattr(spec, "volume_min", None),
                "volume_step": getattr(spec, "volume_step", None),
                "volume_max": getattr(spec, "volume_max", None),
            }
        position_rows = []
        if positions:
            for position in positions:
                position_rows.append({
                    "ticket": getattr(position, "ticket", None),
                    "symbol": getattr(position, "symbol", None),
                    "volume": getattr(position, "volume", None),
                    "price_open": getattr(position, "price_open", None),
                    "price_current": getattr(position, "price_current", None),
                    "sl": getattr(position, "sl", None),
                    "profit": getattr(position, "profit", None),
                })
        return {
            "known": True,
            "source": "mt5",
            "reason": None,
            "account_login": getattr(info, "login", None),
            "currency": getattr(info, "currency", None),
            "balance": getattr(info, "balance", None),
            "equity": getattr(info, "equity", None),
            "floating_pnl": getattr(info, "profit", None),
            "positions": position_rows,
            "pending_orders": len(orders or []),
            "margin": getattr(info, "margin", None),
            "quote": quote,
            "symbol_spec": symbol_spec,
            "flat": not position_rows and not orders,
            "execution_eligible": bool(quote and quote.get("execution_eligible")),
            "order_submission": False,
        }
    finally:
        mt5.shutdown()


def read_broker_quote(now: datetime | None = None, symbol: str = "XAUUSD") -> dict[str, Any] | None:
    state = read_broker_state(now=now, symbol=symbol)
    return state.get("quote")
