"""OHLC loaders for paper replay. Not a FundedNext quote feed."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

from deepseek_trial.config import PACKAGE_DIR, ROOT

CACHE = PACKAGE_DIR / "data"
GOLD_HOUR_PARQUET = ROOT / "brooks_h2_l2" / "research" / "gcf_1h_730d.parquet"


def _ensure_utc_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = pd.to_datetime(out.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    out.index = idx
    return out.sort_index()


def gold_hourly() -> tuple[pd.DataFrame, str]:
    """Prefer local GC=F 1h cache; otherwise Yahoo GC=F. Label the source."""
    if GOLD_HOUR_PARQUET.is_file():
        df = pd.read_parquet(GOLD_HOUR_PARQUET)
        df = _ensure_utc_index(df)[["open", "high", "low", "close"]]
        df = df[~df.index.duplicated(keep="last")]
        return df.dropna(), f"parquet:{GOLD_HOUR_PARQUET.name}"
    return yahoo_ohlc("GC=F", "1h", "730d")


def gold_daily_et() -> tuple[pd.DataFrame, str]:
    """Broker-style daily bars: 17:00 America/New_York session from hourly gold."""
    hours, source = gold_hourly()
    ny = hours.tz_convert("America/New_York")
    daily = ny.resample("1D", label="left", closed="left", offset="17h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    daily.index = daily.index.tz_convert("UTC")
    return daily, f"{source}|resample:17h_ET"


def btc_daily_utc() -> tuple[pd.DataFrame, str]:
    return yahoo_ohlc("BTC-USD", "1d", "max")


def yahoo_ohlc(symbol: str, interval: str, period: str) -> tuple[pd.DataFrame, str]:
    """Yahoo chart API via requests (yfinance is optional)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    range_map = {"max": "10y", "730d": "2y", "1h": "60d"}
    yf_range = range_map.get(period, period)
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?interval={interval}&range={yf_range}&includePrePost=false"
    )
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        ts = result["timestamp"]
        q = result["indicators"]["quote"][0]
        idx = pd.to_datetime(ts, unit="s", utc=True)
        df = pd.DataFrame(
            {"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]},
            index=idx,
        ).dropna()
        df = _ensure_utc_index(df)
        if df.empty:
            raise RuntimeError("empty yahoo chart")
        return df, f"yahoo_chart:{symbol}:{interval}:{yf_range}"
    except Exception:
        import yfinance as yf

        raw = yf.download(symbol, interval=interval, period=period, progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            raise RuntimeError(f"yfinance returned no rows for {symbol} {interval}")
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [str(c).lower() for c in raw.columns]
        df = raw[["open", "high", "low", "close"]].dropna()
        df = _ensure_utc_index(df)
        return df, f"yfinance:{symbol}:{interval}:{period}"
