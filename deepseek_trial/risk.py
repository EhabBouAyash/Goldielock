"""Paper sizing and FundedNext-style internal gates. No live account."""
from __future__ import annotations

import math
from dataclasses import dataclass

from deepseek_trial.config import (
    AGGREGATE_RISK_CAP,
    COST_FRAC_OF_RISK,
    DAILY_STOP,
    FIRM_DAILY_LOSS,
    FIRM_EQUITY_FLOOR,
    FIRM_MAX_LOSS,
    HEADROOM,
    MARGIN_CAP_FRAC,
    RISK_PER_TRADE,
    SPREAD_FRAC_OF_STOP,
    STARTING_BALANCE,
    TRIAL_DRAWDOWN_CAP,
    TRIAL_EQUITY_FLOOR,
    SymbolSpec,
)


def round_away(value: float, fill: float, tick: float) -> float:
    if tick <= 0:
        return value
    steps = value / tick
    if value >= fill:
        return math.ceil(steps - 1e-12) * tick
    return math.floor(steps + 1e-12) * tick


def floor_to_step(lots: float, step: float) -> float:
    if step <= 0:
        return lots
    return math.floor(lots / step + 1e-12) * step


def spread_price(mid: float, spec: SymbolSpec) -> float:
    return mid * spec.spread_bps / 1e4


def one_lot_stop_loss(spec: SymbolSpec, entry: float, stop: float) -> float:
    return spec.contract_size * abs(entry - stop)


def round_trip_cost(spec: SymbolSpec, mid: float, lots: float) -> float:
    spread = spread_price(mid, spec) * spec.contract_size * lots
    slip = 2 * spec.slippage * spec.contract_size * lots
    return spread + slip


@dataclass
class SizeResult:
    lots: float
    planned_risk: float
    reason: str


def size_trade(
    spec: SymbolSpec,
    side: str,
    entry: float,
    stop: float,
    equity: float,
    daily_pnl: float,
    peak_equity: float,
    open_planned_risk: float,
) -> SizeResult:
    if not (entry > 0 and stop > 0) or entry == stop:
        return SizeResult(0.0, 0.0, "DATA_MISSING")
    if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
        return SizeResult(0.0, 0.0, "DATA_MISSING")

    distance = abs(entry - stop)
    spread = spread_price(entry, spec)
    if spread > SPREAD_FRAC_OF_STOP * distance:
        return SizeResult(0.0, 0.0, "COST_TOO_HIGH")

    lot_loss = one_lot_stop_loss(spec, entry, stop)
    lot_cost = round_trip_cost(spec, entry, 1.0)
    L = lot_loss + lot_cost
    if L <= 0:
        return SizeResult(0.0, 0.0, "DATA_MISSING")

    lots = floor_to_step(RISK_PER_TRADE / L, spec.volume_step)
    if lots < spec.min_volume:
        return SizeResult(0.0, 0.0, "LOT_TOO_SMALL")

    notional = spec.contract_size * entry * lots
    margin = notional / spec.leverage if spec.leverage else notional
    if margin > MARGIN_CAP_FRAC * equity:
        lots = floor_to_step((MARGIN_CAP_FRAC * equity) * spec.leverage / (spec.contract_size * entry), spec.volume_step)
        if lots < spec.min_volume:
            return SizeResult(0.0, 0.0, "MARGIN_LIMIT")

    planned = spec.contract_size * abs(entry - stop) * lots
    if round_trip_cost(spec, entry, lots) > COST_FRAC_OF_RISK * planned:
        return SizeResult(0.0, 0.0, "COST_TOO_HIGH")
    if open_planned_risk + planned > AGGREGATE_RISK_CAP:
        return SizeResult(0.0, 0.0, "RISK_LIMIT")

    worst = equity - planned - HEADROOM
    if worst < FIRM_EQUITY_FLOOR or equity - planned < TRIAL_EQUITY_FLOOR:
        return SizeResult(0.0, 0.0, "RISK_LIMIT")
    if peak_equity - (equity - planned) > TRIAL_DRAWDOWN_CAP:
        return SizeResult(0.0, 0.0, "RISK_LIMIT")
    if daily_pnl - planned <= -min(DAILY_STOP, FIRM_DAILY_LOSS - HEADROOM):
        return SizeResult(0.0, 0.0, "RISK_LIMIT")
    if STARTING_BALANCE - (equity - planned) > FIRM_MAX_LOSS - HEADROOM:
        return SizeResult(0.0, 0.0, "RISK_LIMIT")
    return SizeResult(lots, planned, "VALID_SIGNAL")
