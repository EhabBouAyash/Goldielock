"""The MT5 account is the source of truth. The handwritten backlog is only a note.

A withdrawal is not a trading loss. A note that says the account is fine does
not raise the loss room. If the broker numbers do not add up, or an open trade
has no stop, the account cannot approve a new trade.
"""
from __future__ import annotations

from datetime import datetime

from deepseek_trial.research_types_v2 import INITIAL_BALANCE, AccountSnapshot


def research_snapshot(
    now: datetime,
    equity: float = INITIAL_BALANCE,
    daily_trade_pnl: float = 0.0,
    free_margin: float | None = None,
    open_positions: int = 0,
    pending_orders: int = 0,
    open_risk_usd: float = 0.0,
    pending_risk_usd: float = 0.0,
) -> AccountSnapshot:
    """Paper account for a replay. It is not a live FundedNext login."""
    return AccountSnapshot(
        source="research_simulator",
        as_of=now,
        authoritative=True,
        research_only=True,
        balance=equity,
        equity=equity,
        free_margin=equity if free_margin is None else free_margin,
        margin_used=0.0,
        daily_trade_pnl=daily_trade_pnl,
        open_risk_usd=open_risk_usd,
        pending_risk_usd=pending_risk_usd,
        open_positions=open_positions,
        pending_orders=pending_orders,
        withdrawals_usd=0.0,
        automation_permitted=False,
        missing_stop=False,
        reconciliation_ok=True,
        notes=("Research simulator. Not the broker account.",),
    )


def annotations_from_text(text: str) -> tuple[str, ...]:
    """Keep manual lines as notes. Do not read an equity figure out of them."""
    notes = []
    for line in (text or "").splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#"):
            notes.append(cleaned)
    return tuple(notes)


def _stop_ok(row: dict) -> bool:
    try:
        return float(row.get("sl") or 0) > 0
    except (TypeError, ValueError):
        return False


def from_mt5(
    account: dict,
    deals: list[dict],
    positions: list[dict],
    orders: list[dict],
    annotations: list[str] | tuple[str, ...],
    now: datetime,
    day_start: datetime | None,
    starting_balance: float = INITIAL_BALANCE,
) -> AccountSnapshot:
    """Build the snapshot the risk check is allowed to trust."""
    trade_pnl = 0.0
    balance_ops = 0.0
    withdrawals = 0.0
    daily = 0.0
    for deal in deals:
        profit = float(deal.get("profit") or 0)
        kind = deal.get("kind")
        when = deal.get("time")
        if kind == "balance":
            balance_ops += profit
            if profit < 0:
                withdrawals += -profit
            continue
        if kind != "trade":
            continue
        trade_pnl += profit
        if day_start is not None and when is not None and day_start <= when <= now:
            daily += profit

    implied = starting_balance + trade_pnl + balance_ops
    balance = float(account.get("balance"))
    equity = float(account.get("equity"))
    gap = abs(implied - balance) > 1.0
    missing_stop = any(not _stop_ok(row) for row in list(positions) + list(orders))
    open_risk = 0.0
    pending_risk = 0.0
    for row in positions:
        open_risk += max(0.0, float(row.get("risk_usd") or 0))
    for row in orders:
        pending_risk += max(0.0, float(row.get("risk_usd") or 0))

    notes = [
        "Manual notes do not set balance, equity, or remaining loss room.",
        *list(annotations),
    ]
    ok = not gap and not missing_stop and day_start is not None
    if day_start is None:
        notes.append("DAILY_WINDOW_UNKNOWN")
    if gap:
        notes.append("RECONCILIATION_GAP")
    if missing_stop:
        notes.append("MISSING_STOP")
    return AccountSnapshot(
        source="mt5",
        as_of=now,
        authoritative=ok,
        research_only=False,
        balance=balance,
        equity=equity,
        free_margin=float(account.get("free_margin")),
        margin_used=float(account.get("margin_used") or 0),
        daily_trade_pnl=daily,
        open_risk_usd=open_risk,
        pending_risk_usd=pending_risk,
        open_positions=len(positions),
        pending_orders=len(orders),
        withdrawals_usd=withdrawals,
        automation_permitted=bool(account.get("automation_permitted", False)),
        missing_stop=missing_stop,
        reconciliation_ok=ok,
        notes=tuple(notes),
    )
