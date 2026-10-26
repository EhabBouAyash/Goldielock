"""Sizing, friction, stop floor/cap. $125 risk. Stops never widen after fill."""
from __future__ import annotations

import math

from deepseek_trial.config import (
    COMMISSION_PER_SIDE,
    FRICTION_MAX_FRAC,
    GOLD_TICK,
    MARGIN_CAP_FRAC,
    RISK_PER_TRADE,
    SLIPPAGE_SPREAD_FRAC,
    STOP_CAP_ATR,
    STOP_FLOOR_ATR,
    SymbolSpec,
)


def spread_price(mid: float, spec: SymbolSpec) -> float:
    return mid * spec.spread_bps / 1e4


def slippage_per_fill(mid: float, spec: SymbolSpec) -> float:
    spread = spread_price(mid, spec)
    return max(spec.tick, SLIPPAGE_SPREAD_FRAC * spread)


def friction_per_ounce(mid: float, spec: SymbolSpec) -> float:
    spread = spread_price(mid, spec)
    slip = 2 * slippage_per_fill(mid, spec)
    commission = 2 * COMMISSION_PER_SIDE * mid
    return spread + slip + commission


def round_stop(side: str, stop: float, tick: float) -> float:
    if tick <= 0:
        return stop
    if side == "BUY":
        return math.floor(stop / tick + 1e-12) * tick
    return math.ceil(stop / tick - 1e-12) * tick


def apply_stop_band(side: str, entry: float, structural: float, atr: float, tick: float) -> tuple[float | None, str]:
    if not (entry > 0 and atr > 0) or not math.isfinite(atr):
        return None, "DATA_MISSING"
    d = abs(entry - structural)
    floor_d = STOP_FLOOR_ATR * atr
    cap_d = STOP_CAP_ATR * atr
    if d < floor_d:
        d = floor_d
    if d > cap_d:
        return None, "STOP_TOO_WIDE"
    if side == "BUY":
        stop = round_stop(side, entry - d, tick)
        if stop >= entry:
            return None, "DATA_MISSING"
    else:
        stop = round_stop(side, entry + d, tick)
        if stop <= entry:
            return None, "DATA_MISSING"
    return stop, ""


def size_trade(spec: SymbolSpec, side: str, entry: float, stop: float, equity: float) -> tuple[float, float, str]:
    d = abs(entry - stop)
    if d <= 0:
        return 0.0, 0.0, "DATA_MISSING"
    if friction_per_ounce(entry, spec) > FRICTION_MAX_FRAC * d:
        return 0.0, 0.0, "FRICTION"
    slip = slippage_per_fill(entry, spec)
    one_lot = spec.contract_size * (d + 2 * slip + 2 * COMMISSION_PER_SIDE * entry)
    if one_lot <= 0:
        return 0.0, 0.0, "DATA_MISSING"
    lots = math.floor(RISK_PER_TRADE / one_lot / spec.volume_step + 1e-12) * spec.volume_step
    if lots < spec.min_volume:
        return 0.0, 0.0, "LOT_TOO_SMALL"
    notional = spec.contract_size * entry * lots
    margin = notional / spec.leverage if spec.leverage else notional
    if margin > MARGIN_CAP_FRAC * equity:
        lots = math.floor(
            (MARGIN_CAP_FRAC * equity * spec.leverage) / (spec.contract_size * entry) / spec.volume_step + 1e-12
        ) * spec.volume_step
        if lots < spec.min_volume:
            return 0.0, 0.0, "MARGIN_LIMIT"
    r_cash = spec.contract_size * lots * (d + 2 * slip + 2 * COMMISSION_PER_SIDE * entry)
    return lots, r_cash, "OK"
