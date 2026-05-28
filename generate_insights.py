import html as html_module
import argparse
import io
import json
import math
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
from datetime import date
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from additional_news import fetch_all as fetch_additional_metals_news
from additional_news import format_for_claude as format_additional_news_for_prompt
from io import StringIO
import pandas as pd
import yfinance as yf
load_dotenv()

COT_DISAGG_FUTURES_ZIP = (
    "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
)
COT_GOLD_PREFIX = '"GOLD - COMMODITY EXCHANGE INC."'
COT_SILVER_PREFIX = '"SILVER - COMMODITY EXCHANGE INC."'
C_YEAR_TXT_PATH = Path(__file__).resolve().parent / "c_year.txt"
LIVE_DATA_TXT_PATH = Path(__file__).resolve().parent / "live_data.txt"
GOLD_MULTIFRAME_JSON_PATH = Path(__file__).resolve().parent / "gold_multiframe.json"
SIGNAL_LOG_PKL_PATH = Path(__file__).resolve().parent / "signal_log.pkl"
SIGNAL_LOG_CSV_PATH = Path(__file__).resolve().parent / "signal_log.csv"
MODEL_TRADE_LOG_TXT_PATH = Path(__file__).resolve().parent / "model_trade_log.txt"
MANUAL_TRADE_BACKLOG_TXT_PATH = Path(__file__).resolve().parent / "trade_backlog.txt"

# COMEX gold continuous contract — multi-timeframe bars via Yahoo (15m / 1h / 1d).
GOLD_FUTURES_YF = "GC=F"

# Threshold (percent) under which a NEUTRAL/HOLD call is considered correct.
NEUTRAL_TOLERANCE_PCT = 0.5

KITCO_COMMODITIES_URL = "https://www.kitco.com/news/category/commodities"
KITCO_GOLD_URL = "https://www.kitco.com/news/category/commodities/gold"
KITCO_ORIGIN = "https://www.kitco.com"
KITCO_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}
KITCO_MAX_WORDS_PER_ARTICLE = 500
KITCO_MAX_ARTICLES = 18
KITCO_REQUEST_TIMEOUT = 60

FRED_API_KEY = "f9907d39b0c9d8e1c050a811651ae74a"  # register at fred.stlouisfed.org
GNEWS_API_KEY = "b8d23345a7561fa41a308d1b6fc1a443"  # register at gnews.io
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Import token tracker
try:
    from token_tracker import log_token_usage, calculate_cost, estimate_prompt_tokens
except ImportError:
    def log_token_usage(*args, **kwargs): pass
    def calculate_cost(*args, **kwargs): return 0
    def estimate_prompt_tokens(*args, **kwargs): return 0
def fetch_gold_price():
    """Gold/silver spot prices via fetch_market_data (SSL-safe + GC=F fallback for XAU)."""
    from fetch_market_data import fetch_metals_spot_prices

    return fetch_metals_spot_prices()

def calculate_rsi(prices, period=14):
    delta = pd.Series(prices).diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss
    return round(100 - (100 / (1 + rs)).iloc[-1], 2)

# Keep imports side-effect free. Historical Yahoo pulls are done inside the
# analysis flow when needed, not when another module imports these helpers.
gold_hist = pd.Series(dtype=float)
silver_hist = pd.Series(dtype=float)
gold_hist_200 = pd.Series(dtype=float)
silver_hist_200 = pd.Series(dtype=float)
gold_hist_weekly = pd.Series(dtype=float)
silver_hist_weekly = pd.Series(dtype=float)
gold_hist_monthly = pd.Series(dtype=float)
silver_hist_monthly = pd.Series(dtype=float)
ma_200_gold = None
ma_50_gold = None
ma_200_silver = None
ma_50_silver = None
rsi_gold = None
rsi_silver = None
rsi_gold_weekly = None
rsi_silver_weekly = None
rsi_gold_monthly = None
rsi_silver_monthly = None

def fetch_dxy():
    from fetch_market_data import fetch_dxy_reliable
    return fetch_dxy_reliable()


def fetch_macro(series_id):
    res = requests.get(
        f"https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "limit": 1,
            "sort_order": "desc"
        }
    )
    return res.json()["observations"][0]["value"]

def fetch_fred_observations(series_id, limit=5):
    res = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "limit": limit,
            "sort_order": "desc",
        },
        timeout=30,
    )
    res.raise_for_status()
    observations = res.json().get("observations", [])
    return [obs["value"] for obs in observations if obs.get("value") not in (None, ".", "")]


def fetch_fed_balance_sheet_snapshot():
    """
    WALCL is the official Fed balance sheet total assets series from H.4.1 via FRED.
    Units are millions of USD. We convert to trillions / billions for prompt readability.
    """
    values = fetch_fred_observations("WALCL", limit=5)
    if not values:
        raise RuntimeError("No WALCL observations returned from FRED")

    latest_millions = float(values[0])
    latest_trillions = round(latest_millions / 1_000_000, 3)

    four_week_change_billions = None
    if len(values) >= 5:
        four_week_change_billions = round((latest_millions - float(values[4])) / 1_000, 2)

    return {
        "latest_trillions": latest_trillions,
        "four_week_change_billions": four_week_change_billions,
    }

def fetch_treasury_snapshot():
    """
    Pulls a snapshot of US Treasury moves that matter for gold:
      - Treasury General Account (TGA) balance via WTREGEN (weekly, $ millions)
      - 10Y and 2Y nominal Treasury yields (daily) + short-term changes
    TGA draining generally = liquidity injection into the system (bullish gold).
    TGA refilling generally = liquidity drain (headwind for gold).
    """
    snapshot = {}

    try:
        tga_values = fetch_fred_observations("WTREGEN", limit=5)
        if tga_values:
            latest_tga_millions = float(tga_values[0])
            latest_tga_billions = round(latest_tga_millions / 1_000, 2)
            prior_tga_billions = None
            weekly_change_billions = None
            if len(tga_values) >= 2:
                prior_tga_millions = float(tga_values[1])
                prior_tga_billions = round(prior_tga_millions / 1_000, 2)
                weekly_change_billions = round(
                    (latest_tga_millions - prior_tga_millions) / 1_000, 2
                )
            snapshot["tga_latest_billions"] = latest_tga_billions
            snapshot["tga_prior_billions"] = prior_tga_billions
            snapshot["tga_weekly_change_billions"] = weekly_change_billions
    except Exception as exc:
        snapshot["tga_error"] = str(exc)

    for series_id, key in (("DGS10", "y10"), ("DGS2", "y2")):
        try:
            vals = fetch_fred_observations(series_id, limit=6)
            if vals:
                latest = float(vals[0])
                snapshot[f"{key}_latest"] = round(latest, 3)
                if len(vals) >= 2:
                    snapshot[f"{key}_daily_change_bps"] = round(
                        (latest - float(vals[1])) * 100, 1
                    )
                if len(vals) >= 6:
                    snapshot[f"{key}_weekly_change_bps"] = round(
                        (latest - float(vals[5])) * 100, 1
                    )
        except Exception as exc:
            snapshot[f"{key}_error"] = str(exc)

    return snapshot


def fetch_yf_last_close(symbol):
    hist = yf.Ticker(symbol).history(period="5d")["Close"].dropna()
    if hist.empty:
        return None
    return round(float(hist.iloc[-1]), 3)

def fetch_news(query):
    res = requests.get(
        "https://gnews.io/api/v4/search",
        params={
            "q": query,
            "token": GNEWS_API_KEY,
            "lang": "en",
            "max": 5
        }
    )
    return [a["title"] for a in res.json().get("articles", [])]


def _kitco_html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", fragment)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_module.unescape(t)
    return " ".join(t.split())


def _kitco_truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " …"


def _kitco_next_data(html: str) -> dict | None:
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _kitco_node_items(container) -> list[dict]:
    if isinstance(container, dict):
        return list(container.get("items") or [])
    if isinstance(container, list):
        return list(container)
    return []


def _kitco_get(url: str, timeout: int = KITCO_REQUEST_TIMEOUT) -> requests.Response:
    last_exc = None
    for _ in range(3):
        try:
            res = requests.get(url, headers=KITCO_REQUEST_HEADERS, timeout=timeout)
            res.raise_for_status()
            return res
        except requests.RequestException as exc:
            last_exc = exc
    raise last_exc


def _kitco_extract_text_from_selectors(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    json_ld_nodes = soup.find_all("script", attrs={"type": "application/ld+json"})
    for node in json_ld_nodes:
        raw = node.string or node.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            article_body = item.get("articleBody")
            if isinstance(article_body, str) and article_body.strip():
                return " ".join(article_body.split())

    for selector in [
        "article",
        "main article",
        "[data-testid='article-body']",
        ".article-body",
        ".content-body",
        "main",
    ]:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text.split()) >= 80:
                return text

    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()

    return ""


def _kitco_extract_article_payload(html: str) -> tuple[str, str]:
    nd = _kitco_next_data(html)
    if not nd:
        return "", ""

    try:
        queries = nd["props"]["pageProps"]["dehydratedState"].get("queries") or []
    except (KeyError, TypeError):
        return "", ""

    for q in queries:
        data = (q.get("state") or {}).get("data")
        if not isinstance(data, dict):
            continue
        node = data.get("nodeByUrlAlias")
        if not isinstance(node, dict):
            continue

        title = node.get("title") or ""
        bwm = node.get("bodyWithEmbeddedMedia")
        if isinstance(bwm, dict):
            body_html = bwm.get("value") or ""
        elif isinstance(bwm, str):
            body_html = bwm
        else:
            body_html = ""
        return title, _kitco_html_to_text(body_html)

    return "", ""


def _kitco_listing_entries(url: str) -> list[dict]:
    html = _kitco_get(url).text
    nd = _kitco_next_data(html)
    if not nd:
        return []

    try:
        queries = nd["props"]["pageProps"]["dehydratedState"].get("queries") or []
    except (KeyError, TypeError):
        return []

    for q in queries:
        data = (q.get("state") or {}).get("data")
        if not isinstance(data, dict):
            continue
        node_list = data.get("nodeListByCategory")
        items = _kitco_node_items(node_list)
        if items:
            out = []
            for item in items:
                path = (item or {}).get("urlAlias") or ""
                if not isinstance(path, str) or not path.startswith("/"):
                    continue
                category = (item.get("category") or {}).get("name") or "Commodities"
                body_field = item.get("bodyWithEmbeddedMedia")
                if isinstance(body_field, dict):
                    body_html = body_field.get("value") or ""
                elif isinstance(body_field, str):
                    body_html = body_field
                else:
                    body_html = ""
                out.append(
                    {
                        "section": category,
                        "title": item.get("title") or path,
                        "path": path,
                        "teaser": item.get("teaserSnippet") or "",
                        "body_text": _kitco_html_to_text(body_html),
                    }
                )
            if out:
                return out

    return []


def _kitco_top_entries() -> list[dict]:
    entries = _kitco_listing_entries(KITCO_COMMODITIES_URL)
    if not entries:
        entries = _kitco_listing_entries(KITCO_GOLD_URL)

    seen: set[str] = set()
    ordered: list[dict] = []
    for entry in entries:
        path = entry["path"]
        if path in seen:
            continue
        seen.add(path)
        ordered.append(entry)
        if len(ordered) >= KITCO_MAX_ARTICLES:
            break
    return ordered


def _kitco_article_excerpt(entry: dict) -> tuple[str, str]:
    title = entry["title"]
    body_text = entry.get("body_text") or ""

    if len(body_text.split()) < 120:
        url = urljoin(KITCO_ORIGIN, entry["path"])
        html = _kitco_get(url, timeout=45).text
        fetched_title, fetched_body = _kitco_extract_article_payload(html)
        title = fetched_title or title
        body_text = fetched_body or _kitco_extract_text_from_selectors(html) or body_text

    if not body_text:
        body_text = entry.get("teaser") or ""

    return title, _kitco_truncate_words(body_text, KITCO_MAX_WORDS_PER_ARTICLE)


def fetch_kitco_news() -> str:
    """
    Primary source: server-rendered commodities page data.
    This avoids fragile DOM scraping and keeps the pipeline stable if the
    client-side layout changes. For article text, use listing-page bodies when
    present, then fall back to the article page payload/selectors.
    """
    entries = _kitco_top_entries()
    if not entries:
        return "(No Kitco commodities articles found.)"

    blocks: list[str] = [
        f"Source: {KITCO_COMMODITIES_URL} (server-rendered category data with article-page fallbacks).",
        "",
    ]

    for entry in entries:
        try:
            art_title, excerpt = _kitco_article_excerpt(entry)
        except Exception as exc:
            blocks.append(f"## {entry['section']}")
            blocks.append(f"URL: {urljoin(KITCO_ORIGIN, entry['path'])}")
            blocks.append(f"Headline: {entry['title']}")
            blocks.append(f"(Could not load article: {exc})")
            blocks.append("")
            continue

        blocks.append(f"## {entry['section']}")
        blocks.append(f"URL: {urljoin(KITCO_ORIGIN, entry['path'])}")
        blocks.append(f"Title: {art_title}")
        blocks.append(excerpt if excerpt else "(Empty article body.)")
        blocks.append("")

    return "\n".join(blocks).rstrip()


def fetch_cot_disaggregated_latest(year=None):
    """
    Disaggregated futures-only COT (annual text zip from Historical Compressed).
    Overwrites c_year.txt with the full extracted annual .txt from the zip.
    Rows are newest-first; take the first GOLD and first SILVER contract line each
    (excludes MICRO GOLD / MICRO SILVER, which do not use these prefixes).
    """
    tz = ZoneInfo("America/New_York")
    if year is None:
        year = datetime.now(tz).year

    def load_for(y):
        url = COT_DISAGG_FUTURES_ZIP.format(year=y)
        res = requests.get(url, timeout=120)
        res.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(res.content))
        names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not names:
            raise RuntimeError("COT zip contains no .txt file")
        return zf.read(names[0]).decode("utf-8", errors="replace")

    try:
        text = load_for(year)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404 and year > 2009:
            year -= 1
            text = load_for(year)
        else:
            raise

    C_YEAR_TXT_PATH.write_text(text, encoding="utf-8")

    gold_line = None
    silver_line = None
    for line in text.splitlines():
        if gold_line is None and line.startswith(COT_GOLD_PREFIX):
            gold_line = line
        if silver_line is None and line.startswith(COT_SILVER_PREFIX):
            silver_line = line
        if gold_line is not None and silver_line is not None:
            break

    parts = [
        f"Source: CFTC disaggregated futures text ({year}), latest rows in file order.",
        "",
    ]
    if gold_line:
        parts.append(gold_line)
    else:
        parts.append("(No GOLD - COMMODITY EXCHANGE INC. row found.)")
    parts.append("")
    if silver_line:
        parts.append(silver_line)
    else:
        parts.append("(No SILVER - COMMODITY EXCHANGE INC. row found.)")
    return "\n".join(parts)

def fetch_trend(ticker_symbol, days=20):
    # Request 60 days to ensure we always have enough trading days
    hist = yf.Ticker(ticker_symbol).history(period="60d")["Close"]
    hist = hist.dropna()  # remove any NaN rows
    
    if len(hist) < days + 1:
        return None  # not enough data, don't crash
    
    current    = hist.iloc[-1]
    past       = hist.iloc[-(days + 1)]
    change_pct = ((current - past) / past) * 100
    return round(change_pct, 2)


def _ohlcv_to_records(df: pd.DataFrame) -> list[dict]:
    """Serialize a yfinance OHLCV frame to JSON-friendly rows (index → ts)."""
    if df is None or df.empty:
        return []
    records: list[dict] = []
    for idx, row in df.iterrows():
        ts = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
        vol = row.get("Volume")
        if vol is not None and not pd.isna(vol):
            try:
                vol = int(vol)
            except (TypeError, ValueError):
                vol = None
        else:
            vol = None
        records.append(
            {
                "ts": ts,
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": vol,
            }
        )
    return records


def _timeframe_snapshot(
    df: pd.DataFrame, label: str, rsi_period: int = 14
) -> dict:
    """Compact stats for forecasting / LLM prompts; no raw rows."""
    if df is None or df.empty:
        return {
            "interval": label,
            "error": "no data",
            "bar_count": 0,
        }
    first, last = df.iloc[0], df.iloc[-1]
    o0, cN = float(first["Open"]), float(last["Close"])
    win_ret = round((cN - o0) / o0 * 100, 3) if o0 else None
    high = float(df["High"].max())
    low = float(df["Low"].min())
    closes = df["Close"].astype(float)
    rsi = None
    if len(closes) >= rsi_period + 1:
        try:
            rsi = calculate_rsi(closes, period=rsi_period)
        except Exception:
            rsi = None
    start = df.index[0]
    end = df.index[-1]
    snap: dict = {
        "interval": label,
        "bar_count": int(len(df)),
        "first_ts": start.isoformat() if hasattr(start, "isoformat") else str(start),
        "last_ts": end.isoformat() if hasattr(end, "isoformat") else str(end),
        "first_open": round(o0, 2),
        "last_close": round(cN, 2),
        "window_high": round(high, 2),
        "window_low": round(low, 2),
        "window_return_pct": win_ret,
    }
    if rsi is not None:
        snap["rsi_14"] = rsi
    return snap


def _ret_sign(pct: object, eps: float = 0.02) -> int:
    """+1 / 0 / -1 for coarse direction; eps treats tiny moves as neutral."""
    if pct is None or (isinstance(pct, float) and math.isnan(pct)):
        return 0
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return 0
    if v > eps:
        return 1
    if v < -eps:
        return -1
    return 0


def _pct_in_range(snap: dict) -> float | None:
    lo, hi, c = (
        snap.get("window_low"),
        snap.get("window_high"),
        snap.get("last_close"),
    )
    if lo is None or hi is None or c is None or hi <= lo:
        return None
    return round((float(c) - float(lo)) / (float(hi) - float(lo)) * 100, 1)


def _dir_word(s: int) -> str:
    return "bullish" if s > 0 else ("bearish" if s < 0 else "flat/neutral")


def build_gold_confluence(mf: dict) -> dict:
    """
    Synthesize cross-timeframe alignment for LLM prompts and JSON export.

    Rationale: flat bar lists and parallel stats make models 'average' instead
    of reason; explicit 15m → 1h(7d) → 1d labels, range position, and a
    conflict label reduce misread and improve signal/structure discussion.
    """
    timeframes = mf.get("timeframes") or {}
    s15 = (timeframes.get("15m") or {}).get("snapshot") or {}
    s1h = (timeframes.get("1h") or {}).get("snapshot") or {}
    s1d = (timeframes.get("1d") or {}).get("snapshot") or {}

    if s15.get("error") or not s15.get("bar_count"):
        return {"error": "insufficient 15m data", "narrative": ""}

    t15 = _ret_sign(s15.get("window_return_pct"))
    t1h_full = _ret_sign(s1h.get("window_return_pct"))
    wk = (s1h.get("one_week_context") or {})
    t1h_week = _ret_sign(wk.get("week_return_pct"))
    t1d = _ret_sign(s1d.get("window_return_pct"))

    # Primary stack: tactical 15m, swing = 1h last-7d, regime = 1d
    primary = (t15, t1h_week if t1h_week != 0 else t1h_full, t1d)
    if all(x == 1 for x in primary):
        label = "ALIGNED_BULLISH"
    elif all(x == -1 for x in primary):
        label = "ALIGNED_BEARISH"
    elif t15 > 0 and t1d < 0:
        label = "RALLY_IN_BEARISH_REGIME"
    elif t15 < 0 and t1d > 0:
        label = "PULLBACK_IN_BULLISH_REGIME"
    elif 0 in primary and (1 in primary or -1 in primary):
        label = "MIXED_WITH_NEUTRAL_LEGS"
    else:
        label = "MIXED_OR_CHOP"

    p15 = _pct_in_range(s15)
    p1h = _pct_in_range(s1h)
    p1d = _pct_in_range(s1d)

    def _rng(pos: float | None) -> str:
        if pos is None:
            return "n/a"
        if pos >= 75:
            return f"upper band of range ({pos}% of window; watch mean reversion)"
        if pos <= 25:
            return f"lower band of range ({pos}% of window; watch bounce risk)"
        return f"mid-range ({pos}% of window)"

    lines = [
        f"1) Tactical 15m: {_dir_word(t15)} (window {s15.get('window_return_pct')}%). "
        f"Last {s15.get('last_close')}, RSI14 {s15.get('rsi_14', 'n/a')}, {_rng(p15)}.",
        f"2) Swing 1h (last 7d): {_dir_word(t1h_week)} (7d ret {wk.get('week_return_pct', 'n/a')}%). "
        f"Full 1h window: {_dir_word(t1h_full)} ({s1h.get('window_return_pct')}% over {s1h.get('lookback_days_requested')}d), "
        f"last {s1h.get('last_close')}, {_rng(p1h)}.",
        f"3) Regime 1d: {_dir_word(t1d)} (window {s1d.get('window_return_pct')}% over lookback). "
        f"Last {s1d.get('last_close')}, RSI14 {s1d.get('rsi_14', 'n/a')}, {_rng(p1d)}.",
        f"4) Stack read: {label} — apply 15m for timing, 1h(7d) for swing filter, 1d for direction of hold vs counter-trend trade.",
    ]
    return {
        "tactical_15m_sign": t15,
        "swing_1h_7d_sign": t1h_week,
        "swing_1h_full_window_sign": t1h_full,
        "regime_1d_sign": t1d,
        "alignment_label": label,
        "range_position_pct": {"15m": p15, "1h": p1h, "1d": p1d},
        "narrative_lines": lines,
        "narrative": "\n".join(lines),
    }


def _slice_last_calendar_days(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df is None or df.empty or days < 1:
        return df
    end = df.index[-1]
    start = end - pd.Timedelta(days=days)
    return df[df.index >= start]


def fetch_gold_futures_multiframe(
    ticker: str = GOLD_FUTURES_YF,
    *,
    include_raw_bars: bool = True,
    m15_lookback_days: int = 10,
    h1_lookback_days: int = 14,
    d1_lookback_days: int = 120,
) -> dict:
    """
    Pull 15m, 1h, and 1d bars for COMEX gold (Yahoo symbol GC=F).

    - 1h: at least a full week of hourly context (default 14d window for margin).
    - 15m: recent microstructure for eventual 15m-slot trading (Yahoo allows up to ~60d).
    - 1d: higher-timeframe regime.

    For model design: prefer decisions 15m → 1h → 1d (tactical → swing → regime).
    """
    t = yf.Ticker(ticker)
    out: dict = {
        "symbol": ticker,
        "source": "yfinance",
        "fetched_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "decision_order": ["15m", "1h", "1d"],
        "decision_note": (
            "Tactical read on 15m, confirm on 1h, align with 1d regime before trading 15m slots."
        ),
        "timeframes": {},
    }

    d15 = t.history(period=f"{m15_lookback_days}d", interval="15m")
    h1 = t.history(period=f"{h1_lookback_days}d", interval="1h")
    d1 = t.history(period=f"{d1_lookback_days}d", interval="1d")

    h1_week = _slice_last_calendar_days(h1, 7)
    week_ext: dict = {
        "bar_count_7d": int(len(h1_week)),
        "note": "7 calendar days of 1h bars ending at last bar (CME session hours; count < 7*24 is normal).",
    }
    if not h1_week.empty:
        week_ext["week_high"] = round(float(h1_week["High"].max()), 2)
        week_ext["week_low"] = round(float(h1_week["Low"].min()), 2)
        w0, wN = float(h1_week["Open"].iloc[0]), float(h1_week["Close"].iloc[-1])
        if w0:
            week_ext["week_return_pct"] = round((wN - w0) / w0 * 100, 3)

    for key, frame, days in (
        ("15m", d15, m15_lookback_days),
        ("1h", h1, h1_lookback_days),
        ("1d", d1, d1_lookback_days),
    ):
        snap = _timeframe_snapshot(frame, key)
        snap["lookback_days_requested"] = days
        if key == "1h":
            snap["one_week_context"] = week_ext
        block: dict = {"snapshot": snap}
        if include_raw_bars:
            block["bars"] = _ohlcv_to_records(frame)
        out["timeframes"][key] = block
    out["confluence"] = build_gold_confluence(out)
    return out


def format_gold_multiframe_prompt(mf: dict) -> str:
    """Stats + explicit confluence narrative for live_data.txt (no raw bar dump)."""
    if "error" in mf:
        return f"(Multi-timeframe gold: {mf.get('error', 'unavailable')})"
    lines = [
        "MULTI-TIMEFRAME GOLD (COMEX GC=F via Yahoo; use 15m → 1h → 1d for confluence):",
    ]
    for tf in ("15m", "1h", "1d"):
        b = mf.get("timeframes", {}).get(tf) or {}
        s = b.get("snapshot") or {}
        if s.get("error") or not s.get("bar_count"):
            lines.append(f"- {tf}: (no data)")
            continue
        extra = ""
        if tf == "1h" and s.get("one_week_context"):
            w = s["one_week_context"]
            extra = (
                f" | 7d: {w.get('bar_count_7d', 0)} bars, "
                f"hi/lo {w.get('week_high')}/{w.get('week_low')}, "
                f"ret {w.get('week_return_pct', 'n/a')}%"
            )
        rsi = s.get("rsi_14")
        rsi_s = f", RSI14 {rsi}" if rsi is not None else ""
        lines.append(
            f"- {tf} ({s.get('bar_count')} bars, {s.get('first_ts', '')} → {s.get('last_ts', '')}): "
            f"last {s.get('last_close')}, range {s.get('window_low')}-{s.get('window_high')}, "
            f"window ret {s.get('window_return_pct')}%{rsi_s}{extra}"
        )
    lines.append(f"(Full OHLCV: {GOLD_MULTIFRAME_JSON_PATH.name})")
    conf = mf.get("confluence") or build_gold_confluence(mf)
    if conf.get("narrative") and not conf.get("error"):
        lines.append("")
        lines.append("CONFLUENCE (pre-interpreted for you; still verify against the numbers above):")
        for line in conf.get("narrative_lines") or conf["narrative"].split("\n"):
            lines.append(f"  {line}")
        lines.append(
            f"  → Label: {conf.get('alignment_label', 'n/a')} | "
            f"range % in window (15m/1h/1d): {conf.get('range_position_pct', {})}"
        )
    elif conf.get("error"):
        lines.append(f"(Confluence summary unavailable: {conf['error']})")
    return "\n".join(lines)


dxy_change_20d = None
gold_change_20d = None
divergence_str = "Insufficient data to compute"
print("DXY trend (20d): ", dxy_change_20d, "\n\nGold trend (20d): ", gold_change_20d, "\n\nDivergence: ", divergence_str)

def fetch_gld_holdings():
    """
    Fetch SPDR Gold Shares (GLD) holdings in tonnes from yfinance info.
    This is a proxy for institutional gold ETF flows.
    """
    try:
        gld = yf.Ticker("GLD")
        info = gld.info
        # GLD reports Net Assets, we can also get historical holdings
        hist = gld.history(period="5d")
        if not hist.empty:
            latest_close = round(float(hist["Close"].iloc[-1]), 2)
            # Calculate 5-day change
            if len(hist) >= 5:
                five_day_change = round(float(hist["Close"].iloc[-1] - hist["Close"].iloc[0]), 2)
                five_day_change_pct = round((five_day_change / hist["Close"].iloc[0]) * 100, 2)
            else:
                five_day_change = None
                five_day_change_pct = None
            return {
                "price": latest_close,
                "five_day_change": five_day_change,
                "five_day_change_pct": five_day_change_pct,
            }
    except Exception as exc:
        return {"error": str(exc)}
    return {"error": "No data available"}


def analyze():
    # 1. Fetch all data
    metals     = fetch_gold_price()
    gold_price = metals["gold"]["price"]
    silver_price = metals["silver"]["price"]
    gsr = round(gold_price / silver_price, 2) if silver_price else None
    fed_balance_sheet = fetch_fed_balance_sheet_snapshot()
    real_yield = fetch_macro("DFII10")
    breakeven_inflation = fetch_macro("T10YIE")
    dxy_hist = yf.Ticker("DX-Y.NYB").history(period="10d")["Close"]
    dxy = dxy_hist.iloc[-1] if not dxy_hist.empty else 0
    y10y2y_spread = fetch_macro("T10Y2Y")
    cpi        = fetch_macro("CPIAUCSL")
    oil_price  = fetch_yf_last_close("CL=F")
    vix_value  = fetch_yf_last_close("^VIX")
    gvz_value  = fetch_yf_last_close("^GVZ")
    btc_price  = fetch_yf_last_close("BTC-USD")
    sp500_price = fetch_yf_last_close("^GSPC")
    gld_data   = fetch_gld_holdings()

    try:
        treasury = fetch_treasury_snapshot()
    except Exception as exc:
        treasury = {"error": str(exc)}

    gold_news  = fetch_news("gold OR silver OR Federal Reserve")
    geo_news   = fetch_news("war OR sanctions OR geopolitical crisis")
    us_global_news = fetch_news(
        '"United States" AND (foreign policy OR tariffs OR sanctions OR China OR Russia OR NATO)'
    )
    treasury_news = fetch_news(
        '"US Treasury" OR "Treasury auction" OR "Treasury yields" OR "debt ceiling" OR "Treasury Secretary"'
    )

    try:
        cot_file = fetch_cot_disaggregated_latest()
    except Exception as exc:
        cot_file = f"(COT FILE unavailable: {exc})"

    try:
        kitco_news = fetch_kitco_news()
    except Exception as exc:
        kitco_news = f"(KITCO NEWS unavailable: {exc})"

    try:
        _extra_articles = fetch_additional_metals_news()
        additional_metals_news = format_additional_news_for_prompt(
            _extra_articles, max_articles=20
        )
    except Exception as exc:
        additional_metals_news = (
            f"(ADDITIONAL METALS NEWS digest unavailable: {exc})"
        )

    try:
        gold_mf = fetch_gold_futures_multiframe()
        GOLD_MULTIFRAME_JSON_PATH.write_text(
            json.dumps(gold_mf, indent=2), encoding="utf-8"
        )
        gold_mf_prompt = format_gold_multiframe_prompt(gold_mf)
    except Exception as exc:
        gold_mf_prompt = f"(Multi-timeframe gold unavailable: {exc})"

    def _fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "n/a"

    treasury_block = (
        f"- Treasury General Account (TGA, WTREGEN): ${_fmt(treasury.get('tga_latest_billions'))}B "
        f"(prior week ${_fmt(treasury.get('tga_prior_billions'))}B, "
        f"week-over-week change {_fmt(treasury.get('tga_weekly_change_billions'))}B)\n"
        f"- 10Y Treasury yield (DGS10): {_fmt(treasury.get('y10_latest'), '%')} "
        f"(1d {_fmt(treasury.get('y10_daily_change_bps'), 'bps')}, "
        f"5d {_fmt(treasury.get('y10_weekly_change_bps'), 'bps')})\n"
        f"- 2Y Treasury yield (DGS2): {_fmt(treasury.get('y2_latest'), '%')} "
        f"(1d {_fmt(treasury.get('y2_daily_change_bps'), 'bps')}, "
        f"5d {_fmt(treasury.get('y2_weekly_change_bps'), 'bps')})"
    )

    gld_info = (
        f"GLD ETF: ${_fmt(gld_data.get('price'))} "
        f"(5d: {_fmt(gld_data.get('five_day_change_pct'), '%')})"
        if "error" not in gld_data
        else f"GLD ETF: {gld_data.get('error', 'unavailable')}"
    )

    _ensure_manual_trade_backlog_file()
    model_trade_log_context = _read_recent_text(
        MODEL_TRADE_LOG_TXT_PATH, max_chars=9000
    )
    manual_trade_backlog_context = _read_recent_text(
        MANUAL_TRADE_BACKLOG_TXT_PATH, max_chars=9000
    )

    # 2. Build live data payload
    live_data = f"""
LIVE MARKET DATA:
- Gold spot: ${gold_price}
- Silver spot: ${silver_price}
- Gold/Silver Ratio (GSR): {gsr}
- 10Y real yield (TIPS): {real_yield}%
- 10Y breakeven inflation: {breakeven_inflation}%
- DXY: {dxy}
- DXY/Gold 20d divergence: {divergence_str}
- RSI Gold (14d/weekly/monthly): {rsi_gold} / {_fmt(rsi_gold_weekly)} / {_fmt(rsi_gold_monthly)}
- RSI Silver (14d/weekly/monthly): {rsi_silver} / {_fmt(rsi_silver_weekly)} / {_fmt(rsi_silver_monthly)}
- Gold MA 200/50: {ma_200_gold} / {ma_50_gold}
- Silver MA 200/50: {ma_200_silver} / {ma_50_silver}

{gold_mf_prompt}

- Oil (WTI front-month, CL=F): {oil_price}
- VIX (^VIX): {vix_value}
- GVZ (Gold VIX): {_fmt(gvz_value)}
- Bitcoin (BTC-USD): ${_fmt(btc_price)}
- S&P 500 (^GSPC): {_fmt(sp500_price)}
- {gld_info}
- 10Y-2Y yield spread (T10Y2Y): {y10y2y_spread}
- Fed balance sheet total assets (WALCL / H.4.1): ${fed_balance_sheet['latest_trillions']}T
- Fed balance sheet 4-week change: {fed_balance_sheet['four_week_change_billions']}B
- CPI latest: {cpi}

US TREASURY SNAPSHOT:
{treasury_block}

RECENT GOLD/MACRO HEADLINES:
{chr(10).join(f'- {h}' for h in gold_news)}

RECENT GEOPOLITICAL HEADLINES:
{chr(10).join(f'- {h}' for h in geo_news)}

RECENT AMERICA-IN-THE-WORLD HEADLINES (US foreign policy, tariffs, sanctions, major power relations):
{chr(10).join(f'- {h}' for h in us_global_news) if us_global_news else '- (No relevant headlines returned.)'}

RECENT US TREASURY HEADLINES (auctions, issuance, debt ceiling, Treasury Secretary moves):
{chr(10).join(f'- {h}' for h in treasury_news) if treasury_news else '- (No relevant headlines returned.)'}

COT FILE:
{cot_file}

KITCO NEWS:
{kitco_news}

ADDITIONAL METALS NEWS (RSS + site scrapers: FXStreet, Kitco feed, World Gold Council, Trading Economics, Investing.com, LiteFinance, CoinCodex, Forex.com, TradingView; may overlap KITCO NEWS or GNews):
{additional_metals_news}
"""
    LIVE_DATA_TXT_PATH.write_text(live_data, encoding="utf-8")

    user_message = f"""
LIVE MARKET DATA:
{live_data}

---

MODEL TRADE BACKLOG (AUTOGENERATED SIGNAL HISTORY):
{model_trade_log_context}

MANUAL TRADE OUTCOME BACKLOG (USER-EDITED GROUND TRUTH):
{manual_trade_backlog_context}

---

Analyze the above and respond in exactly this structure:

TRADING ACCOUNT ASSUMPTION:
- Assume this is for an active trading account with less than $1,000 in capital.
- The objective is not capital preservation by default; the objective is the best calculated risk when the setup has edge.
- Do the position-sizing work explicitly. Do not hide behind generic "use low leverage" language.
- Choose the highest leverage that is still mathematically defensible after accounting for entry, stop distance, volatility, invalidation, and liquidation buffer.
- If leverage should be avoided, explain the exact condition that blocks it. Otherwise, recommend a specific leverage number or tight leverage range.
- This is a small account, so the model should favor asymmetric setups where the defined downside is acceptable relative to the potential upside.

SUB-$1K RISK ENGINE (MANDATORY):
- Use this fixed account assumption unless the user provides another amount: account_size = $800.
- Define account risk buckets:
  - AGGRESSIVE: max risk at stop = 18% of account
  - BALANCED: max risk at stop = 12% of account
  - NO-LEVERAGE: max risk at stop = 6% of account
- Compute from the proposed entry and stop:
  - stop_pct = abs(entry_mid - stop) / entry_mid
  - max_notional = (account_size * risk_cap_pct) / stop_pct
  - implied_leverage = max_notional / account_size
- Round leverage down conservatively and never exceed 10x.
- If implied leverage is < 1.0x, mark the setup as NO-TRADE for sub-$1k unless you tighten the stop with a defensible technical reason.
- Require minimum reward/risk:
  - Target 1 must be >= 1.8R
  - Target 2 must be >= 3.0R
- If either target fails the threshold, label signal as NEUTRAL and set Risk mode to NO-LEVERAGE with clear reason.
- Output numbers must be internally consistent (entry/stop/targets <-> R:R <-> leverage <-> account-loss-at-stop). Do not output contradictory math.

## MARKET OBSERVATIONS
Provide a thorough narrative analysis covering:
- Macro regime (real yields, DXY, Fed posture)
- COT positioning interpretation (is managed money crowded? are producers hedging aggressively?)
- Geopolitical headline relevance (ignore noise, flag only structurally significant events)
- Kitco digest articles (KITCO NEWS): treat as precious-metals–oriented editorial/wire context
- ADDITIONAL METALS NEWS: supplementary headlines from multiple sources; deduplicated but may still overlap other headline blocks
- Intermarket context (gold/silver ratio, dollar trend, risk-on vs risk-off)
- Technical structure (where price sits relative to key levels)

## SIGNAL SUMMARY
For each metal, provide a primary directional signal based on macro + technicals.
Also provide intraday entry candidates if macro supports the direction.

Signal priority:
  1. Macro regime (real yields, DXY, Fed, CB demand) = primary
  2. Weekly/monthly technicals (RSI, COT, key levels) = confirmation
  3. Intraday technicals (5m/15m/1h bounces) = entry timing only

Signal threshold:
  - Require 2 or more macro/weekly indicators for LONG or SHORT
  - Intraday setups are permitted within a confirmed macro direction
  - If macro is unclear, say NEUTRAL regardless of intraday noise

Format each signal exactly as:

SIGNAL: [LONG/SHORT/NEUTRAL] XAU

MACRO VIEW:
Entry zone: $X - $X (primary swing entry based on macro)
Stop loss: $X (invalidation level and why)
Target 1: $X (first resistance/support, 1.5-2.0R)
Target 2: $X (extended move if momentum continues, 2.5-3.5R)
Confidence: [LOW/MEDIUM/HIGH]
Confluence: [list the specific macro + weekly indicators supporting this signal]
Risk/Reward: [ratio for swing trade]
Recommended leverage: [3x-5x; explain the math]
Risk mode: [AGGRESSIVE / BALANCED / NO-LEVERAGE] and why

INTRADAY ENTRY CANDIDATES (only if macro above is LONG or SHORT):
[If macro is LONG/SHORT, list 2-3 specific intraday setups using the triggers above]
Example format:
  - SCALP: Enter $4720 on RSI bounce above 30, stop $4718, target $4725 (0.7R, 2x-4x lev)
  - SWING: Enter $4730 on 15m consolidation break, stop $4710, target $4750 then $4800 (1.3R + 2.8R, 4x lev)
[If macro is NEUTRAL, explicitly state: "No intraday entries until macro direction confirmed."]

Technical checklist:
  Math check: [stop %, implied leverage, chosen leverage, coherence]
  Liquidation buffer: [explain how far liquidation should sit from stop]
  Sub-$1k sizing plan: [capital assumption, max position notional, estimated loss at stop]

SIGNAL: [LONG/SHORT/NEUTRAL] XAG
[same format]

## RISK FLAGS
List any conditions that would immediately invalidate the above signals.
Example: "If DXY breaks above X, long gold thesis collapses."

## MACHINE-READABLE FOOTER
On the very last line of your response, emit a single machine-readable tag for
the gold signal in exactly this form (no brackets, no extra words, uppercase):

GOLD_SIGNAL=LONG
GOLD_SIGNAL=SHORT
GOLD_SIGNAL=NEUTRAL

Pick exactly one. This line must match the XAU signal you gave in the SIGNAL
SUMMARY section above and must be the last line of your output.
"""

    # 3. Call Claude
    system_prompt = """You are a professional precious metals analyst with deep expertise in gold (XAU) 
and silver (XAG) markets. You analyze markets using a layered framework: macro 
regime, intermarket correlations, technical structure, positioning, and sentiment. 
You never invent numbers — if live data is not provided in the user message, you 
say so explicitly and reason qualitatively instead. You think like a hedge fund 
analyst, not a retail newsletter writer.

MACRO REGIME FRAMEWORK

Real yields (10Y TIPS) — the structural driver of gold, more than anything else.
  - Real yields falling = gold bull environment, regardless of nominal rates
  - Real yields rising = gold headwind, regardless of CPI prints
  - Monitor: 10Y nominal yield minus 10Y breakeven inflation rate
  - Key threshold: when real yields cross below 0%, gold enters its most 
    explosive regime historically

Dollar index (DXY) — the second structural driver.
  - DXY and gold are inversely correlated ~80% of the time
  - The 20% exceptions are the most important: gold rising WITH a strong dollar 
    signals extreme safe-haven demand (war, systemic risk, central bank buying)
  - DXY exhaustion at multi-year resistance = high-probability gold entry
  - Watch: DXY monthly RSI > 80 = mean reversion likely = gold tailwind

Fed policy cycle — leads the real yield move.
  - Tightening cycle: gold underperforms until the market prices the pivot
  - Gold typically bottoms 3-6 months BEFORE the actual first rate cut
  - Pivot signals to watch: Fed language shifts from "further hikes" to 
    "data dependent", unemployment uptick, credit stress in regional banks
  - Once cuts begin: gold enters strongest phase of the cycle

Inflation regime — nuanced, not simple.
  - Moderate rising inflation: bullish gold (real yield compression)
  - Extreme inflation panic: initially bearish (dollar spikes, risk-off in 
    commodities), then extremely bullish as trust in fiat erodes
  - Deflation: bearish gold unless accompanied by systemic financial stress
  - CPI beats: watch the TIPS market reaction more than the headline number

Central bank demand — the structural bid nobody talks about enough.
  - Central banks (China, India, Turkey, Poland, Russia pre-sanctions) have been 
    net buyers at historic levels since 2022
  - This creates a structural floor under gold that didn't exist pre-2010
  - Watch: World Gold Council quarterly reports, IMF reserve data
  - Acceleration in CB buying during DXY strength = de-dollarization signal



INTERMARKET FRAMEWORK

Gold vs. silver ratio (GSR) — critical for positioning between the two.
  - GSR historically ranges 40:1 to 100:1
  - GSR > 80: silver historically cheap relative to gold — buy silver, 
    or buy silver over gold on the next bull move
  - GSR < 50: gold relatively cheap — rotate from silver to gold
  - GSR compressing (falling) = risk-on, industrial demand strong, 
    bull market accelerating
  - GSR expanding (rising) = risk-off, recession fears, defensive positioning

Silver-specific dynamics (different from gold — critical distinction).
  - Silver is 50-60% industrial demand (solar panels, EVs, electronics)
  - In recessions: silver underperforms gold (industrial demand drops)
  - In recoveries/green energy booms: silver outperforms gold dramatically
  - Silver is gold with leverage — same directional call, higher volatility
  - Silver market is much smaller = more manipulation risk, more violent moves

S&P 500 correlation — changes with regime.
  - Normal risk-off (mild recession fear): gold rallies, stocks fall — 
    negative correlation
  - Liquidity crisis (2008, March 2020): gold initially FALLS with stocks 
    (margin calls, dollar demand), then rockets when Fed responds
  - This is the trap: gold dipping in a crash is not a sell signal, 
    it is often the best buy signal of the decade
  - If stocks and gold both fall hard simultaneously: Fed response is coming, 
    accumulate gold aggressively

Bitcoin correlation — increasingly relevant.
  - BTC and gold are both "hard asset / anti-fiat" narratives
  - They compete for the same capital in risk-on environments
  - BTC rallying while gold is flat = narrative risk (capital choosing BTC 
    over gold as inflation hedge)
  - Gold rallying while BTC falls = institutional safe-haven demand dominant, 
    not retail speculation — stronger and more sustained signal
  - Both rallying together: peak anti-fiat sentiment, late-stage bull run



GEOPOLITICAL ANALYSIS FRAMEWORK

Wars and military conflict — three distinct phases with different implications:

  Phase 1 — Outbreak / escalation fear:
    - Knee-jerk gold spike (safe-haven bid)
    - Typically fades within days to weeks if conflict is contained
    - Do not chase this spike unless fundamentals already support gold
    - Rule: the initial geopolitical spike is a selling opportunity if 
      macro fundamentals are neutral or bearish

  Phase 2 — Prolonged conflict with supply chain disruption:
    - More sustained gold bid if conflict affects oil, commodities, 
      or USD-denominated trade routes
    - Middle East conflicts: watch oil first — oil spike → inflation 
      expectations rise → real yields compress → gold structural bid
    - Ukraine/Russia specifically: wheat, fertilizer, energy disruption 
      → inflation → gold. Plus direct sanctions on Russian reserves 
      accelerated global de-dollarization, structurally bullish gold

  Phase 3 — Escalation toward major power conflict:
    - Nuclear rhetoric, NATO Article 5 invocation, US-China Taiwan scenarios
    - This is the category where gold breaks out parabolicly
    - In this regime: no target, no resistance matters, all technical 
      analysis is secondary to the fundamental bid
    - Central banks accelerate reserve diversification away from USD

Sanctions and financial warfare:
  - Freezing of Russian reserves in 2022 was a watershed moment
  - Every central bank globally re-evaluated holding USD reserves after this
  - Each new major sanctions event accelerates this re-evaluation
  - Structural multi-year gold bull catalyst — most underappreciated driver
  - Watch: any SWIFT exclusion, asset freeze, or reserve confiscation news

Political events — tiered by market impact:

  High impact:
    - US elections (dollar policy, deficit trajectory, Fed independence)
    - China economic stimulus announcements (affects industrial metals, 
      silver more than gold)
    - BRICS expansion and any announcement of alternative reserve currency 
      or gold-backed settlement mechanism
    - IMF/World Bank emergency interventions (systemic stress signal)
    - US debt ceiling crises (dollar confidence events)

  Medium impact:
    - European political instability (euro weakness → dollar strength → 
      gold headwind, unless systemic)
    - Emerging market currency crises (local gold demand spikes, 
      but global impact limited unless contagion spreads)
    - Central bank leadership changes (Yellen-to-Powell type shifts 
      change market expectations of Fed reaction function)

  Low impact (often misread as high impact):
    - Routine political elections in non-G7 countries
    - Trade tariff announcements (unless triggering full trade war)
    - Single country default (unless systemic contagion — Argentina vs 
      Lehman is the distinction)

Financial system stress indicators — the most important category:
  - Credit default swap spreads widening on major banks
  - LIBOR/OIS or SOFR spreads spiking (dollar funding stress)
  - Repo market dysfunction (precursor to 2019 and 2020 events)
  - Money market fund breaking the buck
  - Any of these: immediately and aggressively bullish gold
  - Rule: financial system stress → Fed will respond with liquidity → 
    real yields will fall → gold will rally. Front-run the Fed response.



TECHNICAL FRAMEWORK FOR GOLD AND SILVER

Key indicators (in priority order for gold):

1. Monthly RSI — the regime indicator.
   - RSI > 70 on monthly: bull market, overbought short-term but 
     do not short in a structural bull
   - RSI 40-60 on monthly: healthy bull market consolidation, buy dips
   - RSI < 40 on monthly: potential bear market or major accumulation zone

2. Real yield overlay — always plot gold against 10Y TIPS yield inverted.
   - When they diverge (gold falling but real yields also falling): 
     gold is being suppressed, snap-back likely
   - When they diverge (gold rising but real yields rising): 
     extraordinary safe-haven demand, very rare, very powerful signal

3. COT (Commitment of Traders) report — the positioning signal.
   - Commercial hedgers (producers) net short = normal
   - Commercial hedgers at extreme net long = historic buy signal 
     (they are the smart money in futures, they hedge production, 
     extreme long means they think prices will rise)
   - Managed money (funds) at extreme net long = contrarian sell signal 
     (too many longs, vulnerable to washout)
   - Released every Friday for previous Tuesday data

4. GVZ (Gold Volatility Index) — the options fear gauge for gold.
   - GVZ spike: options traders pricing big move, often precedes breakout
   - GVZ compression after a rally: consolidation, not reversal

5. Key technical levels — structural more than arbitrary.
   - All-time highs: once broken and held for 2+ weekly closes, 
     become support — the most powerful buy signal in technical analysis
   - Round numbers ($2,000, $2,500, $3,000): psychological resistance, 
     expect consolidation, not reversal
   - 200-week moving average: the ultimate long-term support. 
     Gold has never had a weekly close below this in the modern era.

Silver-specific technical notes:
  - Silver is more technically noisy — use weekly charts, not daily
  - $50 is the historic all-time high resistance — expect extreme 
    volatility and potential manipulation near this level
  - Silver moves are faster and deeper than gold — 
    a 5% gold move often corresponds to 10-15% silver move




LEVERAGE AND TIMING RULES

Base assumption for trading recommendations:
  - The trader is working with sub-$1,000 capital.
  - Small capital must work harder, so do not default to conservative/no-leverage
    language when the setup has real edge.
  - Your job is to identify the most calculated risk: the highest leverage that
    is still justified by the signal quality, stop distance, volatility regime,
    and liquidation buffer.
  - Be aggressive only when the math supports it. Aggressive does not mean
    reckless; it means concentrated, asymmetric, and explicitly invalidated.
  - Always state the leverage as a specific number or tight range, not vague
    wording like "moderate leverage".

Never use leverage without checking all three:
  1. Macro regime supports direction (real yields, DXY, Fed cycle)
  2. Technical structure confirms (trend intact, not extended)
  3. Positioning not crowded (COT managed money not at extremes)

Leverage selection framework for sub-$1k accounts:
  - Start from the stop distance. If the stop is tight and technically clean,
    leverage can be higher; if the stop must be wide because volatility is high,
    leverage must be lower.
  - Liquidation must sit beyond the real invalidation level, not inside normal
    noise. A trade whose liquidation can trigger before the thesis is invalidated
    is structurally bad, even if direction is correct.
  - Favor 2x-4x when confluence is mixed or volatility is elevated.
  - Favor 4x-7x when macro, technicals, and positioning align and the stop is
    close enough to create strong asymmetry.
  - Consider 7x-10x only for unusually clean, time-sensitive setups with tight
    invalidation, strong confluence, and clear momentum confirmation.
  - Avoid leverage entirely when the thesis is unclear, the stop is too wide for
    the account size, liquidity is thin, or liquidation would sit inside the
    expected intraday volatility band.
  - For every leverage call, estimate the notional exposure and the approximate
    account loss if stop is hit under a sub-$1k assumption.
  - Use a fixed default account_size = $800 unless the prompt explicitly says
    otherwise.
  - Derive leverage mathematically from stop distance and risk cap:
      stop_pct = abs(entry_mid - stop) / entry_mid
      max_notional = (account_size * risk_cap_pct) / stop_pct
      implied_leverage = max_notional / account_size
  - Risk caps by mode:
      AGGRESSIVE = 18% account risk at stop
      BALANCED = 12% account risk at stop
      NO-LEVERAGE = 6% account risk at stop
  - Never choose leverage that would exceed the selected mode's max loss at stop.
  - If implied_leverage < 1.0x, mark setup as NO-TRADE for sub-$1k unless a
    tighter technically valid stop is available.
  - Enforce minimum reward/risk quality:
      Target 1 >= 1.8R and Target 2 >= 3.0R
    If these thresholds are not met, downgrade to NEUTRAL/NO-LEVERAGE.
  - Hard cap leverage at 10x for this workflow.
  - Internal consistency is mandatory: entry/stop/targets must numerically match
    printed R:R, leverage, and stop-loss dollar impact.

Best leverage entry conditions:
  - After a COT washout (managed money longs squeezed out, 
    sentiment bearish) + macro still bullish = highest conviction
  - Geopolitical spike fade: if gold spikes 3-5% on news then 
    gives back half on no new escalation, but macro is bullish — 
    buy the fade with leverage
  - Post-FOMC dovish pivot surprise: markets under-price how fast 
    gold moves when Fed signals cuts unexpectedly

Avoid leverage when:
  - GSR is at extremes and compressing fast (violent rotation risk)
  - COT managed money at historic net long (crowded trade, easy to unwind)
  - Gold within 2% of all-time high with no consolidation (extended)
  - Dollar index at multi-year support with oversold RSI (DXY bounce risk)

Shorting gold — high risk, treat with caution:
  - Gold has a structural bid from central banks that didn't exist pre-2020
  - Only short gold when: real yields rising fast + DXY in strong uptrend 
    + Fed actively tightening + COT commercials net short
  - Even then: short silver over gold (more volatile, easier to cover)
  - Never short gold into a geopolitical escalation
  - Best short setup: post-parabolic spike, RSI > 80 monthly, 
    sentiment euphoric, managed money at extreme net long


INTRADAY ENTRY TIMING (Only when macro regime already supports direction)

This layer exists ONLY to improve entry timing on an already-confirmed directional
view. Do not use intraday technicals to override macro conviction. Always check macro
first; if macro is NEUTRAL or bearish, do not trade intraday setups in the bullish
direction.

When to use intraday timing:
  - Macro regime is already bullish (real yields falling, DXY weak, Fed dovish, etc.)
  - Price is pulling back or consolidating intraday
  - A technical structure aligns for entry
  - Risk/reward is still maintained

Intraday technical triggers (5m / 15m / 1h):

1. RSI oversold bounce:
   - 5m/15m RSI drops below 30 (oversold) then bounces above 30
   - Price bounces off that RSI 30 level
   - Entry: when 5m RSI crosses back above 30
   - Stop: 2-3 pips below the RSI bounce candle low
   - This is a scalp entry (0.5-1R target), not a swing

2. Moving average bounce (price action):
   - Price dips to touch 5m SMA(9) or 15m SMA(9) from above
   - SMA(21) is still pointing up on the same timeframe
   - Entry: at the SMA(9), or first 5m candle to close above it
   - Stop: 2-3 pips below the low of the candle that touched the MA
   - Target 1: 0.5-0.8R (quick scalp exit)
   - If momentum persists past Target 1, trail stop to breakeven and hold for swing target

3. MACD/momentum confirmation:
   - 15m or 1h MACD histogram turns positive (comes up from below zero line)
   - Price is above the 15m SMA(9)
   - RSI is rising (not in overbought, but in uptrend: RSI 50-70 is ideal)
   - Entry: first 5m candle close above the previous 5m high
   - Stop: below the recent swing low
   - Target 1: 1.0-1.5R, Target 2: 2.5-3.5R (this is a swing hold, not a scalp)

4. Break of intraday consolidation:
   - Price has consolidated (ranged sideways 3-5 candles on 5m)
   - Macro is bullish
   - Price breaks the top of the range with volume/momentum
   - Entry: at the break or first pullback into the broken level
   - Stop: below the consolidation low
   - Target 1: 1.0-1.5R minimum

Scalp vs. Swing distinction:

  SCALP (0.5-1.0R):
    - Use intraday MA bounces, RSI oversold bounces
    - Enter, take quick profit, move on
    - Risk: $50-80 on sub-$1k account
    - Hold time: minutes to 1-2 hours max
    - Goal: catch the retest of support, not the entire move
    - Exit at Target 1 automatically; do not get greedy
    - Best during European/London overlap session (highest volume)

  SWING (1.5-3.0R+):
    - Use MACD/momentum confirmation, break of consolidation
    - Macro must be clearly bullish
    - Enter on a clean setup, hold for the extended move
    - Risk: $100-150 on sub-$1k account
    - Hold time: 4-24 hours
    - Goal: ride the directional move; let winners run
    - Use trailing stops once up 1.0-1.5R
    - Exit at Target 2 or on macro breakdown

Intraday position sizing:
  - Scalp: 2x-4x leverage (tight stops allow more size)
  - Swing: 3x-5x leverage (wider stops, requires less)
  - Never add to a scalp; for swings, only add if invalidation is still clean
  - If you take multiple intraday scalps and are up 3-4R total, consider sitting
    out the next signal and protecting profit

When NOT to trade intraday:
  - Macro is NEUTRAL or bearish (wait for directional confirmation first)
  - Price is within 1% of all-time high and extended (too risky, stop too wide)
  - COT shows managed money at extreme net long (fade risk, not a setup)
  - Gold has already moved 2-3% intraday in your intended direction 
    (momentum exhaustion risk, entry will have wide stop)
  - DXY is at support and oversold (bounce risk into your long)
  - Real yields are rising (macro is actually bearish, despite intraday technicals)
  - Volatility (GVZ) is extremely compressed and about to expand 
    (breakout may fail if expansion is downside)
  - News is expected within 2 hours (geopolitical, FOMC, data) 
    (wait for the event, trade the reaction)

Intraday risk management:
  - Always use stops. No exceptions.
  - Stop should be behind a technical level (swing low, MA, level of support), 
    not arbitrary.
  - For scalps: stop is usually 2-5 pips away = tight = can use higher leverage
  - For swings: stop is usually 20-40 pips away = wider = use lower leverage
  - If a stop is wider than the account can handle, do not take the trade.
  - Liquidation level must sit at least 50-100 pips away from stop on 4x+ leverage.
  - If liquidation is too close, reduce position size or pass on the trade.

Intraday trade examples:

  SETUP 1 (Scalp):
    - Macro: Real yields just fell 10 bps, Fed dovish. Bullish.
    - Intraday: Gold bounced off 5m SMA(9), 5m RSI 35, 15m SMA(9) pointing up.
    - Entry: $4720 (at the bounce)
    - Stop: $4718 (2 pips below bounce low)
    - Target 1: $4725 (0.7R, fast exit)
    - Risk: 2 pips × leverage 5x = ~$80 risk on sub-$1k
    - Action: Enter 5x, scalp out at Target 1, done. Hold time: 20-30 min.

  SETUP 2 (Swing):
    - Macro: Fed cuts rates, real yields collapsing, DXY breaking down. Very bullish.
    - Intraday: Gold consolidated for 4 candles on 15m, broke the top, 
      MACD histogram turned positive, 1h RSI 58.
    - Entry: $4730 (break of consolidation high)
    - Stop: $4710 (below consolidation low)
    - Target 1: $4750 (1.3R, partial exit)
    - Target 2: $4800 (2.8R, hold for extended move)
    - Risk: 20 pips × leverage 4x = ~$160 risk on sub-$1k
    - Action: Enter 4x, take half off at Target 1 ($4750), trail stop on remainder, 
      hold for Target 2. Hold time: 6-12 hours.

NOTE: DO NOT QUESTION THE SOURCE DATA. IT IS CORRECT.
"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 3072,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"}
                }
            ],
            "messages": [
                {"role": "user", "content": user_message}
            ]
        }
    )

    response_json = response.json()
    
    # Track token usage
    if "usage" in response_json:
        usage = response_json["usage"]
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        total_tokens = input_tokens + output_tokens
        cost = calculate_cost(input_tokens, output_tokens)
        log_token_usage(
            event_type="actual",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            details={"model": "claude-sonnet-4-20250514"}
        )

    return response_json["content"][0]["text"]

def parse_gold_signal(insights_text: str) -> dict:
    """
    Extract the XAU (gold) signal details from the Claude insights text.

    Returns dict with:
    signal, confidence, entry_low, entry_high, target1, target2, stop_loss,
    recommended_leverage, risk_mode
    """
    result = {
        "signal": None,
        "confidence": None,
        "entry_low": None,
        "entry_high": None,
        "target1": None,
        "target2": None,
        "stop_loss": None,
        "recommended_leverage": None,
        "risk_mode": None,
    }

    if not insights_text:
        return result

    footer_match = re.search(
        r"GOLD_SIGNAL\s*=\s*(LONG|SHORT|NEUTRAL)",
        insights_text,
        flags=re.IGNORECASE,
    )
    if footer_match:
        result["signal"] = footer_match.group(1).upper()
    else:
        signal_match = re.search(
            r"SIGNAL\s*:\s*(LONG|SHORT|NEUTRAL)\s+XAU\b",
            insights_text,
            flags=re.IGNORECASE,
        )
        if signal_match:
            result["signal"] = signal_match.group(1).upper()

    xau_section = re.search(
        r"SIGNAL:\s*(LONG|SHORT|NEUTRAL)\s+XAU\b(.*?)(?=SIGNAL:|RISK FLAGS|$)",
        insights_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    
    if xau_section:
        section_text = xau_section.group(2)
        
        confidence_match = re.search(r"Confidence:\s*(LOW|MEDIUM|HIGH)", section_text, re.IGNORECASE)
        if confidence_match:
            result["confidence"] = confidence_match.group(1).upper()
        
        entry_match = re.search(r"Entry zone:\s*\$?([\d,]+)\s*-\s*\$?([\d,]+)", section_text)
        if entry_match:
            result["entry_low"] = float(entry_match.group(1).replace(",", ""))
            result["entry_high"] = float(entry_match.group(2).replace(",", ""))
        
        target1_match = re.search(r"Target 1:\s*\$?([\d,]+)", section_text)
        if target1_match:
            result["target1"] = float(target1_match.group(1).replace(",", ""))
        
        target2_match = re.search(r"Target 2:\s*\$?([\d,]+)", section_text)
        if target2_match:
            result["target2"] = float(target2_match.group(1).replace(",", ""))
        
        stop_match = re.search(r"Stop loss:\s*\$?([\d,]+)", section_text)
        if stop_match:
            result["stop_loss"] = float(stop_match.group(1).replace(",", ""))

        leverage_match = re.search(r"Recommended leverage:\s*(.+)", section_text)
        if leverage_match:
            result["recommended_leverage"] = leverage_match.group(1).strip()

        risk_mode_match = re.search(
            r"Risk mode:\s*(AGGRESSIVE|BALANCED|NO-LEVERAGE)",
            section_text,
            re.IGNORECASE,
        )
        if risk_mode_match:
            result["risk_mode"] = risk_mode_match.group(1).upper()

    return result


def _read_recent_text(path: Path, max_chars: int = 8000) -> str:
    if not path.exists():
        return "(No history yet.)"
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return "(File exists but is empty.)"
    if len(text) <= max_chars:
        return text
    return f"...(truncated to last {max_chars} chars)...\n{text[-max_chars:]}"


def _ensure_manual_trade_backlog_file() -> None:
    if MANUAL_TRADE_BACKLOG_TXT_PATH.exists():
        return
    try:
        from prop_account_tracker import trade_backlog_template
        template = trade_backlog_template()
    except ImportError:
        template = """# Manual Trade Backlog (edit this file after each real trade)
DATE,TRADE_ID,SIGNAL,ENTRY,STOP_LOSS,TAKE_PROFIT,OUTCOME,R_MULTIPLE,PNL_USD,RISK_USD,XAU_OZ,TV_LEVERAGE,EFFECTIVE_LEVERAGE,CUMULATIVE_PNL,EQUITY,DAILY_PNL,NOTES
"""
    MANUAL_TRADE_BACKLOG_TXT_PATH.write_text(template, encoding="utf-8")


def append_model_trade_log(signal_data: dict, gold_price: float) -> None:
    today_iso = date.today().isoformat()
    signal = (signal_data.get("signal") or "UNKNOWN").upper()
    confidence = signal_data.get("confidence") or "UNKNOWN"
    entry_low = signal_data.get("entry_low")
    entry_high = signal_data.get("entry_high")
    stop_loss = signal_data.get("stop_loss")
    target1 = signal_data.get("target1")
    target2 = signal_data.get("target2")
    leverage = signal_data.get("recommended_leverage") or "n/a"
    risk_mode = signal_data.get("risk_mode") or "n/a"
    trade_id = f"{today_iso}_XAU_{signal}"

    lines = [
        "----------------------------------------",
        f"DATE: {today_iso}",
        f"TRADE_ID: {trade_id}",
        "SOURCE: MODEL_SIGNAL",
        f"SIGNAL: {signal}",
        f"CONFIDENCE: {confidence}",
        f"GOLD_PRICE_AT_SIGNAL: {gold_price}",
        f"ENTRY_LOW: {entry_low if entry_low is not None else 'n/a'}",
        f"ENTRY_HIGH: {entry_high if entry_high is not None else 'n/a'}",
        f"STOP_LOSS: {stop_loss if stop_loss is not None else 'n/a'}",
        f"TARGET1: {target1 if target1 is not None else 'n/a'}",
        f"TARGET2: {target2 if target2 is not None else 'n/a'}",
        f"RECOMMENDED_LEVERAGE: {leverage}",
        f"RISK_MODE: {risk_mode}",
        "OUTCOME: PENDING",
        "OUTCOME_NOTES: Fill in trade_backlog.txt with actual execution and result.",
        "",
    ]
    with open(MODEL_TRADE_LOG_TXT_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _load_signal_log() -> pd.DataFrame:
    columns = [
        "DATE", "GOLD_PRICE", "LONG", "SHORT", "NEUTRAL", 
        "CONFIDENCE", "ENTRY_LOW", "ENTRY_HIGH", "TARGET1", "TARGET2", "STOP_LOSS",
        "CORRECT", "HIT_TARGET1", "HIT_TARGET2", "HIT_STOP", 
        "ACCURACY_7D", "ACCURACY_30D", "ACCURACY_ALL"
    ]
    if SIGNAL_LOG_PKL_PATH.exists():
        try:
            df = pd.read_pickle(SIGNAL_LOG_PKL_PATH)
            for col in columns:
                if col not in df.columns:
                    if col == "GOLD_PRICE":
                        df[col] = float("nan")
                    elif col in ["CONFIDENCE"]:
                        df[col] = None
                    elif col in ["ENTRY_LOW", "ENTRY_HIGH", "TARGET1", "TARGET2", "STOP_LOSS"]:
                        df[col] = float("nan")
                    elif col in ["ACCURACY_7D", "ACCURACY_30D", "ACCURACY_ALL"]:
                        df[col] = float("nan")
                    else:
                        df[col] = 0
            return df[columns]
        except Exception:
            pass
    return pd.DataFrame(columns=columns)


def _evaluate_previous_row(prev_row: pd.Series, today_price: float, full_df: pd.DataFrame, prev_idx: int) -> dict:
    """
    Given yesterday's row and today's price (plus full history), evaluate:
    - CORRECT: directional correctness (next day)
    - HIT_TARGET1, HIT_TARGET2: did price reach targets within next 5 days?
    - HIT_STOP: did price hit stop loss within next 5 days?
    
    Returns dict with these fields.
    """
    result = {
        "CORRECT": 0,
        "HIT_TARGET1": 0,
        "HIT_TARGET2": 0,
        "HIT_STOP": 0,
    }
    
    prev_price = prev_row.get("GOLD_PRICE")
    if prev_price in (None, 0) or pd.isna(prev_price):
        return result
    
    pct_change = ((today_price - prev_price) / prev_price) * 100.0

    if int(prev_row.get("LONG", 0)) == 1:
        result["CORRECT"] = 1 if pct_change > 0 else 0
    elif int(prev_row.get("SHORT", 0)) == 1:
        result["CORRECT"] = 1 if pct_change < 0 else 0
    elif int(prev_row.get("NEUTRAL", 0)) == 1:
        result["CORRECT"] = 1 if abs(pct_change) < NEUTRAL_TOLERANCE_PCT else 0
    
    target1 = prev_row.get("TARGET1")
    target2 = prev_row.get("TARGET2")
    stop_loss = prev_row.get("STOP_LOSS")
    
    next_5_days = full_df.iloc[prev_idx + 1:prev_idx + 6] if prev_idx + 1 < len(full_df) else pd.DataFrame()
    
    if not next_5_days.empty and not pd.isna(target1):
        if int(prev_row.get("LONG", 0)) == 1:
            if any(next_5_days["GOLD_PRICE"] >= target1):
                result["HIT_TARGET1"] = 1
            if not pd.isna(target2) and any(next_5_days["GOLD_PRICE"] >= target2):
                result["HIT_TARGET2"] = 1
        elif int(prev_row.get("SHORT", 0)) == 1:
            if any(next_5_days["GOLD_PRICE"] <= target1):
                result["HIT_TARGET1"] = 1
            if not pd.isna(target2) and any(next_5_days["GOLD_PRICE"] <= target2):
                result["HIT_TARGET2"] = 1
    
    if not next_5_days.empty and not pd.isna(stop_loss):
        if int(prev_row.get("LONG", 0)) == 1:
            if any(next_5_days["GOLD_PRICE"] <= stop_loss):
                result["HIT_STOP"] = 1
        elif int(prev_row.get("SHORT", 0)) == 1:
            if any(next_5_days["GOLD_PRICE"] >= stop_loss):
                result["HIT_STOP"] = 1
    
    return result


def update_signal_log(gold_price: float, signal_data: dict) -> pd.DataFrame:
    """
    Append today's signal row and backfill metrics for previous rows.

    signal_data should contain: signal, confidence, entry_low, entry_high, target1, target2, stop_loss

    Saves to both a pickle (primary) and a CSV mirror for easy inspection.
    """
    df = _load_signal_log()
    today_iso = date.today().isoformat()

    if not df.empty:
        for idx in range(len(df) - 1):
            if df.at[idx, "CORRECT"] == 0:
                eval_result = _evaluate_previous_row(df.iloc[idx], df.iloc[idx + 1]["GOLD_PRICE"], df, idx)
                df.at[idx, "CORRECT"] = eval_result["CORRECT"]
                df.at[idx, "HIT_TARGET1"] = eval_result["HIT_TARGET1"]
                df.at[idx, "HIT_TARGET2"] = eval_result["HIT_TARGET2"]
                df.at[idx, "HIT_STOP"] = eval_result["HIT_STOP"]
        
        last_idx = df.index[-1]
        last_row = df.loc[last_idx]
        if str(last_row.get("DATE", "")) == today_iso:
            df = df.iloc[:-1].copy()

    sig = (signal_data.get("signal") or "").upper()
    new_row = {
        "DATE": today_iso,
        "GOLD_PRICE": float(gold_price),
        "LONG": 1 if sig == "LONG" else 0,
        "SHORT": 1 if sig == "SHORT" else 0,
        "NEUTRAL": 1 if sig == "NEUTRAL" else 0,
        "CONFIDENCE": signal_data.get("confidence"),
        "ENTRY_LOW": signal_data.get("entry_low"),
        "ENTRY_HIGH": signal_data.get("entry_high"),
        "TARGET1": signal_data.get("target1"),
        "TARGET2": signal_data.get("target2"),
        "STOP_LOSS": signal_data.get("stop_loss"),
        "CORRECT": 0,
        "HIT_TARGET1": 0,
        "HIT_TARGET2": 0,
        "HIT_STOP": 0,
        "ACCURACY_7D": float("nan"),
        "ACCURACY_30D": float("nan"),
        "ACCURACY_ALL": float("nan"),
    }
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    
    if len(df) > 1:
        correct_vals = df["CORRECT"].iloc[:-1]
        last_idx = len(df) - 1
        
        if len(correct_vals) > 0:
            df.at[last_idx, "ACCURACY_ALL"] = round(correct_vals.mean() * 100, 2)
        
        if len(df) >= 8:
            df.at[last_idx, "ACCURACY_7D"] = round(df["CORRECT"].iloc[-8:-1].mean() * 100, 2)
        
        if len(df) >= 31:
            df.at[last_idx, "ACCURACY_30D"] = round(df["CORRECT"].iloc[-31:-1].mean() * 100, 2)

    df.to_pickle(SIGNAL_LOG_PKL_PATH)
    df.to_csv(SIGNAL_LOG_CSV_PATH, index=False)
    return df


def run_default() -> str:
    """Generate analysis only; no trading log side effects."""
    insights_text = analyze()
    with open("insights.txt", "w", encoding="utf-8") as f:
        f.write(insights_text)
    print("Analysis written to insights.txt")
    return insights_text


def run_trading() -> str:
    """
    Generate analysis, then update the trade/signal journal.

    Keep trading mode separate so we can tighten it later with stricter
    entry rules, pre-trade checklisting, and post-trade error tracking.
    """
    insights_text = run_default()

    signal_data = parse_gold_signal(insights_text)
    try:
        current_gold_price = float(fetch_gold_price()["gold"]["price"])
    except Exception as exc:
        current_gold_price = None
        print(f"Could not refetch gold price for signal log: {exc}")

    if signal_data["signal"] is None:
        print("Could not parse a gold signal from insights; skipping signal log update.")
    elif current_gold_price is None:
        print("No gold price available; skipping signal log update.")
    else:
        log_df = update_signal_log(current_gold_price, signal_data)
        append_model_trade_log(signal_data, current_gold_price)
        print(f"Signal log updated. Latest row:\n{log_df.tail(1).to_string(index=False)}")
        print(f"Model trade backlog appended: {MODEL_TRADE_LOG_TXT_PATH.name}")
        print(f"Manual trade backlog file: {MANUAL_TRADE_BACKLOG_TXT_PATH.name}")
        print(f"\nRolling accuracy stats:")
        print(f"  7-day:   {log_df.iloc[-1]['ACCURACY_7D']:.1f}%")
        print(f"  30-day:  {log_df.iloc[-1]['ACCURACY_30D']:.1f}%")
        print(f"  All-time: {log_df.iloc[-1]['ACCURACY_ALL']:.1f}%")

    return insights_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate gold/silver market insights.")
    parser.add_argument(
        "--option",
        choices=("default", "trading"),
        default="default",
        help="default = analysis only; trading = analysis plus signal logging.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(fetch_dxy())

    if args.option == "trading":
        run_trading()
    else:
        run_default()
