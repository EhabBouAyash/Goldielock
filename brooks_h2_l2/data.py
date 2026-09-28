"""XAUUSD 1-minute history -> parquet -> mid-price bars.

Two free sources:
- HistData.com (default): one request per year, bid-only. Timestamps are New
  York local time *with* daylight saving (verified against Dukascopy, despite
  the site saying EST). The ask side is modelled as bid + a price-proportional
  spread.
- Dukascopy: real bid/ask per minute, but one request per day and side, and the
  datafeed rate-limits heavily (HTTP 429), so years of history take hours.
"""
from __future__ import annotations

import argparse
import io
import lzma
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
HISTDATA_DIR = DATA_DIR / "histdata"
DUKASCOPY_DIR = DATA_DIR / "dukascopy"
UA = {"User-Agent": "Mozilla/5.0"}
# Dukascopy medians: $0.30 at ~$1,940 (Sep 2023), ~$0.50 at ~$3,630 (Sep 2025): about 1.4-1.5 bp.
DEFAULT_SPREAD_BPS = 1.6

# ------------------------------------------------------------------ HistData
HD_PAGE = "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair}/{period}"
HD_POST = "https://www.histdata.com/get.php"
HD_TZ = "America/New_York"


def _histdata_zip(pair: str, year: int, month: int | None) -> bytes | None:
    period = f"{year}" if month is None else f"{year}/{month}"
    page_url = HD_PAGE.format(pair=pair.lower(), period=period)
    with urllib.request.urlopen(urllib.request.Request(page_url, headers=UA), timeout=60) as resp:
        page = resp.read().decode("utf-8", "ignore")
    token = re.search(r'name="tk" id="tk" value="([0-9a-f]+)"', page)
    if not token:
        return None
    form = {"tk": token.group(1), "date": str(year),
            "datemonth": f"{year}" if month is None else f"{year}{month:02d}",
            "platform": "ASCII", "timeframe": "M1", "fxpair": pair.upper()}
    req = urllib.request.Request(HD_POST, data=urllib.parse.urlencode(form).encode(),
                                 headers={**UA, "Referer": page_url})
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = resp.read()
    return body if body[:2] == b"PK" else None


def _parse_histdata(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        frame = pd.read_csv(zf.open(name), sep=";", header=None,
                            names=["ts", "open", "high", "low", "close", "volume"])
    local = pd.DatetimeIndex(pd.to_datetime(frame["ts"], format="%Y%m%d %H%M%S"))
    # The repeated 01:00 hour on the autumn DST change is ambiguous; drop it (quiet Asian session).
    frame.index = local.tz_localize(HD_TZ, ambiguous="NaT", nonexistent="NaT").tz_convert("UTC")
    frame = frame[frame.index.notna()]
    return frame[["open", "high", "low", "close"]].astype(float).add_prefix("bid_")


def download_histdata(pair: str = "XAUUSD", start_year: int = 2021, out_dir: Path = HISTDATA_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    periods = [(y, None) for y in range(start_year, today.year)] + [(today.year, m) for m in range(1, today.month + 1)]
    paths = []
    for year, month in periods:
        stem = f"{pair.lower()}_m1_{year}" + ("" if month is None else f"-{month:02d}")
        out = out_dir / f"{stem}.parquet"
        current = month is not None and (year, month) == (today.year, today.month)
        if out.exists() and not current:
            paths.append(out)
            continue
        t0 = time.time()
        blob = _histdata_zip(pair, year, month)
        if blob is None:
            print(f"{stem}: not published yet", flush=True)
            continue
        frame = _parse_histdata(blob)
        frame.to_parquet(out)
        paths.append(out)
        print(f"{stem}: {len(frame)} minutes ({time.time() - t0:.0f}s)", flush=True)
    return paths


# ------------------------------------------------------------------ Dukascopy
DK_URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m:02d}/{d:02d}/{side}_candles_min_1.bi5"
DK_POINT = 1000.0  # XAUUSD prices are stored as integer thousandths
DK_RECORD = struct.Struct(">5if")  # seconds-from-midnight, open, close, low, high, volume
DK_PAUSE = 0.25
DK_RATE_LIMIT_WAIT = (30, 300)  # the datafeed limits request volume, not concurrency


def _fetch(url: str, retries: int = 6) -> bytes:
    failures = limited = 0
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as resp:
                body = resp.read()
            time.sleep(DK_PAUSE)
            return body
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return b""
            if exc.code == 429:
                limited += 1
                time.sleep(min(DK_RATE_LIMIT_WAIT[1], DK_RATE_LIMIT_WAIT[0] * limited))
                continue
            failures += 1
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            failures += 1
        if failures >= retries:
            raise RuntimeError(f"failed after {retries} attempts: {url}")
        time.sleep(2**failures)


def _dk_day(symbol: str, day: date, side: str, cache: Path) -> pd.DataFrame | None:
    path = cache / side / f"{day.isoformat()}.bi5"  # an empty file marks a day without data
    if path.exists():
        raw = path.read_bytes()
    else:
        # Dukascopy months are zero-based.
        raw = _fetch(DK_URL.format(sym=symbol, y=day.year, m=day.month - 1, d=day.day, side=side))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    if not raw:
        return None
    buf = lzma.decompress(raw)
    rows = np.array([DK_RECORD.unpack_from(buf, k) for k in range(0, len(buf), DK_RECORD.size)], dtype=float)
    if rows.size == 0:
        return None
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        {"open": rows[:, 1], "high": rows[:, 4], "low": rows[:, 3], "close": rows[:, 2]},
        index=pd.DatetimeIndex(start + pd.to_timedelta(rows[:, 0], unit="s")),
    ) / DK_POINT
    # Closed-market minutes are written as flat, zero-volume candles.
    return frame[rows[:, 5] > 0]


def download_dukascopy_month(symbol: str, year: int, month: int, out_dir: Path = DUKASCOPY_DIR,
                             workers: int = 4) -> Path | None:
    out = out_dir / f"{symbol.lower()}_1m_{year}-{month:02d}.parquet"
    if out.exists():
        return out
    first = date(year, month, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    today = datetime.now(timezone.utc).date()
    if last >= today:
        return None  # only complete months are stored
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    jobs = [(d, side) for d in days if d.weekday() != 5 for side in ("BID", "ASK")]
    cache = out_dir / "raw" / symbol.lower()
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda job: _dk_day(symbol, *job, cache), jobs))
    bids = [b for b in results[0::2] if b is not None]
    asks = [a for a in results[1::2] if a is not None]
    if not bids or not asks:
        return None
    frame = pd.concat(bids).add_prefix("bid_").join(pd.concat(asks).add_prefix("ask_"), how="inner").sort_index()
    out_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out)
    return out


def download_dukascopy(symbol: str, start: date, end: date) -> list[Path]:
    paths, cursor = [], date(start.year, start.month, 1)
    while cursor <= end:
        t0 = time.time()
        path = download_dukascopy_month(symbol, cursor.year, cursor.month)
        print(f"{cursor:%Y-%m}: {path.name if path else 'skipped'} ({time.time() - t0:.0f}s)", flush=True)
        if path:
            paths.append(path)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return paths


# ------------------------------------------------------------------ loading
def load_minutes(source: str = "histdata", spread_bps: float = DEFAULT_SPREAD_BPS,
                 symbol: str = "xauusd", start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """1-minute frame with bid_* and ask_* OHLC columns, UTC index.

    start/end are inclusive ISO dates. The 2021+ study passes start='2021-01-01'
    so adding earlier years does not change those results.
    """
    folder = HISTDATA_DIR if source == "histdata" else DUKASCOPY_DIR
    files = sorted(folder.glob(f"{symbol}_*.parquet"))
    if not files:
        raise FileNotFoundError(f"no {source} files in {folder}; run `python3 -m brooks_h2_l2 download`")
    frame = pd.concat(pd.read_parquet(f) for f in files).sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    if source == "histdata":
        spread = (frame["bid_close"] * spread_bps / 1e4).round(3)
        for col in ("open", "high", "low", "close"):
            frame[f"ask_{col}"] = frame[f"bid_{col}"] + spread
    bad = (frame["ask_close"] < frame["bid_close"]) | (frame["bid_high"] < frame["bid_low"])
    frame = frame[~bad]
    if start:
        frame = frame[frame.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        frame = frame[frame.index < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)]
    return frame


def mid_bars(minutes: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Mid-price OHLC bars; a bar is labelled by its open time and covers [open, open + rule)."""
    mid = pd.DataFrame({
        "open": (minutes["bid_open"] + minutes["ask_open"]) / 2,
        "high": (minutes["bid_high"] + minutes["ask_high"]) / 2,
        "low": (minutes["bid_low"] + minutes["ask_low"]) / 2,
        "close": (minutes["bid_close"] + minutes["ask_close"]) / 2,
    })
    bars = mid.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return bars.dropna()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download XAUUSD 1-minute history")
    parser.add_argument("--source", choices=["histdata", "dukascopy"], default="histdata")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat())
    args = parser.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if args.source == "histdata":
        download_histdata("XAUUSD", start.year)
    else:
        download_dukascopy("XAUUSD", start, end)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
