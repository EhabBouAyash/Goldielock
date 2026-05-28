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
    clean = [v for v in volumes if v is not None]
    if len(clean) < 5:
        return 0.0
    baseline = sum(clean[-20:]) / len(clean[-20:])
    if baseline <= 0:
        return 0.0
    return _clamp((clean[-1] / baseline) - 1)


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

    vector = {
        "price_vs_sma9": round(price_vs_sma9, 4),
        "ma_stack": round(ma_stack, 4),
        "rsi_centered": round(rsi_centered, 4),
        "range_position": round(range_pos, 4),
        "momentum_5bar": round(momentum_5bar, 4),
        "volume_pressure": round(_volume_pressure(volumes), 4),
    }
    directional_score = sum(
        vector[name] * CONFLUENCE_FEATURE_WEIGHTS[name]
        for name in CONFLUENCE_FEATURES
    )
    return vector, round(directional_score * 10, 2)


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
                confluence_vector, confluence_score = _build_confluence_vector(
                    closes,
                    highs,
                    lows,
                    volumes,
                    sma_9,
                    sma_21,
                    sma_50,
                    rsi,
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
                }
            except Exception as e:
                data["intervals"][interval] = {"error": f"Processing error: {str(e)}"}
    data["confluence_tensor"] = _build_confluence_tensor(data["intervals"])
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
