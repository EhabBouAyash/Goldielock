"""Pure strategy engine for XAUUSD supply/demand setups.

The engine does not fetch data or call an LLM. It turns completed OHLCV candle
records into deterministic zones, a market regime, and (only when every gate
passes) an actionable setup.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .structure import classify_movement_state, dynamic_zones


TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


@dataclass(frozen=True)
class StrategyConfig:
    atr_period: int = 14
    fast_ema: int = 9
    slow_ema: int = 21
    base_max_bars: int = 4
    departure_bars: int = 3
    departure_atr: float = 1.5
    pivot_left_bars: int = 2
    pivot_right_bars: int = 2
    pivot_lookback_bars: int = 30
    max_anchor_gap_bars: int = 6
    base_body_atr: float = 0.6
    max_zone_width_atr: float = 2.0
    grind_window_bars: int = 6
    max_mitigation_pct: float = 70.0
    zone_proximity_atr: float = 0.20
    grind_zone_proximity_atr: float = 0.35
    entry_buffer_atr: float = 0.05
    stop_buffer_atr: float = 0.15
    crossover_lookback: int = 3
    pattern_lookback: int = 3
    max_zone_touches: int = 2
    min_zone_score: float = 0.58
    min_reward_risk: float = 1.5
    target_reward_risk: float = 2.0
    risk_fraction: float = 0.005
    min_pattern_range_atr: float = 0.5
    require_htf_alignment: bool = True
    # Spread plus stop-order slippage per ounce, charged before the R:R gate.
    round_trip_cost: float = 0.40
    order_expiry_bars: int = 6
    # Daily CME maintenance break / rollover, when XAUUSD spreads widen.
    blocked_utc_hours: tuple[int, ...] = (21, 22)


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def completed_candles(
    records: Iterable[dict[str, Any]],
    timeframe: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Normalize candles and exclude a still-forming final bar when detectable."""
    minutes = TIMEFRAME_MINUTES.get(timeframe)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    clean: list[dict[str, Any]] = []
    for raw in records:
        values = [_number(raw.get(key)) for key in ("open", "high", "low", "close")]
        if any(value is None for value in values):
            continue
        open_, high, low, close = (float(value) for value in values)
        if high < low or high < max(open_, close) or low > min(open_, close):
            continue
        clean.append(
            {
                "ts": raw.get("ts"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": _number(raw.get("volume")),
            }
        )

    if clean and minutes:
        last_ts = _parse_ts(clean[-1].get("ts"))
        if last_ts and (now - last_ts).total_seconds() < minutes * 60:
            clean.pop()
    return clean


def true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    if not candles:
        return []
    result = [candles[0]["high"] - candles[0]["low"]]
    for index in range(1, len(candles)):
        previous_close = candles[index - 1]["close"]
        candle = candles[index]
        result.append(
            max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )
        )
    return result


def atr_series(candles: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    ranges = true_ranges(candles)
    values: list[float | None] = [None] * len(ranges)
    if len(ranges) < period:
        return values
    current = sum(ranges[:period]) / period
    values[period - 1] = current
    for index in range(period, len(ranges)):
        current = ((current * (period - 1)) + ranges[index]) / period
        values[index] = current
    return values


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value * multiplier) + (result[-1] * (1.0 - multiplier)))
    return result


def _adx(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(candles) < period * 2 + 1:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    ranges = true_ranges(candles)[1:]
    for index in range(1, len(candles)):
        up = candles[index]["high"] - candles[index - 1]["high"]
        down = candles[index - 1]["low"] - candles[index]["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    smooth_tr = sum(ranges[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])
    dx_values: list[float] = []
    for index in range(period, len(ranges)):
        smooth_tr = smooth_tr - (smooth_tr / period) + ranges[index]
        smooth_plus = smooth_plus - (smooth_plus / period) + plus_dm[index]
        smooth_minus = smooth_minus - (smooth_minus / period) + minus_dm[index]
        if smooth_tr <= 0:
            continue
        plus_di = 100.0 * smooth_plus / smooth_tr
        minus_di = 100.0 * smooth_minus / smooth_tr
        denominator = plus_di + minus_di
        dx_values.append(0.0 if denominator == 0 else 100.0 * abs(plus_di - minus_di) / denominator)
    if len(dx_values) < period:
        return None
    current = sum(dx_values[:period]) / period
    for value in dx_values[period:]:
        current = ((current * (period - 1)) + value) / period
    return round(current, 2)


def classify_regime(
    one_hour: list[dict[str, Any]],
    four_hour: list[dict[str, Any]],
    config: StrategyConfig,
) -> dict[str, Any]:
    """Pandas/NumPy regime with explicit impulse, grind, and consolidation states."""
    state = classify_movement_state(
        one_hour,
        atr_period=config.atr_period,
        impulse_atr=config.departure_atr,
        grind_window=config.grind_window_bars,
        pivot_left=config.pivot_left_bars,
        pivot_right=config.pivot_right_bars,
    )
    higher = classify_movement_state(
        four_hour,
        atr_period=config.atr_period,
        impulse_atr=config.departure_atr,
        grind_window=config.grind_window_bars,
        pivot_left=config.pivot_left_bars,
        pivot_right=config.pivot_right_bars,
    )
    if higher.get("regime") == "UNKNOWN" and len(four_hour) >= 26:
        closes_4h = [candle["close"] for candle in four_hour]
        ema20_4h = ema_series(closes_4h, 20)
        if closes_4h[-1] > ema20_4h[-1] > ema20_4h[-5]:
            higher = {"regime": "UPTREND", "movement_state": "TREND_UP"}
        elif closes_4h[-1] < ema20_4h[-1] < ema20_4h[-5]:
            higher = {"regime": "DOWNTREND", "movement_state": "TREND_DOWN"}
    state["four_hour_bias"] = (
        "UP"
        if higher.get("regime") == "UPTREND"
        else "DOWN"
        if higher.get("regime") == "DOWNTREND"
        else "MIXED"
    )
    state["four_hour_movement_state"] = higher.get("movement_state", "UNKNOWN")
    state["reason"] = (
        f"Deterministic movement state={state.get('movement_state')} from "
        "EMA slope/spread, ADX, ATR impulse, and grind metrics."
    )
    return state


def detect_zones(
    candles: list[dict[str, Any]],
    timeframe: str,
    config: StrategyConfig,
) -> list[dict[str, Any]]:
    """Recompute confirmed-pivot zones on every completed bar."""
    return dynamic_zones(
        candles,
        timeframe,
        atr_period=config.atr_period,
        pivot_left=config.pivot_left_bars,
        pivot_right=config.pivot_right_bars,
        pivot_lookback=config.pivot_lookback_bars,
        impulse_atr=config.departure_atr,
        impulse_max_bars=config.departure_bars,
        base_max_bars=config.base_max_bars,
        max_anchor_gap_bars=config.max_anchor_gap_bars,
        base_body_atr=config.base_body_atr,
        max_zone_width_atr=config.max_zone_width_atr,
    )


def _engulfing(previous: dict[str, Any], current: dict[str, Any]) -> str | None:
    previous_low = min(previous["open"], previous["close"])
    previous_high = max(previous["open"], previous["close"])
    current_low = min(current["open"], current["close"])
    current_high = max(current["open"], current["close"])
    if (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current_low <= previous_low
        and current_high >= previous_high
    ):
        return "BULLISH_ENGULFING"
    if (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current_low <= previous_low
        and current_high >= previous_high
    ):
        return "BEARISH_ENGULFING"
    return None


def candlestick_pattern(candles: list[dict[str, Any]], index: int) -> str | None:
    if index < 1 or index >= len(candles):
        return None
    current = candles[index]
    prior = candles[index - 1]
    engulfing = _engulfing(prior, current)
    if engulfing:
        return engulfing

    body = abs(current["close"] - current["open"])
    full_range = current["high"] - current["low"]
    if full_range <= 0:
        return None
    upper_wick = current["high"] - max(current["open"], current["close"])
    lower_wick = min(current["open"], current["close"]) - current["low"]
    body = max(body, full_range * 0.05)
    if lower_wick >= 2.0 * body and upper_wick <= body and current["close"] >= current["low"] + 0.6 * full_range:
        return "BULLISH_HAMMER"
    if upper_wick >= 2.0 * body and lower_wick <= body and current["close"] <= current["low"] + 0.4 * full_range:
        return "BEARISH_SHOOTING_STAR"
    return None


def _recent_crossover(
    candles: list[dict[str, Any]],
    direction: str,
    config: StrategyConfig,
) -> dict[str, Any] | None:
    closes = [candle["close"] for candle in candles]
    if len(closes) < config.slow_ema + config.crossover_lookback + 1:
        return None
    fast = ema_series(closes, config.fast_ema)
    slow = ema_series(closes, config.slow_ema)
    start = max(1, len(closes) - config.crossover_lookback)
    for index in range(len(closes) - 1, start - 1, -1):
        bullish = fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]
        bearish = fast[index - 1] >= slow[index - 1] and fast[index] < slow[index]
        if (direction == "LONG" and bullish) or (direction == "SHORT" and bearish):
            return {
                "direction": direction,
                "index": index,
                "ts": candles[index].get("ts"),
                "fast": round(fast[index], 2),
                "slow": round(slow[index], 2),
            }
    return None


def _pattern_in_zone(
    candles: list[dict[str, Any]],
    zone: dict[str, Any],
    direction: str,
    config: StrategyConfig,
) -> dict[str, Any] | None:
    expected = "BULLISH" if direction == "LONG" else "BEARISH"
    atrs = atr_series(candles, config.atr_period)
    start = max(1, len(candles) - config.pattern_lookback)
    for index in range(len(candles) - 1, start - 1, -1):
        candle = candles[index]
        intersects = candle["low"] <= zone["high"] and candle["high"] >= zone["low"]
        atr = atrs[index]
        if atr is None or candle["high"] - candle["low"] < config.min_pattern_range_atr * atr:
            continue
        pattern = candlestick_pattern(candles, index)
        if intersects and pattern and pattern.startswith(expected):
            return {
                "name": pattern,
                "index": index,
                "ts": candle.get("ts"),
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
            }
    return None


def _candidate_zones(
    zones: list[dict[str, Any]],
    current_price: float,
    atr: float,
    regime: str,
    movement_state: str,
    range_low: float,
    range_high: float,
    config: StrategyConfig,
) -> list[tuple[dict[str, Any], str]]:
    allowed = (
        {"DEMAND": "LONG"}
        if regime == "UPTREND"
        else {"SUPPLY": "SHORT"}
        if regime == "DOWNTREND"
        else {"DEMAND": "LONG", "SUPPLY": "SHORT"}
    )
    result: list[tuple[dict[str, Any], str]] = []
    for zone in zones:
        direction = allowed.get(zone["type"])
        if not direction or zone["touches"] > config.max_zone_touches:
            continue
        if float(zone.get("mitigation_pct", 0.0)) >= config.max_mitigation_pct:
            continue
        if zone["score"] < config.min_zone_score:
            continue
        if movement_state == "GRIND_UP" and (
            zone["type"] != "DEMAND" or zone.get("formation") != "RBR"
        ):
            continue
        if movement_state == "GRIND_DOWN" and (
            zone["type"] != "SUPPLY" or zone.get("formation") != "DBD"
        ):
            continue
        distance = (
            zone["low"] - current_price
            if current_price < zone["low"]
            else current_price - zone["high"]
            if current_price > zone["high"]
            else 0.0
        )
        proximity = (
            config.grind_zone_proximity_atr
            if movement_state.startswith("GRIND_")
            else config.zone_proximity_atr
        )
        if distance <= proximity * atr:
            candidate = dict(zone)
            candidate["zone_tolerance_atr"] = proximity
            range_width = range_high - range_low
            curve_position = (
                ((zone["low"] + zone["high"]) / 2.0 - range_low) / range_width
                if range_width > 0
                else 0.5
            )
            candidate["curve_position_1h"] = round(curve_position, 3)
            if regime == "CONSOLIDATING":
                at_outer_edge = (
                    zone["type"] == "DEMAND" and curve_position <= 0.35
                ) or (
                    zone["type"] == "SUPPLY" and curve_position >= 0.65
                )
                if not at_outer_edge:
                    continue
            result.append((candidate, direction))
    return sorted(
        result,
        key=lambda item: (
            0 if item[0]["timeframe"] == "4h" else 1 if item[0]["timeframe"] == "1h" else 2,
            -item[0]["score"],
        ),
    )


def _target_for(
    direction: str,
    entry: float,
    stop: float,
    zones: list[dict[str, Any]],
    one_hour: list[dict[str, Any]],
    config: StrategyConfig,
) -> tuple[float | None, str | None, float | None]:
    risk = abs(entry - stop)
    if risk <= 0:
        return None, None, None
    if direction == "LONG":
        opposing = sorted(
            (zone["low"] for zone in zones if zone["type"] == "SUPPLY" and zone["low"] > entry)
        )
        swing = max(candle["high"] for candle in one_hour[-20:])
        structural = [value for value in [opposing[0] if opposing else None, swing] if value and value > entry]
        target = min(structural) if structural else entry + config.target_reward_risk * risk
        source = "nearest_supply_or_1H_swing"
    else:
        opposing = sorted(
            (zone["high"] for zone in zones if zone["type"] == "DEMAND" and zone["high"] < entry),
            reverse=True,
        )
        swing = min(candle["low"] for candle in one_hour[-20:])
        structural = [value for value in [opposing[0] if opposing else None, swing] if value and value < entry]
        target = max(structural) if structural else entry - config.target_reward_risk * risk
        source = "nearest_demand_or_1H_swing"
    reward_risk = abs(target - entry) / risk
    return round(target, 2), source, round(reward_risk, 2)


def analyze_market(
    interval_records: dict[str, list[dict[str, Any]]],
    *,
    config: StrategyConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the complete deterministic strategy over raw interval candle records."""
    config = config or StrategyConfig()
    candles = {
        timeframe: completed_candles(records, timeframe, now=now)
        for timeframe, records in interval_records.items()
    }
    required = {"5m": 30, "15m": 40, "1h": 60, "4h": 30}
    missing = {
        timeframe: {"required": minimum, "available": len(candles.get(timeframe, []))}
        for timeframe, minimum in required.items()
        if len(candles.get(timeframe, [])) < minimum
    }
    if missing:
        return {
            "status": "DATA_ERROR",
            "signal": "NO_TRADE",
            "reason": "Insufficient completed candles.",
            "missing": missing,
            "config": asdict(config),
        }

    regime = classify_regime(candles["1h"], candles["4h"], config)
    detected: list[dict[str, Any]] = []
    for timeframe in ("4h", "1h", "15m"):
        detected.extend(detect_zones(candles[timeframe], timeframe, config))
    return evaluate_setup(candles, regime, detected, config, now=now)


def _decision_time(
    candles: dict[str, list[dict[str, Any]]],
    now: datetime | None,
) -> datetime | None:
    if now is not None:
        return now.astimezone(timezone.utc)
    last = _parse_ts((candles.get("5m") or [{}])[-1].get("ts"))
    return last + timedelta(minutes=TIMEFRAME_MINUTES["5m"]) if last else None


def _trigger_state(
    execution: list[dict[str, Any]],
    pattern_index: int,
    direction: str,
    entry: float,
    stop: float,
) -> str | None:
    """Detect a stop order whose trigger or invalidation already traded."""
    for candle in execution[pattern_index + 1 :]:
        if direction == "LONG":
            if candle["low"] <= stop:
                return "INVALIDATED_BEFORE_TRIGGER"
            if candle["high"] >= entry:
                return "ALREADY_TRIGGERED"
        else:
            if candle["high"] >= stop:
                return "INVALIDATED_BEFORE_TRIGGER"
            if candle["low"] <= entry:
                return "ALREADY_TRIGGERED"
    return None


def evaluate_setup(
    candles: dict[str, list[dict[str, Any]]],
    regime: dict[str, Any],
    detected: list[dict[str, Any]],
    config: StrategyConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply candidate, confirmation, execution-safety, and R:R gates.

    Shared by the live pipeline and the backtester so both apply identical
    rules to completed candles.
    """
    detected = sorted(detected, key=lambda zone: zone["score"], reverse=True)
    execution_atr = atr_series(candles["15m"], config.atr_period)[-1]
    current_price = candles["15m"][-1]["close"]
    if not execution_atr:
        return {
            "status": "DATA_ERROR",
            "signal": "NO_TRADE",
            "reason": "15M ATR unavailable.",
            "regime": regime,
            "zones": detected,
            "config": asdict(config),
        }

    candidates = _candidate_zones(
        detected,
        current_price,
        execution_atr,
        regime["regime"],
        regime.get("movement_state", "UNKNOWN"),
        min(candle["low"] for candle in candles["1h"][-50:]),
        max(candle["high"] for candle in candles["1h"][-50:]),
        config,
    )
    htf_filtered = 0
    if config.require_htf_alignment:
        opposing_bias = {"LONG": "DOWN", "SHORT": "UP"}
        htf_bias = regime.get("four_hour_bias", "MIXED")
        aligned = [
            (zone, direction)
            for zone, direction in candidates
            if htf_bias != opposing_bias[direction]
        ]
        htf_filtered = len(candidates) - len(aligned)
        candidates = aligned
    decided_at = _decision_time(candles, now)
    if (
        candidates
        and decided_at is not None
        and decided_at.hour in config.blocked_utc_hours
    ):
        return {
            "status": "NO_TRADE",
            "signal": "NO_TRADE",
            "reason": (
                f"Session blocked: {decided_at.hour:02d}:00 UTC is inside the "
                "rollover/maintenance window where spreads widen."
            ),
            "regime": regime,
            "current_chart_price": round(current_price, 2),
            "atr_15m": round(execution_atr, 4),
            "candidate_zones": [zone for zone, _ in candidates],
            "zones": detected,
            "config": asdict(config),
        }
    diagnostics: list[dict[str, Any]] = []
    for zone, direction in candidates:
        for execution_tf in ("5m", "15m"):
            execution = candles[execution_tf]
            pattern = _pattern_in_zone(execution, zone, direction, config)
            crossover = _recent_crossover(execution, direction, config)
            diagnostics.append(
                {
                    "zone_id": zone["id"],
                    "direction": direction,
                    "execution_timeframe": execution_tf,
                    "pattern": pattern,
                    "crossover": crossover,
                }
            )
            if not pattern or not crossover:
                diagnostics[-1]["rejection"] = (
                    "NO_PATTERN_OR_CROSSOVER"
                    if not pattern and not crossover
                    else "NO_PATTERN"
                    if not pattern
                    else "NO_CROSSOVER"
                )
                continue
            if abs(pattern["index"] - crossover["index"]) > config.crossover_lookback:
                diagnostics[-1]["rejection"] = "CONFIRMATIONS_NOT_ALIGNED"
                continue

            if direction == "LONG":
                entry = pattern["high"] + config.entry_buffer_atr * execution_atr
                stop = min(zone["low"], pattern["low"]) - config.stop_buffer_atr * execution_atr
                order_type = "BUY_STOP"
            else:
                entry = pattern["low"] - config.entry_buffer_atr * execution_atr
                stop = max(zone["high"], pattern["high"]) + config.stop_buffer_atr * execution_atr
                order_type = "SELL_STOP"
            entry = round(entry, 2)
            stop = round(stop, 2)
            trigger_state = _trigger_state(
                execution, pattern["index"], direction, entry, stop
            )
            diagnostics[-1]["trigger_state"] = trigger_state
            if trigger_state:
                diagnostics[-1]["rejection"] = trigger_state
                continue
            target, target_source, reward_risk = _target_for(
                direction,
                entry,
                stop,
                detected,
                candles["1h"],
                config,
            )
            if target is None or reward_risk is None:
                diagnostics[-1]["rejection"] = "NO_TARGET"
                continue
            net_reward = abs(target - entry) - config.round_trip_cost
            net_risk = abs(entry - stop) + config.round_trip_cost
            reward_risk_net = round(net_reward / net_risk, 2)
            if reward_risk_net < config.min_reward_risk:
                diagnostics[-1]["rejection"] = "REWARD_RISK"
                continue

            execution_minutes = TIMEFRAME_MINUTES[execution_tf]
            return {
                "status": "ACTIONABLE",
                "signal": direction,
                "order_type": order_type,
                "entry": entry,
                "stop_loss": stop,
                "target_1": target,
                "reward_risk": reward_risk,
                "reward_risk_net": reward_risk_net,
                "target_source": target_source,
                "order_expiry_minutes": config.order_expiry_bars * execution_minutes,
                "valid_until": (
                    (
                        decided_at
                        + timedelta(minutes=config.order_expiry_bars * execution_minutes)
                    ).isoformat()
                    if decided_at
                    else None
                ),
                "regime": regime,
                "selected_zone": zone,
                "confirmation": {
                    "execution_timeframe": execution_tf,
                    "candlestick_pattern": pattern,
                    "ema_crossover": crossover,
                },
                "current_chart_price": round(current_price, 2),
                "atr_15m": round(execution_atr, 4),
                "zones": detected,
                "diagnostics": diagnostics,
                "config": asdict(config),
                "strategy_rule": (
                    "UPTREND buys demand; DOWNTREND sells supply; CONSOLIDATING "
                    "trades only an outer zone. Every entry requires zone contact, "
                    "a closed reversal candle, and a 9/21 EMA crossover."
                ),
            }

    status = "WAIT" if candidates else "NO_TRADE"
    if any(item.get("trigger_state") for item in diagnostics):
        reason = (
            "The confirmed setup already traded through its entry or stop before "
            "an order could be placed; not chasing it."
        )
    elif candidates:
        reason = "Price is at an eligible zone, but the closed candlestick and EMA crossover confirmations are incomplete."
    elif htf_filtered:
        reason = "The only zones at price oppose the 4H bias."
    else:
        reason = "Price is not at a qualified fresh supply/demand zone for the current regime."
    return {
        "status": status,
        "signal": "NO_TRADE",
        "reason": reason,
        "htf_filtered": htf_filtered,
        "regime": regime,
        "current_chart_price": round(current_price, 2),
        "atr_15m": round(execution_atr, 4),
        "candidate_zones": [zone for zone, _ in candidates],
        "zones": detected,
        "diagnostics": diagnostics,
        "config": asdict(config),
    }
