"""M5 gold history and H4 warmup for GOLD-OCT26-v1.

Yahoo GC=F last-trade OHLC is a chart proxy, not bid/ask midpoint from the
intended execution feed. The source string says so.
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path

import pandas as pd
import requests

from deepseek_trial.bars import ensure_utc, resample_ohlc
from deepseek_trial.config import PACKAGE_DIR

CACHE = PACKAGE_DIR / "data"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _chart_url(symbol: str, interval: str, span: str) -> str:
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={span}&includePrePost=true"
    )


def _fetch_ohlc(symbol: str, interval: str, span: str) -> pd.DataFrame:
    resp = requests.get(_chart_url(symbol, interval, span), timeout=60, headers=HEADERS)
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    idx = pd.to_datetime(ts, unit="s", utc=True)
    df = pd.DataFrame(
        {"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]},
        index=idx,
    ).dropna()
    df = ensure_utc(df)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _cache_load(path: Path, max_age_hours: float) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    if time.time() - path.stat().st_mtime > max_age_hours * 3600:
        return None
    try:
        with path.open("rb") as handle:
            return pickle.load(handle)
    except (OSError, pickle.PickleError, EOFError):
        return None


def _cache_save(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        pickle.dump(df, handle, protocol=pickle.HIGHEST_PROTOCOL)


def gold_m5() -> tuple[pd.DataFrame, str]:
    """Yahoo GC=F 5-minute last-trade bars (not XAUUSD bid/ask midpoint)."""
    path = CACHE / "gc_f_m5_60d.pkl"
    cached = _cache_load(path, 6)
    if cached is not None and len(cached) > 100:
        return cached, "yahoo_chart:GC=F:5m:60d:last_trade_ohlc_not_midpoint (cache)"
    df = _fetch_ohlc("GC=F", "5m", "60d")
    _cache_save(path, df)
    return df, "yahoo_chart:GC=F:5m:60d:last_trade_ohlc_not_midpoint"


def gold_h4_history() -> tuple[pd.DataFrame, str]:
    """H4 from GC=F 1-hour bars so EMA warmup can reach 300 completed H4 candles.

    5-minute Yahoo history is only ~60 days (~300 H4 bars). The spec requires 300 H4
    of warm-up before trial decisions; without this longer H4 series a 5m replay
    would evaluate only the last day. Same symbol, last-trade OHLC, labelled mix.
    """
    path = CACHE / "gc_f_h4_from_1h.pkl"
    cached = _cache_load(path, 12)
    if cached is not None and len(cached) > 300:
        return cached, "yahoo_chart:GC=F:1h:730d->H4 (cache)"
    df = None
    used = ""
    for span in ("730d", "2y", "1y"):
        try:
            raw = _fetch_ohlc("GC=F", "1h", span)
            if len(raw) > 500:
                df = raw
                used = span
                break
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
            continue
    if df is None:
        return pd.DataFrame(columns=["open", "high", "low", "close"]), "yahoo_chart:GC=F:1h:MISSING"
    h4 = resample_ohlc(df, "4h", origin="epoch")
    _cache_save(path, h4)
    return h4, f"yahoo_chart:GC=F:1h:{used}->H4:last_trade_ohlc_not_midpoint"


def merge_h4(h4_hist: pd.DataFrame, h4_live: pd.DataFrame) -> pd.DataFrame:
    """Older 1h-derived H4, then 5m-derived H4 for the replay window (5m wins on overlap)."""
    if h4_hist is None or len(h4_hist) == 0:
        return h4_live
    if len(h4_live) == 0:
        return ensure_utc(h4_hist)
    live = ensure_utc(h4_live)
    hist = ensure_utc(h4_hist)
    older = hist[hist.index < live.index[0]]
    out = pd.concat([older, live])
    return out[~out.index.duplicated(keep="last")].sort_index()
