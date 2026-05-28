"""
Reliable market data fetcher for XAUUSD and DXY.

Bypasses yfinance SSL issues by calling Yahoo Finance chart API directly with
verify=False, and provides multiple fallback data sources.

Data sources:
- DXY: Yahoo chart API (DX-Y.NYB) with SSL bypass, or synthetic from FX rates
- XAUUSD intraday bars: Yahoo chart API (GC=F as chart proxy) with SSL bypass
- XAUUSD spot price: gold-api.com (used as the decision/logging price)
"""

import math
import warnings
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
YAHOO_TIMEOUT = 20

DXY_SYMBOL = "DX-Y.NYB"
CHART_PROXY_SYMBOL = "GC=F"

# DXY basket weights for synthetic calculation
DXY_BASKET = {"EUR": 0.576, "JPY": 0.136, "GBP": 0.119, "CAD": 0.091, "SEK": 0.042, "CHF": 0.036}
DXY_CONSTANT = 50.14348112


def _yahoo_chart(symbol: str, interval: str = "1d", range_val: str = "5d") -> dict | None:
    """Fetch raw chart data from Yahoo Finance chart API with SSL bypass."""
    try:
        url = YAHOO_CHART_URL.format(symbol=symbol)
        params = {"range": range_val, "interval": interval, "includePrePost": "false"}
        r = requests.get(url, params=params, headers=YAHOO_HEADERS, verify=False, timeout=YAHOO_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        return result[0]
    except Exception:
        return None


def _chart_to_dataframe(chart: dict) -> pd.DataFrame | None:
    """Convert Yahoo chart API response to a pandas OHLCV DataFrame."""
    timestamps = chart.get("timestamp")
    quotes = chart.get("indicators", {}).get("quote", [{}])[0]
    if not timestamps or not quotes:
        return None

    df = pd.DataFrame({
        "Open": quotes.get("open"),
        "High": quotes.get("high"),
        "Low": quotes.get("low"),
        "Close": quotes.get("close"),
        "Volume": quotes.get("volume"),
    }, index=pd.to_datetime(timestamps, unit="s", utc=True))
    df.index.name = "Datetime"
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df if not df.empty else None


def fetch_dxy_reliable() -> dict[str, Any]:
    """
    Fetch DXY data using Yahoo chart API with SSL bypass.
    Falls back to synthetic DXY from live FX rates if Yahoo fails.
    """
    # Primary: Yahoo chart API direct call
    chart = _yahoo_chart(DXY_SYMBOL, interval="1d", range_val="10d")
    if chart:
        timestamps = chart.get("timestamp", [])
        quotes = chart.get("indicators", {}).get("quote", [{}])[0]
        closes = quotes.get("close", [])
        opens = quotes.get("open", [])
        highs = quotes.get("high", [])
        lows = quotes.get("low", [])

        valid_indices = [i for i, c in enumerate(closes) if c is not None]
        if valid_indices:
            last_idx = valid_indices[-1]
            prev_idx = valid_indices[-2] if len(valid_indices) >= 2 else last_idx
            current = closes[last_idx]
            prev_close = closes[prev_idx]
            change = current - prev_close if prev_close else 0
            change_pct = (change / prev_close * 100) if prev_close else 0

            return {
                "value": round(current, 3),
                "high": round(highs[last_idx], 3) if highs[last_idx] else round(current, 3),
                "low": round(lows[last_idx], 3) if lows[last_idx] else round(current, 3),
                "open": round(opens[last_idx], 3) if opens[last_idx] else round(current, 3),
                "change": round(change, 3),
                "change_pct": round(change_pct, 3),
                "source": "yahoo_direct",
            }

    # Fallback: Synthetic DXY from live FX rates
    return _fetch_synthetic_dxy()


def _fetch_synthetic_dxy() -> dict[str, Any]:
    """Compute DXY from live FX rates via open.er-api.com + frankfurter.app."""
    rates = None

    for url in ("https://open.er-api.com/v6/latest/USD", "https://api.frankfurter.app/latest?from=USD"):
        try:
            r = requests.get(url, timeout=12)
            if r.status_code == 200:
                data = r.json()
                rates = data.get("rates", {})
                if rates:
                    break
        except Exception:
            continue

    if not rates:
        return {"value": 0, "high": 0, "low": 0, "open": 0, "source": "failed"}

    needed = {"EUR", "JPY", "GBP", "CAD", "SEK", "CHF"}
    if not needed.issubset(set(rates.keys())):
        return {"value": 0, "high": 0, "low": 0, "open": 0, "source": "failed"}

    # DXY = 50.14348112 * (USDEUR)^0.576 * (USDJPY)^0.136 * (USDGBP)^0.119 * ...
    # Note: EUR and GBP are quoted as EUR/USD on ICE, so DXY uses their inverse
    eur = rates["EUR"]  # EUR per 1 USD
    jpy = rates["JPY"]
    gbp = rates["GBP"]  # GBP per 1 USD
    cad = rates["CAD"]
    sek = rates["SEK"]
    chf = rates["CHF"]

    # The actual DXY formula uses EUR/USD (i.e., USD per EUR = 1/eur) inverted
    dxy = DXY_CONSTANT * (1/eur)**0.576 * jpy**0.136 * (1/gbp)**0.119 * cad**0.091 * sek**0.042 * chf**0.036

    return {
        "value": round(dxy, 3),
        "high": round(dxy, 3),
        "low": round(dxy, 3),
        "open": round(dxy, 3),
        "change": 0,
        "change_pct": 0,
        "source": "synthetic_fx_rates",
        "components": {
            "EUR/USD": round(1/eur, 5),
            "USD/JPY": round(jpy, 3),
            "GBP/USD": round(1/gbp, 5),
            "USD/CAD": round(cad, 5),
            "USD/SEK": round(sek, 4),
            "USD/CHF": round(chf, 5),
        },
    }


def fetch_dxy_intraday(interval: str = "1h", range_val: str = "5d") -> pd.DataFrame | None:
    """Fetch DXY intraday bars for chart analysis."""
    chart = _yahoo_chart(DXY_SYMBOL, interval=interval, range_val=range_val)
    if chart:
        return _chart_to_dataframe(chart)
    return None


def fetch_xauusd_chart_bars(interval: str, range_val: str = "5d") -> pd.DataFrame | None:
    """
    Fetch XAUUSD-proxy intraday bars from GC=F via Yahoo chart API (SSL bypass).
    
    GC=F gold futures track spot XAUUSD movement almost identically for technical
    analysis purposes (correlation > 0.99 on intraday timeframes). The spot price
    from gold-api remains the decision/logging price.
    """
    chart = _yahoo_chart(CHART_PROXY_SYMBOL, interval=interval, range_val=range_val)
    if chart:
        return _chart_to_dataframe(chart)
    return None


def fetch_all_xauusd_intervals() -> dict[str, pd.DataFrame | None]:
    """Fetch all required timeframes for XAUUSD chart analysis."""
    intervals = {
        "5m": ("5m", "5d"),
        "15m": ("15m", "5d"),
        "1h": ("1h", "1mo"),
        "4h": ("4h", "1mo"),
    }
    results = {}
    for tf, (interval, range_val) in intervals.items():
        results[tf] = fetch_xauusd_chart_bars(interval, range_val)
    return results


GOLD_API_BASE = "https://api.gold-api.com/price"


def _gold_api_get(path: str, params: dict | None = None) -> dict | None:
    """Call gold-api with SSL verify disabled (works when system clock skew breaks certs)."""
    try:
        url = f"{GOLD_API_BASE}/{path}"
        r = requests.get(url, params=params, timeout=30, verify=False)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            return None
        return data
    except Exception:
        return None


def _gc_f_last_close() -> float | None:
    """Last GC=F close as XAUUSD spot proxy when gold-api is unreachable."""
    df = fetch_xauusd_chart_bars("1h", "5d")
    if df is not None and not df.empty:
        close = df["Close"].iloc[-1]
        if close is not None and not (isinstance(close, float) and math.isnan(close)):
            return round(float(close), 2)
    return None


def fetch_xauusd_spot_snapshot() -> dict[str, Any]:
    """
    Fetch XAUUSD spot price for execution/logging.

    Primary: gold-api.com. Fallback: GC=F last close (Yahoo chart, SSL bypass).
    """
    data = _gold_api_get("XAU")
    if data and data.get("price"):
        return {
            "price": round(float(data["price"]), 2),
            "bid": data.get("bid"),
            "ask": data.get("ask"),
            "change": data.get("ch"),
            "change_pct": data.get("chp"),
            "open": None,
            "high": None,
            "low": None,
            "source": "gold-api XAU",
            "updated_at": data.get("updatedAt"),
        }

    proxy_close = _gc_f_last_close()
    if proxy_close:
        return {
            "price": proxy_close,
            "bid": None,
            "ask": None,
            "change": None,
            "change_pct": None,
            "open": None,
            "high": None,
            "low": None,
            "source": "GC=F chart proxy (gold-api unavailable)",
            "fallback": True,
        }

    return {"price": None, "source": "unavailable"}


def fetch_metals_spot_prices() -> dict[str, dict[str, Any]]:
    """
    Fetch gold and silver spot snapshots. Same shape as generate_insights.fetch_gold_price().
    """
    from datetime import date

    out: dict[str, dict[str, Any]] = {}

    for sym, key in (("XAU", "gold"), ("XAG", "silver")):
        if sym == "XAU":
            out[key] = fetch_xauusd_spot_snapshot()
            # Optional OHLC history when gold-api works
            if not out[key].get("fallback"):
                today = date.today().isoformat()
                ohlc = _gold_api_get(f"{sym}/history", {"date": today})
                if ohlc:
                    out[key]["open"] = ohlc.get("open")
                    out[key]["high"] = ohlc.get("high")
                    out[key]["low"] = ohlc.get("low")
            continue

        data = _gold_api_get(sym)
        if data and data.get("price"):
            today = date.today().isoformat()
            ohlc = _gold_api_get(f"{sym}/history", {"date": today}) or {}
            out[key] = {
                "price": data.get("price"),
                "bid": data.get("bid"),
                "ask": data.get("ask"),
                "change": data.get("ch"),
                "change_pct": data.get("chp"),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
                "source": f"gold-api {sym}",
            }
        else:
            out[key] = {"price": None, "source": "unavailable"}

    return out
