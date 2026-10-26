"""Pandas/NumPy market-structure and dynamic zone calculations."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def candles_frame(candles: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(candles).copy()
    required = ["open", "high", "low", "close"]
    if frame.empty or any(column not in frame for column in required):
        return pd.DataFrame(columns=["ts", *required, "volume"])
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "volume" not in frame:
        frame["volume"] = np.nan
    else:
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    if "ts" not in frame:
        frame["ts"] = None
    return frame.dropna(subset=required).reset_index(drop=True)


def add_atr(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    result = frame.copy()
    previous_close = result["close"].shift(1)
    ranges = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    )
    result["true_range"] = ranges.max(axis=1)
    result["atr"] = result["true_range"].ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period,
    ).mean()
    return result


def add_fractal_pivots(
    frame: pd.DataFrame,
    *,
    left_bars: int = 2,
    right_bars: int = 2,
) -> pd.DataFrame:
    """Mark pivots only after N bars on both sides have confirmed them."""
    result = frame.copy()
    window = left_bars + right_bars + 1
    highs = result["high"].rolling(window=window, center=True).max()
    lows = result["low"].rolling(window=window, center=True).min()
    result["pivot_high"] = np.isclose(result["high"], highs, equal_nan=False)
    result["pivot_low"] = np.isclose(result["low"], lows, equal_nan=False)
    if right_bars:
        result.loc[len(result) - right_bars :, ["pivot_high", "pivot_low"]] = False
    return result


def add_structure_columns(
    candles: list[dict[str, Any]],
    *,
    atr_period: int = 14,
    pivot_left: int = 2,
    pivot_right: int = 2,
) -> pd.DataFrame:
    frame = candles_frame(candles)
    if frame.empty:
        return frame
    return add_fractal_pivots(
        add_atr(frame, atr_period),
        left_bars=pivot_left,
        right_bars=pivot_right,
    )


def _rma(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def adx_series(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = frame["high"].diff()
    down_move = -frame["low"].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=frame.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=frame.index,
    )
    atr = _rma(frame["true_range"], period)
    plus_di = 100.0 * _rma(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100.0 * _rma(minus_dm, period) / atr.replace(0, np.nan)
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    return _rma(dx.fillna(0.0), period)


def _recent_impulse(
    frame: pd.DataFrame,
    *,
    impulse_atr: float,
    max_bars: int = 3,
) -> dict[str, Any] | None:
    if frame.empty or pd.isna(frame.iloc[-1]["atr"]):
        return None
    end = len(frame) - 1
    atr = float(frame.iloc[end]["atr"])
    candidates: list[dict[str, Any]] = []
    for bars in range(1, max_bars + 1):
        baseline = end - bars
        if baseline < 0:
            continue
        move = float(frame.iloc[end]["close"] - frame.iloc[baseline]["close"])
        normalized = abs(move) / atr if atr > 0 else 0.0
        candidates.append(
            {
                "bars": bars,
                "direction": "UP" if move > 0 else "DOWN",
                "move_atr": normalized,
            }
        )
    strongest = max(candidates, key=lambda item: item["move_atr"], default=None)
    return strongest if strongest and strongest["move_atr"] >= impulse_atr else None


def classify_movement_state(
    candles: list[dict[str, Any]],
    *,
    atr_period: int = 14,
    impulse_atr: float = 1.5,
    grind_window: int = 6,
    pivot_left: int = 2,
    pivot_right: int = 2,
) -> dict[str, Any]:
    """Classify impulse, grind, directional trend, or consolidation objectively."""
    frame = add_structure_columns(
        candles,
        atr_period=atr_period,
        pivot_left=pivot_left,
        pivot_right=pivot_right,
    )
    if len(frame) < max(55, atr_period * 2):
        return {"regime": "UNKNOWN", "movement_state": "UNKNOWN"}

    frame["ema20"] = frame["close"].ewm(span=20, adjust=False).mean()
    frame["ema50"] = frame["close"].ewm(span=50, adjust=False).mean()
    frame["adx"] = adx_series(frame, atr_period)
    latest = frame.iloc[-1]
    atr = float(latest["atr"])
    if not np.isfinite(atr) or atr <= 0:
        return {"regime": "UNKNOWN", "movement_state": "UNKNOWN"}

    recent = frame.tail(grind_window)
    x = np.arange(len(recent), dtype=float)
    slope = float(np.polyfit(x, recent["close"].to_numpy(dtype=float), 1)[0])
    slope_atr_per_bar = slope / atr
    ranges = (recent["high"] - recent["low"]).replace(0, np.nan)
    body_ratio = ((recent["close"] - recent["open"]).abs() / ranges).fillna(0.0)
    average_range_atr = float(ranges.mean() / atr)
    overlap_ratios: list[float] = []
    for index in range(1, len(recent)):
        previous = recent.iloc[index - 1]
        current = recent.iloc[index]
        overlap = max(
            0.0,
            min(float(previous["high"]), float(current["high"]))
            - max(float(previous["low"]), float(current["low"])),
        )
        smaller_range = min(
            float(previous["high"] - previous["low"]),
            float(current["high"] - current["low"]),
        )
        overlap_ratios.append(overlap / smaller_range if smaller_range > 0 else 0.0)
    overlap = float(np.mean(overlap_ratios)) if overlap_ratios else 0.0
    travel = float(abs(recent.iloc[-1]["close"] - recent.iloc[0]["close"]))
    path = float(recent["close"].diff().abs().sum())
    efficiency = travel / path if path > 0 else 0.0

    impulse = _recent_impulse(frame, impulse_atr=impulse_atr)
    grind = (
        abs(slope_atr_per_bar) >= 0.12
        and average_range_atr <= 1.05
        and float(body_ratio.mean()) <= 0.48
        and overlap >= 0.35
        and efficiency >= 0.55
    )
    ema_spread_atr = float(abs(latest["ema20"] - latest["ema50"]) / atr)
    ema_slope_atr = float((frame.iloc[-1]["ema20"] - frame.iloc[-6]["ema20"]) / atr)
    adx = float(latest["adx"]) if np.isfinite(latest["adx"]) else None

    if impulse:
        movement_state = f"IMPULSE_{impulse['direction']}"
    elif grind:
        movement_state = "GRIND_UP" if slope > 0 else "GRIND_DOWN"
    elif (adx is not None and adx < 20) or ema_spread_atr < 0.25:
        movement_state = "CONSOLIDATION"
    elif latest["ema20"] > latest["ema50"] and ema_slope_atr > 0.10:
        movement_state = "TREND_UP"
    elif latest["ema20"] < latest["ema50"] and ema_slope_atr < -0.10:
        movement_state = "TREND_DOWN"
    else:
        movement_state = "CONSOLIDATION"

    if movement_state.endswith("_UP"):
        regime = "UPTREND"
    elif movement_state.endswith("_DOWN"):
        regime = "DOWNTREND"
    else:
        regime = "CONSOLIDATING"
    return {
        "regime": regime,
        "movement_state": movement_state,
        "atr14": round(atr, 4),
        "adx14": round(adx, 2) if adx is not None else None,
        "ema20": round(float(latest["ema20"]), 2),
        "ema50": round(float(latest["ema50"]), 2),
        "ema_spread_atr": round(ema_spread_atr, 3),
        "ema20_slope_5bar_atr": round(ema_slope_atr, 3),
        "grind_metrics": {
            "window_bars": grind_window,
            "slope_atr_per_bar": round(slope_atr_per_bar, 3),
            "average_range_atr": round(average_range_atr, 3),
            "average_body_to_range": round(float(body_ratio.mean()), 3),
            "average_overlap": round(overlap, 3),
            "directional_efficiency": round(efficiency, 3),
        },
        "recent_impulse": (
            {
                "direction": impulse["direction"],
                "bars": impulse["bars"],
                "move_atr": round(float(impulse["move_atr"]), 3),
            }
            if impulse
            else None
        ),
    }


def _prior_structure(
    highs: np.ndarray,
    lows: np.ndarray,
    pivot_high_idx: np.ndarray,
    pivot_low_idx: np.ndarray,
    before_index: int,
) -> str:
    prior_highs = pivot_high_idx[pivot_high_idx < before_index]
    prior_lows = pivot_low_idx[pivot_low_idx < before_index]
    if len(prior_highs) >= 2 and len(prior_lows) >= 2:
        last_high, previous_high = highs[prior_highs[-1]], highs[prior_highs[-2]]
        last_low, previous_low = lows[prior_lows[-1]], lows[prior_lows[-2]]
        if last_high > previous_high and last_low > previous_low:
            return "UP"
        if last_high < previous_high and last_low < previous_low:
            return "DOWN"
    return "MIXED"


def _touch_and_mitigation(
    highs: np.ndarray,
    lows: np.ndarray,
    *,
    zone_type: str,
    zone_low: float,
    zone_high: float,
) -> tuple[int, float]:
    width = zone_high - zone_low
    if highs.size == 0 or width <= 0:
        return 0, 0.0
    intersects = (lows <= zone_high) & (highs >= zone_low)
    if not intersects.any():
        return 0, 0.0
    entries = intersects & ~np.concatenate(([False], intersects[:-1]))
    if zone_type == "DEMAND":
        penetration = (zone_high - float(lows[intersects].min())) / width
    else:
        penetration = (float(highs[intersects].max()) - zone_low) / width
    return int(entries.sum()), float(np.clip(penetration, 0.0, 1.0))


def dynamic_zones(
    candles: list[dict[str, Any]],
    timeframe: str,
    *,
    atr_period: int = 14,
    pivot_left: int = 2,
    pivot_right: int = 2,
    pivot_lookback: int = 30,
    impulse_atr: float = 1.5,
    impulse_max_bars: int = 3,
    base_max_bars: int = 4,
    max_anchor_gap_bars: int = 6,
    base_body_atr: float = 0.6,
    max_zone_width_atr: float = 2.0,
) -> list[dict[str, Any]]:
    """Recompute pivot-anchored zones as of the latest completed bar.

    The anchor pivot must sit within ``max_anchor_gap_bars`` of the departure,
    so an old swing can never be attributed to an unrelated impulse. Base
    candles with bodies larger than ``base_body_atr`` are leg-in/leg-out
    candles and do not widen the proximal edge.
    """
    frame = add_structure_columns(
        candles,
        atr_period=atr_period,
        pivot_left=pivot_left,
        pivot_right=pivot_right,
    )
    if len(frame) < atr_period + pivot_left + pivot_right + impulse_max_bars:
        return []

    opens = frame["open"].to_numpy(dtype=float)
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    atrs = frame["atr"].to_numpy(dtype=float)
    timestamps = frame["ts"].tolist()
    pivot_high_idx = np.flatnonzero(frame["pivot_high"].to_numpy(dtype=bool))
    pivot_low_idx = np.flatnonzero(frame["pivot_low"].to_numpy(dtype=bool))
    size = len(frame)

    events: dict[tuple[str, int], dict[str, Any]] = {}
    for end in range(atr_period + pivot_right, size):
        atr = atrs[end]
        if not np.isfinite(atr) or atr <= 0:
            continue
        strongest: dict[str, Any] | None = None
        for bars in range(1, impulse_max_bars + 1):
            baseline = end - bars
            if baseline < 0:
                continue
            move = closes[end] - closes[baseline]
            move_atr = abs(move) / atr
            if move_atr < impulse_atr:
                continue
            if strongest is None or move_atr > strongest["move_atr"]:
                strongest = {
                    "start": baseline + 1,
                    "end": end,
                    "bars": bars,
                    "direction": "UP" if move > 0 else "DOWN",
                    "move_atr": float(move_atr),
                }
        if not strongest:
            continue

        pivots = pivot_low_idx if strongest["direction"] == "UP" else pivot_high_idx
        position = int(np.searchsorted(pivots, strongest["start"])) - 1
        if position < 0:
            continue
        anchor = int(pivots[position])
        gap = strongest["start"] - anchor
        if gap > min(pivot_lookback, max_anchor_gap_bars):
            continue
        key = (
            "DEMAND" if strongest["direction"] == "UP" else "SUPPLY",
            anchor,
        )
        existing = events.get(key)
        if existing is None or strongest["move_atr"] > existing["move_atr"]:
            strongest["anchor"] = anchor
            events[key] = strongest

    zones: list[dict[str, Any]] = []
    for (zone_type, anchor), event in events.items():
        atr = float(atrs[event["end"]])
        base_end = min(event["start"] - 1, anchor + base_max_bars - 1)
        anchor_boring = abs(closes[anchor] - opens[anchor]) <= base_body_atr * atr
        # A large-bodied pivot candle is itself the leg-in, so earlier candles
        # belong to the prior move rather than the base.
        base_start = anchor - 1 if anchor_boring and anchor > 0 else anchor
        proximal_edges: list[float] = []
        for index in range(base_start, base_end + 1):
            body_low = min(opens[index], closes[index])
            body_high = max(opens[index], closes[index])
            boring = (body_high - body_low) <= base_body_atr * atr
            if index == anchor:
                if zone_type == "DEMAND":
                    proximal_edges.append(body_high if boring else body_low)
                else:
                    proximal_edges.append(body_low if boring else body_high)
            elif boring:
                proximal_edges.append(body_high if zone_type == "DEMAND" else body_low)
        base_bars = len(proximal_edges)

        if zone_type == "DEMAND":
            zone_low = float(lows[anchor])
            zone_high = float(max(proximal_edges))
            opposite = pivot_high_idx[pivot_high_idx < event["start"]]
            broke_structure = bool(
                opposite.size and closes[event["end"]] > highs[opposite[-1]]
            )
        else:
            zone_high = float(highs[anchor])
            zone_low = float(min(proximal_edges))
            opposite = pivot_low_idx[pivot_low_idx < event["start"]]
            broke_structure = bool(
                opposite.size and closes[event["end"]] < lows[opposite[-1]]
            )
        if zone_high <= zone_low:
            continue
        width_atr = (zone_high - zone_low) / atr if atr > 0 else 99.0
        if width_atr > max_zone_width_atr:
            continue

        touches, mitigation = _touch_and_mitigation(
            highs[event["end"] + 1 :],
            lows[event["end"] + 1 :],
            zone_type=zone_type,
            zone_low=zone_low,
            zone_high=zone_high,
        )
        if zone_type == "DEMAND":
            later = pivot_low_idx[pivot_low_idx > event["end"]]
            invalidated = bool((lows[later] < zone_low).any())
        else:
            later = pivot_high_idx[pivot_high_idx > event["end"]]
            invalidated = bool((highs[later] > zone_high).any())

        incoming_start = max(0, anchor - 3)
        incoming_move = closes[anchor] - closes[incoming_start]
        if zone_type == "DEMAND":
            formation = "RBR" if incoming_move > 0 else "DBR"
            direction = "UP"
        else:
            formation = "DBD" if incoming_move < 0 else "RBD"
            direction = "DOWN"
        prior = _prior_structure(
            highs, lows, pivot_high_idx, pivot_low_idx, event["start"]
        )
        event_kind = "CHOCH" if prior not in {"MIXED", direction} else "BOS"
        structure_event = f"{event_kind}_{direction}" if broke_structure else "NONE"

        tightness = float(np.clip(1.0 - width_atr / 1.5, 0.0, 1.0))
        departure_score = float(np.clip(event["move_atr"] / 2.5, 0.0, 1.0))
        freshness = 1.0 - mitigation
        score = (
            0.40 * departure_score
            + 0.20 * tightness
            + 0.25 * freshness
            + 0.15 * (1.0 if broke_structure else 0.25)
        )
        anchor_ts = timestamps[anchor]
        zones.append(
            {
                "id": f"{timeframe}-{zone_type.lower()}-{anchor_ts or anchor}",
                "timeframe": timeframe,
                "type": zone_type,
                "formation": formation,
                "low": round(zone_low, 2),
                "high": round(zone_high, 2),
                "proximal": round(zone_high if zone_type == "DEMAND" else zone_low, 2),
                "distal": round(zone_low if zone_type == "DEMAND" else zone_high, 2),
                "width_atr": round(float(width_atr), 3),
                "anchor_pivot_index": anchor,
                "anchor_pivot_time": anchor_ts,
                "pivot_confirmation_bars": pivot_right,
                "formed_at": timestamps[event["start"]],
                "as_of_bar": timestamps[-1],
                "base_bars": base_bars,
                "departure_bars": int(event["bars"]),
                "departure_atr": round(float(event["move_atr"]), 3),
                "broke_structure": broke_structure,
                "structure_event": structure_event,
                "touches": touches,
                "mitigation_pct": round(mitigation * 100.0, 1),
                "freshness_pct": round(freshness * 100.0, 1),
                "fresh": mitigation == 0.0,
                "invalidated": invalidated,
                "invalidation_reason": (
                    "confirmed_pivot_beyond_distal_edge" if invalidated else None
                ),
                "score": round(float(score), 3),
                "atr_at_formation": round(atr, 4),
                "departure_end_index": int(event["end"]),
            }
        )

    active = [zone for zone in zones if not zone["invalidated"]]
    active.sort(
        key=lambda zone: (
            zone["departure_end_index"],
            zone["score"],
        ),
        reverse=True,
    )
    return active[:16]
