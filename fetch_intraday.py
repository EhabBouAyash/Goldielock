"""
Intraday data fetcher for gold trading analysis.
Fetches 5m, 15m, 1h, and 4h bars and calculates short-term technicals.

Uses Yahoo chart API directly (with SSL bypass) via fetch_market_data module
since yfinance is broken due to system clock skew causing SSL cert failures.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import warnings
import time
warnings.filterwarnings('ignore')

from fetch_market_data import fetch_xauusd_chart_bars

INTRADAY_JSON_PATH = Path(__file__).resolve().parent / "intraday_data.json"
CONFLUENCE_FEATURES = [
    "price_vs_sma9",
    "ma_stack",
    "rsi_centered",
    "range_position",
    "momentum_5bar",
    "volume_pressure",
]
CONFLUENCE_FEATURE_WEIGHTS = {
    "price_vs_sma9": 0.25,
    "ma_stack": 0.25,
    "rsi_centered": 0.15,
    "range_position": 0.10,
    "momentum_5bar": 0.20,
    "volume_pressure": 0.05,
}
TIMEFRAME_ORDER = ["5m", "15m", "1h", "4h"]
TIMEFRAME_WEIGHTS = {"5m": 0.15, "15m": 0.20, "1h": 0.25, "4h": 0.40}
# Per-timeframe caps preserve broader context on slower charts while limiting
# stale microstructure on the faster execution charts.
CANDLE_LIMITS = {"5m": 120, "15m": 150, "1h": 170, "4h": 96}
DEFAULT_CANDLE_LIMIT = 96

# Entry fill-discipline: max |entry − spot| as a fraction of ATR(15M).
ENTRY_OFFSET_ATR_MULT_ALIGNED = 0.5
ENTRY_OFFSET_ATR_MULT_SCALP = 0.35
# Used only when ATR(15M) cannot be computed — never invent ATR.
ENTRY_OFFSET_FALLBACK_USD = 8.0


def _clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def _sign(value, deadzone=0.0002):
    if value is None:
        return 0.0
    if value > deadzone:
        return 1.0
    if value < -deadzone:
        return -1.0
    return 0.0


def _float_or_none(value):
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ohlcv_to_records(bars):
    """Serialize OHLCV bars so each cycle keeps the actual candle history."""
    records = []
    for idx, row in bars.iterrows():
        volume = _float_or_none(row.get("Volume"))
        records.append({
            "ts": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
            "volume": int(volume) if volume is not None else None,
        })
    return records


def _range_position(current_price, highs, lows):
    if not highs or not lows:
        return 0.0
    window_high = max(highs)
    window_low = min(lows)
    if window_high <= window_low:
        return 0.0
    # Convert 0..1 range location into -1..1 centered feature.
    return _clamp(((current_price - window_low) / (window_high - window_low) * 2) - 1)


def _volume_pressure(volumes):
    """
    Latest completed-bar volume vs ~20-bar baseline, clamped to [-1, 1].

    Trailing zero/None bars (typical of the still-forming candle on Yahoo GC=F)
    are dropped so volume_pressure is not artificially slammed to -1.0 on every TF.
    """
    clean = [v for v in volumes if v is not None]
    dropped_trailing = 0
    while clean and clean[-1] <= 0:
        clean.pop()
        dropped_trailing += 1
    if len(clean) < 5:
        return 0.0
    baseline = sum(clean[-20:]) / len(clean[-20:])
    if baseline <= 0:
        return 0.0
    return _clamp((clean[-1] / baseline) - 1)


def _volume_pressure_quality(volumes):
    """Return (pressure, quality dict) for validation / prompt labeling."""
    raw = list(volumes or [])
    clean = [v for v in raw if v is not None]
    dropped_trailing = 0
    while clean and clean[-1] <= 0:
        clean.pop()
        dropped_trailing += 1

    quality = {
        "usable": False,
        "dropped_trailing_zero_bars": dropped_trailing,
        "bars_with_volume": len(clean),
        "last_volume": None,
        "baseline_20": None,
        "forming_bar_volume_zero": dropped_trailing > 0,
        "source": "GC=F Yahoo chart OHLCV (independent of gold-api bid/ask)",
    }
    if len(clean) < 5:
        quality["reason"] = "insufficient_completed_volume_bars"
        return 0.0, quality

    baseline = sum(clean[-20:]) / len(clean[-20:])
    quality["usable"] = baseline > 0
    quality["last_volume"] = clean[-1]
    quality["baseline_20"] = round(baseline, 2) if baseline else None
    if baseline <= 0:
        quality["reason"] = "non_positive_volume_baseline"
        return 0.0, quality
    pressure = _clamp((clean[-1] / baseline) - 1)
    quality["reason"] = (
        "excluded_forming_zero_volume_bar"
        if dropped_trailing
        else "ok"
    )
    return pressure, quality


def _true_ranges(highs, lows, closes):
    trs = []
    for i in range(1, len(closes)):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    return trs


def _rolling_means(values, period):
    if len(values) < period:
        return []
    return [
        sum(values[i - period + 1 : i + 1]) / period
        for i in range(period - 1, len(values))
    ]


def _percentile_rank(history, value):
    if not history or value is None:
        return None
    below = sum(1 for item in history if item <= value)
    return round(100.0 * below / len(history), 1)


def _bollinger_width_series(closes, period=20, num_std=2.0):
    widths = []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        if mean <= 0:
            continue
        variance = sum((price - mean) ** 2 for price in window) / period
        widths.append((2.0 * num_std * (variance ** 0.5)) / mean)
    return widths


def _adx(highs, lows, closes, period=14):
    """Wilder-style ADX; returns current ADX or None if insufficient bars."""
    if len(closes) < period * 2 + 1:
        return None

    plus_dm = []
    minus_dm = []
    trs = []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    atr = sum(trs[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])
    dx_values = []

    for i in range(period, len(trs)):
        atr = atr - (atr / period) + trs[i]
        smooth_plus = smooth_plus - (smooth_plus / period) + plus_dm[i]
        smooth_minus = smooth_minus - (smooth_minus / period) + minus_dm[i]
        if atr <= 0:
            continue
        plus_di = 100.0 * smooth_plus / atr
        minus_di = 100.0 * smooth_minus / atr
        di_sum = plus_di + minus_di
        dx = 0.0 if di_sum == 0 else 100.0 * abs(plus_di - minus_di) / di_sum
        dx_values.append(dx)

    if len(dx_values) < period:
        return None
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period
    return round(adx, 2)


def _build_volatility_regime(closes, highs, lows, volumes):
    """
    Code-side regime label for the LLM / hard gate.
    consolidating | transitional | trending, plus ATR/BB/ADX diagnostics
    and a breakout_trigger that can override a consolidating veto.
    """
    empty = {
        "regime": "unknown",
        "label_for_prompt": "regime: unknown (insufficient bars)",
        "adx_14": None,
        "atr_14": None,
        "atr_percentile_50": None,
        "bb_width": None,
        "bb_width_percentile_50": None,
        "range_lookback_bars": 20,
        "range_high": None,
        "range_low": None,
        "breakout_trigger": False,
        "breakout_direction": None,
        "gate_blocks_entries": False,
        "reasons": ["insufficient_bars"],
    }
    if len(closes) < 40 or len(highs) != len(closes) or len(lows) != len(closes):
        return empty

    trs = _true_ranges(highs, lows, closes)
    atr_series = _rolling_means(trs, 14)
    if not atr_series:
        return empty
    atr_14 = atr_series[-1]
    atr_history = atr_series[-50:]
    atr_percentile = _percentile_rank(atr_history, atr_14)

    bb_widths = _bollinger_width_series(closes, period=20)
    bb_width = bb_widths[-1] if bb_widths else None
    bb_history = bb_widths[-50:] if bb_widths else []
    bb_percentile = _percentile_rank(bb_history, bb_width)

    adx_14 = _adx(highs, lows, closes, period=14)

    lookback = 20
    prior_highs = highs[-(lookback + 1) : -1]
    prior_lows = lows[-(lookback + 1) : -1]
    range_high = max(prior_highs) if prior_highs else None
    range_low = min(prior_lows) if prior_lows else None
    close = closes[-1]
    volume_pressure = _volume_pressure(volumes)
    if len(closes) >= 6 and closes[-6]:
        momentum_5bar = ((closes[-1] - closes[-6]) / closes[-6]) * 20
    else:
        momentum_5bar = 0.0

    breakout_direction = None
    breakout_trigger = False
    if range_high is not None and range_low is not None:
        if close > range_high and volume_pressure > 0.05 and momentum_5bar > 0:
            breakout_direction = "UP"
            breakout_trigger = True
        elif close < range_low and volume_pressure > 0.05 and momentum_5bar < 0:
            breakout_direction = "DOWN"
            breakout_trigger = True

    reasons = []
    consolidating_votes = 0
    trending_votes = 0

    if adx_14 is not None:
        if adx_14 < 20:
            consolidating_votes += 1
            reasons.append(f"ADX {adx_14} < 20")
        elif adx_14 >= 25:
            trending_votes += 1
            reasons.append(f"ADX {adx_14} >= 25")
        else:
            reasons.append(f"ADX {adx_14} transitional (20-25)")

    if atr_percentile is not None:
        if atr_percentile <= 35:
            consolidating_votes += 1
            reasons.append(f"ATR at {atr_percentile}th percentile of last {len(atr_history)}")
        elif atr_percentile >= 65:
            trending_votes += 1
            reasons.append(f"ATR at {atr_percentile}th percentile of last {len(atr_history)}")
        else:
            reasons.append(f"ATR at {atr_percentile}th percentile of last {len(atr_history)}")

    if bb_percentile is not None:
        if bb_percentile <= 35:
            consolidating_votes += 1
            reasons.append(f"BB width at {bb_percentile}th percentile of last {len(bb_history)}")
        elif bb_percentile >= 65:
            trending_votes += 1
            reasons.append(f"BB width at {bb_percentile}th percentile of last {len(bb_history)}")
        else:
            reasons.append(f"BB width at {bb_percentile}th percentile of last {len(bb_history)}")

    # Absolute compression vs price (percentile alone is noisy on flat histories).
    atr_pct_of_price = (atr_14 / close * 100.0) if close else None
    if atr_pct_of_price is not None:
        if atr_pct_of_price < 0.12:
            consolidating_votes += 1
            reasons.append(f"ATR/price {atr_pct_of_price:.3f}% < 0.12%")
        elif atr_pct_of_price > 0.35:
            trending_votes += 1
            reasons.append(f"ATR/price {atr_pct_of_price:.3f}% > 0.35%")

    # Strong ADX compression override: low ADX + either ATR or BB compressed.
    if (
        adx_14 is not None
        and adx_14 < 20
        and (
            (atr_percentile is not None and atr_percentile <= 40)
            or (bb_percentile is not None and bb_percentile <= 40)
            or (atr_pct_of_price is not None and atr_pct_of_price < 0.15)
        )
    ):
        regime = "consolidating"
    elif consolidating_votes >= 2 and consolidating_votes > trending_votes:
        regime = "consolidating"
    elif trending_votes >= 2 and trending_votes > consolidating_votes:
        regime = "trending"
    elif consolidating_votes == 0 and trending_votes == 0:
        regime = "transitional"
    else:
        regime = "transitional"

    gate_blocks_entries = regime == "consolidating" and not breakout_trigger
    atr_pct_txt = f"{atr_percentile}th" if atr_percentile is not None else "n/a"
    label = (
        f"regime: {regime}, ATR at {atr_pct_txt} percentile of last "
        f"{len(atr_history)}; ADX={adx_14 if adx_14 is not None else 'n/a'}; "
        f"BB_width_pct={bb_percentile if bb_percentile is not None else 'n/a'}; "
        f"breakout_trigger={breakout_trigger}"
        + (f" ({breakout_direction})" if breakout_direction else "")
    )

    return {
        "regime": regime,
        "label_for_prompt": label,
        "adx_14": adx_14,
        "atr_14": round(atr_14, 4) if atr_14 is not None else None,
        "atr_percentile_50": atr_percentile,
        "atr_pct_of_price": round(atr_pct_of_price, 4) if atr_pct_of_price is not None else None,
        "bb_width": round(bb_width, 6) if bb_width is not None else None,
        "bb_width_percentile_50": bb_percentile,
        "range_lookback_bars": lookback,
        "range_high": round(range_high, 2) if range_high is not None else None,
        "range_low": round(range_low, 2) if range_low is not None else None,
        "breakout_trigger": breakout_trigger,
        "breakout_direction": breakout_direction,
        "gate_blocks_entries": gate_blocks_entries,
        "volume_pressure": round(volume_pressure, 4),
        "momentum_5bar": round(momentum_5bar, 4),
        "reasons": reasons,
    }


def _build_regime_summary(intervals):
    """Primary execution regime from 15m (fallback 5m), with hard-gate flag."""
    primary_tf = None
    for tf in ("15m", "5m", "1h"):
        data = intervals.get(tf, {})
        if isinstance(data, dict) and data.get("volatility_regime") and "error" not in data:
            primary_tf = tf
            break

    if not primary_tf:
        return {
            "primary_timeframe": None,
            "regime": "unknown",
            "label_for_prompt": "regime: unknown (no usable bars)",
            "gate_blocks_entries": False,
            "breakout_trigger": False,
            "by_timeframe": {},
        }

    primary = intervals[primary_tf]["volatility_regime"]
    by_tf = {
        tf: data.get("volatility_regime")
        for tf, data in intervals.items()
        if isinstance(data, dict) and data.get("volatility_regime")
    }

    # Confirm chop: if 15m consolidating OR (15m transitional and 5m consolidating).
    regime_15 = (intervals.get("15m") or {}).get("volatility_regime") or {}
    regime_5 = (intervals.get("5m") or {}).get("volatility_regime") or {}
    confirmed_consolidating = (
        regime_15.get("regime") == "consolidating"
        or (
            regime_15.get("regime") == "transitional"
            and regime_5.get("regime") == "consolidating"
        )
    )
    breakout = bool(primary.get("breakout_trigger"))
    gate_blocks = confirmed_consolidating and not breakout

    label = primary.get("label_for_prompt", "regime: unknown")
    if primary_tf != "15m":
        label = f"{label} [primary_tf={primary_tf}]"
    if gate_blocks:
        label = f"{label} | CODE_GATE: blocks new entries until breakout_trigger"

    return {
        "primary_timeframe": primary_tf,
        "regime": "consolidating" if confirmed_consolidating else primary.get("regime"),
        "label_for_prompt": label,
        "gate_blocks_entries": gate_blocks,
        "breakout_trigger": breakout,
        "breakout_direction": primary.get("breakout_direction"),
        "range_high": primary.get("range_high"),
        "range_low": primary.get("range_low"),
        "adx_14": primary.get("adx_14"),
        "atr_percentile_50": primary.get("atr_percentile_50"),
        "bb_width_percentile_50": primary.get("bb_width_percentile_50"),
        "reasons": primary.get("reasons", []),
        "by_timeframe": {
            tf: {
                "regime": reg.get("regime"),
                "adx_14": reg.get("adx_14"),
                "atr_percentile_50": reg.get("atr_percentile_50"),
                "bb_width_percentile_50": reg.get("bb_width_percentile_50"),
                "breakout_trigger": reg.get("breakout_trigger"),
                "gate_blocks_entries": reg.get("gate_blocks_entries"),
                "range_high": reg.get("range_high"),
                "range_low": reg.get("range_low"),
                "label_for_prompt": reg.get("label_for_prompt"),
            }
            for tf, reg in by_tf.items()
            if isinstance(reg, dict)
        },
    }


def _build_volume_quality_summary(intervals):
    """Cross-TF volume feed health (GC=F chart volumes, not gold-api bid/ask)."""
    by_tf = {}
    forming_zero = 0
    usable = 0
    pressures = {}
    for tf in TIMEFRAME_ORDER:
        data = intervals.get(tf, {})
        if not isinstance(data, dict) or data.get("error"):
            continue
        quality = data.get("volume_quality") or {}
        vector = data.get("confluence_vector") or {}
        by_tf[tf] = {
            **quality,
            "volume_pressure": vector.get("volume_pressure"),
        }
        if quality.get("forming_bar_volume_zero"):
            forming_zero += 1
        if quality.get("usable"):
            usable += 1
        if vector.get("volume_pressure") is not None:
            pressures[tf] = vector.get("volume_pressure")

    all_neg_one = (
        len(pressures) >= 2
        and all(abs(float(v) + 1.0) < 1e-9 for v in pressures.values())
    )
    degraded = forming_zero > 0 or usable == 0
    label = (
        f"volume_pressure source=GC=F Yahoo OHLCV (independent of gold-api bid/ask); "
        f"usable_tfs={usable}/{len(by_tf)}; forming_zero_bars_dropped={forming_zero}; "
        f"pressures={pressures}"
    )
    if all_neg_one and forming_zero:
        label += " | NOTE: prior -1.0-everywhere pattern was forming-bar artifact; zeros dropped"
    return {
        "label_for_prompt": label,
        "by_timeframe": by_tf,
        "usable_timeframes": usable,
        "forming_zero_bars_dropped": forming_zero,
        "degraded": degraded and usable == 0,
        "independent_of_bid_ask": True,
        "pressures": pressures,
    }


def _build_entry_fill_guide(intervals):
    """
    Code-computed max entry offset from spot for fill-rate discipline.
    Prefer live ATR(15M); if missing, use a fixed dollar fallback (never invent ATR).
    """
    atr_15m = None
    atr_source = None
    bar_15 = intervals.get("15m") or {}
    if isinstance(bar_15, dict) and "error" not in bar_15:
        atr_15m = bar_15.get("atr_14")
        if atr_15m is None:
            atr_15m = (bar_15.get("volatility_regime") or {}).get("atr_14")
        if atr_15m is not None:
            atr_source = "15m ATR(14) from GC=F chart bars"

    if atr_15m is not None and atr_15m > 0:
        max_aligned = round(ENTRY_OFFSET_ATR_MULT_ALIGNED * float(atr_15m), 2)
        max_scalp = round(ENTRY_OFFSET_ATR_MULT_SCALP * float(atr_15m), 2)
        label = (
            f"ATR(15M)=${float(atr_15m):.2f}/oz | "
            f"max entry_offset aligned={ENTRY_OFFSET_ATR_MULT_ALIGNED}×ATR "
            f"(${max_aligned}) | scalp={ENTRY_OFFSET_ATR_MULT_SCALP}×ATR (${max_scalp})"
        )
        return {
            "atr_15m": round(float(atr_15m), 4),
            "atr_source": atr_source,
            "max_entry_offset_aligned_usd": max_aligned,
            "max_entry_offset_scalp_usd": max_scalp,
            "mult_aligned": ENTRY_OFFSET_ATR_MULT_ALIGNED,
            "mult_scalp": ENTRY_OFFSET_ATR_MULT_SCALP,
            "fallback_used": False,
            "label_for_prompt": label,
        }

    label = (
        f"ATR(15M) unavailable — use fixed fallback max entry_offset "
        f"${ENTRY_OFFSET_FALLBACK_USD:.2f}/oz (do NOT invent ATR)"
    )
    return {
        "atr_15m": None,
        "atr_source": None,
        "max_entry_offset_aligned_usd": ENTRY_OFFSET_FALLBACK_USD,
        "max_entry_offset_scalp_usd": round(ENTRY_OFFSET_FALLBACK_USD * 0.7, 2),
        "mult_aligned": None,
        "mult_scalp": None,
        "fallback_used": True,
        "fallback_usd": ENTRY_OFFSET_FALLBACK_USD,
        "label_for_prompt": label,
    }


def _build_confluence_vector(closes, highs, lows, volumes, sma_9, sma_21, sma_50, rsi):
    current_price = closes[-1]
    price_vs_sma9 = (
        _sign((current_price - sma_9) / current_price)
        if sma_9 and current_price
        else 0.0
    )

    if sma_9 and sma_21 and sma_50:
        if current_price > sma_9 > sma_21 > sma_50:
            ma_stack = 1.0
        elif current_price < sma_9 < sma_21 < sma_50:
            ma_stack = -1.0
        else:
            ma_stack = 0.0
    elif sma_9 and sma_21:
        if current_price > sma_9 > sma_21:
            ma_stack = 0.75
        elif current_price < sma_9 < sma_21:
            ma_stack = -0.75
        else:
            ma_stack = 0.0
    else:
        ma_stack = 0.0

    rsi_centered = _clamp(((rsi or 50) - 50) / 50)
    range_pos = _range_position(current_price, highs, lows)

    if len(closes) >= 6 and closes[-6]:
        momentum_5bar = _clamp(((closes[-1] - closes[-6]) / closes[-6]) * 20)
    else:
        momentum_5bar = 0.0

    volume_pressure, volume_quality = _volume_pressure_quality(volumes)

    vector = {
        "price_vs_sma9": round(price_vs_sma9, 4),
        "ma_stack": round(ma_stack, 4),
        "rsi_centered": round(rsi_centered, 4),
        "range_position": round(range_pos, 4),
        "momentum_5bar": round(momentum_5bar, 4),
        "volume_pressure": round(volume_pressure, 4),
    }
    directional_score = sum(
        vector[name] * CONFLUENCE_FEATURE_WEIGHTS[name]
        for name in CONFLUENCE_FEATURES
    )
    return vector, round(directional_score * 10, 2), volume_quality


def _build_confluence_tensor(intervals):
    matrix = []
    timeframe_scores = {}
    aggregate = 0.0
    total_weight = 0.0

    for timeframe in TIMEFRAME_ORDER:
        data = intervals.get(timeframe, {})
        vector = data.get("confluence_vector") or {name: 0.0 for name in CONFLUENCE_FEATURES}
        matrix.append([vector.get(name, 0.0) for name in CONFLUENCE_FEATURES])
        score = data.get("confluence_score")
        if score is not None and "error" not in data:
            weight = TIMEFRAME_WEIGHTS.get(timeframe, 0.0)
            aggregate += float(score) * weight
            total_weight += weight
            timeframe_scores[timeframe] = score

    aggregate_score = round(aggregate / total_weight, 2) if total_weight else 0.0
    if aggregate_score >= 4:
        label = "BULLISH_CONFLUENCE"
    elif aggregate_score <= -4:
        label = "BEARISH_CONFLUENCE"
    elif aggregate_score > 1:
        label = "MIXED_BULLISH_LEAN"
    elif aggregate_score < -1:
        label = "MIXED_BEARISH_LEAN"
    else:
        label = "MIXED_OR_FLAT"

    return {
        "feature_names": CONFLUENCE_FEATURES,
        "feature_weights": CONFLUENCE_FEATURE_WEIGHTS,
        "timeframe_order": TIMEFRAME_ORDER,
        "timeframe_weights": TIMEFRAME_WEIGHTS,
        "candle_limits": CANDLE_LIMITS,
        "matrix": matrix,
        "timeframe_scores": timeframe_scores,
        "aggregate_score": aggregate_score,
        "alignment_label": label,
        "scale_note": (
            "Feature values are normalized -1..1; scores are directional -10 bearish to +10 bullish. "
            "Candle limits cap prompt context per timeframe to preserve signal quality."
        ),
    }

def calculate_rsi(prices, period=14):
    """Calculate RSI from a list of prices."""
    if len(prices) < period + 1:
        return None
    
    prices = pd.Series(prices)
    deltas = prices.diff()[1:]  # Skip first NaN
    if len(deltas) == 0:
        return None
    
    seed = deltas[:period].mean()
    up = deltas[deltas > 0].sum() / period
    down = -deltas[deltas < 0].sum() / period
    
    rs = up / down if down != 0 else 0
    rsi = 100.0 - (100.0 / (1.0 + rs)) if rs > 0 else 50
    return rsi


def _fetch_intraday_ohlcv(symbol: str, interval: str) -> tuple[pd.DataFrame | None, str | None]:
    """
    Pull intraday OHLCV using Yahoo chart API directly (SSL bypass).
    Uses GC=F as chart proxy for XAUUSD since XAUUSD=X is delisted on Yahoo.
    """
    range_val = "1mo" if interval in ("1h", "4h") else "5d"
    try:
        df = fetch_xauusd_chart_bars(interval, range_val)
        if df is not None and not df.empty:
            return df, None
        return None, "No data returned from chart API"
    except Exception as exc:
        return None, str(exc)


def fetch_intraday_bars(symbol="XAUUSD", intervals=None, retries=3, backoff_base=2):
    """
    Fetch intraday bars for XAUUSD (via GC=F chart proxy) with retry logic.
    Returns dict with keys like '5m', '15m', '1h', '4h' containing OHLCV + technicals.
    
    Args:
        symbol: Logical symbol name (used for labeling, actual data from GC=F proxy)
        intervals: List of timeframes (5m, 15m, 1h, 4h)
        retries: Number of retry attempts on failure
        backoff_base: Exponential backoff multiplier (2 = 1s, 2s, 4s)
    """
    if intervals is None:
        intervals = TIMEFRAME_ORDER

    data = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "chart_proxy": "GC=F",
        "intervals": {}
    }
    
    for interval in intervals:
        hist = None
        last_error = None
        
        for attempt in range(retries):
            try:
                hist, fetch_err = _fetch_intraday_ohlcv(symbol, interval)
                if hist is None or hist.empty:
                    last_error = fetch_err or "No data returned"
                    if attempt < retries - 1:
                        time.sleep(backoff_base ** attempt)
                        continue
                    data["intervals"][interval] = {"error": last_error, "attempt": attempt + 1}
                    break
                break
            except Exception as e:
                last_error = str(e)
                if attempt < retries - 1:
                    time.sleep(backoff_base ** attempt)
                    continue
                data["intervals"][interval] = {"error": last_error, "attempt": attempt + 1}
                break
        
        # If we got data, process it
        if hist is not None and not hist.empty:
            try:
            
                # Handle MultiIndex columns (yfinance structure)
                if isinstance(hist.columns, pd.MultiIndex):
                    # Flatten column names for intraday data
                    hist.columns = [col[0] for col in hist.columns]
                
                for column in ("Open", "High", "Low", "Close"):
                    hist[column] = pd.to_numeric(hist[column], errors="coerce")
                if "Volume" in hist:
                    hist["Volume"] = pd.to_numeric(hist["Volume"], errors="coerce")

                valid_bars = hist.dropna(subset=["Open", "High", "Low", "Close"])
                candles_available = len(valid_bars)
                candle_limit = CANDLE_LIMITS.get(interval, DEFAULT_CANDLE_LIMIT)
                bars = valid_bars.tail(candle_limit).copy()
                
                # Extract data as floats
                closes = pd.to_numeric(bars['Close'], errors='coerce').dropna().values.tolist()
                opens = pd.to_numeric(bars['Open'], errors='coerce').dropna().values.tolist()
                highs = pd.to_numeric(bars['High'], errors='coerce').dropna().values.tolist()
                lows = pd.to_numeric(bars['Low'], errors='coerce').dropna().values.tolist()
                volumes = pd.to_numeric(bars.get('Volume'), errors='coerce').where(
                    pd.notnull(bars.get('Volume')), None
                ).values.tolist() if 'Volume' in bars else []
                
                if not closes:
                    data["intervals"][interval] = {"error": "No valid price data"}
                    continue
                
                # Current values
                current_price = float(closes[-1])
                open_price = float(opens[-1]) if opens else current_price
                high_price = float(highs[-1]) if highs else current_price
                low_price = float(lows[-1]) if lows else current_price
                
                # Calculate simple moving averages
                sma_9 = sum(closes[-9:]) / len(closes[-9:]) if len(closes) >= 9 else None
                sma_21 = sum(closes[-21:]) / len(closes[-21:]) if len(closes) >= 21 else None
                sma_50 = sum(closes[-50:]) / len(closes[-50:]) if len(closes) >= 50 else None
                
                # Calculate RSI
                rsi = calculate_rsi(closes, period=14)
                confluence_vector, confluence_score, volume_quality = _build_confluence_vector(
                    closes,
                    highs,
                    lows,
                    volumes,
                    sma_9,
                    sma_21,
                    sma_50,
                    rsi,
                )
                volatility_regime = _build_volatility_regime(
                    closes,
                    highs,
                    lows,
                    volumes,
                )
                
                candles = _ohlcv_to_records(bars)
                data["intervals"][interval] = {
                    "current_price": round(current_price, 2),
                    "open": round(open_price, 2),
                    "high": round(high_price, 2),
                    "low": round(low_price, 2),
                    "sma_9": round(sma_9, 2) if sma_9 else None,
                    "sma_21": round(sma_21, 2) if sma_21 else None,
                    "sma_50": round(sma_50, 2) if sma_50 else None,
                    "rsi": round(rsi, 2) if rsi else None,
                    "bars_count": len(closes),
                    "candles_available": candles_available,
                    "candle_limit": candle_limit,
                    "candles_fed_to_prompt": len(candles),
                    "candles": candles,
                    "confluence_vector": confluence_vector,
                    "confluence_score": confluence_score,
                    "volume_quality": volume_quality,
                    "atr_14": volatility_regime.get("atr_14"),
                    "volatility_regime": volatility_regime,
                }
            except Exception as e:
                data["intervals"][interval] = {"error": f"Processing error: {str(e)}"}
    data["confluence_tensor"] = _build_confluence_tensor(data["intervals"])
    data["volatility_regime_summary"] = _build_regime_summary(data["intervals"])
    data["volume_quality_summary"] = _build_volume_quality_summary(data["intervals"])
    data["entry_fill_guide"] = _build_entry_fill_guide(data["intervals"])
    return data

def save_intraday_data(data):
    """Save intraday data to JSON file."""
    with open(INTRADAY_JSON_PATH, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    return INTRADAY_JSON_PATH

def load_intraday_data():
    """Load latest intraday data from JSON."""
    if INTRADAY_JSON_PATH.exists():
        with open(INTRADAY_JSON_PATH, 'r') as f:
            return json.load(f)
    return None

def get_intraday_summary():
    """
    Get a text summary of intraday technicals for Claude prompt.
    Falls back to cached data if fetch fails.
    """
    data = fetch_intraday_bars(retries=3)
    
    # If all intervals failed, try loading cached data
    all_failed = all(
        "error" in data["intervals"].get(interval, {}) 
        for interval in TIMEFRAME_ORDER
    )
    if all_failed:
        cached = load_intraday_data()
        if cached:
            data = cached
            # Mark as fallback
            data["note"] = "Using cached data (fetch failed)"
    
    save_intraday_data(data)
    
    summary = "=== INTRADAY TECHNICALS (Gold Futures - 5m/15m/1h/4h) ===\n"
    
    # Add fallback note if present
    if "note" in data:
        summary += f"[NOTE] {data['note']}\n"
    
    for interval in TIMEFRAME_ORDER:
        if interval not in data['intervals']:
            continue
        
        bar_data = data['intervals'][interval]
        if 'error' in bar_data:
            attempt_info = f" (attempt {bar_data.get('attempt', '?')})" if 'attempt' in bar_data else ""
            summary += f"\n[{interval.upper()}] ERROR{attempt_info}: {bar_data['error']}\n"
            continue
        
        summary += f"\n[{interval.upper()}]\n"
        summary += f"  Price: ${bar_data['current_price']}\n"
        summary += f"  Open/High/Low: ${bar_data['open']} / ${bar_data['high']} / ${bar_data['low']}\n"
        summary += f"  SMA(9): ${bar_data['sma_9']} | SMA(21): ${bar_data['sma_21']} | SMA(50): ${bar_data['sma_50']}\n"
        summary += f"  RSI(14): {bar_data['rsi']}\n"
        summary += f"  Confluence score: {bar_data.get('confluence_score', 'n/a')} (-10 bearish to +10 bullish)\n"
        summary += (
            f"  Candles fed: {bar_data.get('candles_fed_to_prompt', bar_data.get('bars_count', 'n/a'))} "
            f"(available {bar_data.get('candles_available', 'n/a')}, cap {bar_data.get('candle_limit', 'n/a')})\n"
        )
        
        # Add trend analysis
        if bar_data['current_price'] and bar_data['sma_9'] and bar_data['sma_21']:
            if bar_data['current_price'] > bar_data['sma_9'] > bar_data['sma_21']:
                summary += f"  → BULLISH: Price > SMA9 > SMA21\n"
            elif bar_data['current_price'] < bar_data['sma_9'] < bar_data['sma_21']:
                summary += f"  → BEARISH: Price < SMA9 < SMA21\n"
            else:
                summary += f"  → MIXED: No clear alignment\n"
    tensor = data.get("confluence_tensor")
    if tensor:
        summary += "\n[CONFLUENCE TENSOR]\n"
        summary += f"  Features: {', '.join(tensor.get('feature_names', []))}\n"
        summary += f"  Timeframes: {', '.join(tensor.get('timeframe_order', []))}\n"
        summary += f"  Matrix: {tensor.get('matrix')}\n"
        summary += f"  Aggregate score: {tensor.get('aggregate_score')} ({tensor.get('alignment_label')})\n"
    
    return summary

if __name__ == "__main__":
    data = fetch_intraday_bars()
    save_intraday_data(data)
    print(get_intraday_summary())
