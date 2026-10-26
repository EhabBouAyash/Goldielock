"""Position size from remaining loss room. The model does not pick the size.

The budget is 0.25% of the starting $25,000 ($62.50), then cut further by
whatever daily, total, and open-risk room is left after a small buffer.
If that room cannot fund the smallest lot, the answer is NO-TRADE.
"""
from __future__ import annotations

import math

from deepseek_trial.research_types_v2 import (
    BUFFER_DAILY,
    BUFFER_EXPOSURE,
    BUFFER_TOTAL,
    CONTRACT,
    DAILY_LOSS_HARD,
    EQUITY_FLOOR,
    FRICTION_MAX_R,
    MIN_VOLUME,
    R_BASE,
    TARGET_R,
    VOLUME_STEP,
    AccountSnapshot,
    CostModel,
)


def floor_step(value: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    return math.floor(value / step + 1e-9) * step


def risk_budget(account: AccountSnapshot) -> tuple[float, str]:
    """Dollars this new idea may lose. Non-positive means do not trade."""
    exposure = max(0.0, account.open_risk_usd) + max(0.0, account.pending_risk_usd)
    if account.open_positions > 0 or account.pending_orders > 0 or exposure > 0:
        # One idea at a time. Existing risk is not a prompt to add another.
        room = 0.0
    else:
        room = R_BASE
    trading_loss = max(0.0, -account.daily_trade_pnl)
    daily_room = DAILY_LOSS_HARD - trading_loss - exposure
    total_room = account.equity - EQUITY_FLOOR - exposure
    allowed = min(
        R_BASE,
        daily_room - BUFFER_DAILY,
        total_room - BUFFER_TOTAL,
        room - BUFFER_EXPOSURE,
    )
    if allowed <= 0:
        return 0.0, "RISK_BUDGET"
    return allowed, ""


def _loss_per_lot(entry: float, stop: float, costs: CostModel) -> float:
    distance = abs(entry - stop)
    extra = costs.slippage_per_side + 2.0 * costs.commission_rate_per_side * entry
    return CONTRACT * (distance + extra)


def _margin(entry: float, lots: float, costs: CostModel) -> float:
    if costs.leverage <= 0:
        return math.inf
    return CONTRACT * entry * lots / costs.leverage


def assess(
    *,
    side: str,
    entry: float,
    stop: float,
    opposing_high: float | None,
    opposing_low: float | None,
    costs: CostModel,
    account: AccountSnapshot,
    spread: float | None = None,
    profit_fn=None,
) -> dict:
    """Return lots and prices, or ok=False with a reason. Never widens a stop."""
    budget, budget_reason = risk_budget(account)
    blank = {
        "ok": False,
        "reason": budget_reason or "RISK_BUDGET",
        "lots": 0.0,
        "risk_usd": 0.0,
        "entry": entry,
        "stop": stop,
        "target": None,
        "friction_r": None,
        "r_budget": budget,
    }
    if budget <= 0:
        return blank
    if side not in ("LONG", "SHORT") or entry <= 0 or stop <= 0:
        blank["reason"] = "STOP_INVALID"
        return blank
    if side == "LONG" and stop >= entry:
        blank["reason"] = "STOP_INVALID"
        return blank
    if side == "SHORT" and stop <= entry:
        blank["reason"] = "STOP_INVALID"
        return blank
    distance = abs(entry - stop)
    if distance <= 0:
        blank["reason"] = "STOP_INVALID"
        return blank
    friction = costs.friction_price(entry, spread)
    friction_r = friction / distance
    if friction_r > FRICTION_MAX_R:
        blank["reason"] = "FRICTION"
        blank["friction_r"] = friction_r
        return blank
    if side == "LONG":
        target = entry + TARGET_R * distance
        if opposing_high is None:
            blank["reason"] = "PRIOR_LEVEL_UNKNOWN"
            return blank
        if target > opposing_high:
            blank["reason"] = "TARGET_BEYOND_LEVEL"
            blank["target"] = target
            return blank
    else:
        target = entry - TARGET_R * distance
        if opposing_low is None:
            blank["reason"] = "PRIOR_LEVEL_UNKNOWN"
            return blank
        if target < opposing_low:
            blank["reason"] = "TARGET_BEYOND_LEVEL"
            blank["target"] = target
            return blank

    per_lot = _loss_per_lot(entry, stop, costs)
    if profit_fn is not None:
        quoted = profit_fn(side, entry, stop, 1.0)
        if quoted is None or quoted <= 0:
            blank["reason"] = "MT5_PROFIT_CALC_UNAVAILABLE"
            return blank
        per_lot = max(per_lot, float(quoted))
    if per_lot <= 0:
        blank["reason"] = "MIN_SIZE"
        return blank
    lots = floor_step(budget / per_lot, VOLUME_STEP)
    if lots < MIN_VOLUME:
        blank["reason"] = "MIN_SIZE"
        blank["friction_r"] = friction_r
        return blank
    while lots >= MIN_VOLUME and _margin(entry, lots, costs) > account.free_margin:
        lots = floor_step(lots - VOLUME_STEP, VOLUME_STEP)
    if lots < MIN_VOLUME:
        blank["reason"] = "MARGIN"
        blank["friction_r"] = friction_r
        return blank
    risk_usd = per_lot * lots
    if risk_usd > budget + 1e-6:
        blank["reason"] = "RISK_BUDGET"
        return blank
    return {
        "ok": True,
        "reason": "",
        "lots": round(lots, 2),
        "risk_usd": risk_usd,
        "entry": entry,
        "stop": stop,
        "target": target,
        "friction_r": friction_r,
        "r_budget": budget,
    }
