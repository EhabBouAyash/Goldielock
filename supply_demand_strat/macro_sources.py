"""Macro inputs used only by the supply/demand strategy.

The collector favors first-party releases and measured cross-asset data over
commentary. Each source fails independently so one blocked feed cannot erase
the rest of the macro context.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import requests


PACKAGE_DIR = Path(__file__).resolve().parent
CACHE_PATH = PACKAGE_DIR / ".macro_context_cache.json"
CALENDAR_CACHE_PATH = PACKAGE_DIR / ".macro_calendar_cache.xml"
CACHE_MAX_AGE_SEC = 10 * 60
CACHE_FALLBACK_MAX_AGE_SEC = 6 * 60 * 60
CALENDAR_CACHE_MAX_AGE_SEC = 3 * 60 * 60
REQUEST_TIMEOUT_SEC = 18
REQUEST_HEADERS = {
    "User-Agent": "Goldielock-SupplyDemand-Macro/1.0 (+local research pipeline)"
}

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
CFTC_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
TGA_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/dts/operating_cash_balance"
)
AUCTIONS_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/od/auctions_query"
)
NY_FED_REPO_URL = (
    "https://markets.newyorkfed.org/api/rp/all/all/results/lastTwoWeeks.json"
)

FRED_SERIES = {
    "DFII10": {"name": "us_10y_real_yield", "units": "percent"},
    "DGS10": {"name": "us_10y_nominal_yield", "units": "percent"},
    "DGS2": {"name": "us_2y_nominal_yield", "units": "percent"},
    "T10YIE": {"name": "us_10y_breakeven_inflation", "units": "percent"},
    "VIXCLS": {"name": "vix_close", "units": "index"},
    "DCOILWTICO": {"name": "wti_spot", "units": "usd_per_barrel"},
}

# Tier 1 is an official issuer. Tier 2 is established market reporting. Tier 3
# supplies geopolitical breadth. Gold-specialist commentary is deliberately
# limited so it cannot crowd out rates, inflation, or event risk.
RSS_FEEDS = (
    {
        "name": "Federal Reserve Monetary Policy",
        "url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "channel": "central_bank",
        "tier": 1,
        "max_age_days": 35,
    },
    {
        "name": "Federal Reserve Speeches",
        "url": "https://www.federalreserve.gov/feeds/speeches.xml",
        "channel": "central_bank",
        "tier": 1,
        "max_age_days": 30,
    },
    {
        "name": "BLS Consumer Price Index",
        "url": "https://www.bls.gov/feed/cpi.rss",
        "channel": "inflation",
        "tier": 1,
        "max_age_days": 45,
    },
    {
        "name": "BLS Employment Situation",
        "url": "https://www.bls.gov/feed/empsit.rss",
        "channel": "labor",
        "tier": 1,
        "max_age_days": 45,
    },
    {
        "name": "BLS Producer Price Index",
        "url": "https://www.bls.gov/feed/ppi.rss",
        "channel": "inflation",
        "tier": 1,
        "max_age_days": 45,
    },
    {
        "name": "BLS Job Openings and Labor Turnover",
        "url": "https://www.bls.gov/feed/jolts.rss",
        "channel": "labor",
        "tier": 1,
        "max_age_days": 45,
    },
    {
        "name": "BEA Economic Releases",
        "url": "https://apps.bea.gov/rss/rss.xml",
        "channel": "growth_pce",
        "tier": 1,
        "max_age_days": 45,
    },
    {
        "name": "European Central Bank",
        "url": "https://www.ecb.europa.eu/rss/press.html",
        "channel": "central_bank",
        "tier": 1,
        "max_age_days": 14,
    },
    {
        "name": "EIA Today in Energy",
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
        "channel": "energy",
        "tier": 1,
        "max_age_days": 14,
    },
    {
        "name": "CNBC Economy",
        "url": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "channel": "economy",
        "tier": 2,
        "max_age_days": 4,
    },
    {
        "name": "Gold News Cross-check",
        "url": (
            "https://news.google.com/rss/search?"
            "q=gold%20%28Federal%20Reserve%20OR%20Treasury%20yields%20OR%20"
            "dollar%20OR%20central%20bank%29%20when%3A2d&hl=en-US&gl=US&"
            "ceid=US%3Aen"
        ),
        "channel": "gold",
        "tier": 2,
        "max_age_days": 3,
        "publisher_allowlist": (
            "reuters", "cnbc", "bloomberg", "kitco", "world gold council",
            "marketwatch", "financial times", "wall street journal",
            "associated press",
        ),
    },
    {
        "name": "BBC World",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "channel": "geopolitics",
        "tier": 3,
        "max_age_days": 2,
    },
    {
        "name": "Al Jazeera",
        "url": "https://www.aljazeera.com/xml/rss/all.xml",
        "channel": "geopolitics",
        "tier": 3,
        "max_age_days": 2,
    },
)

CHANNEL_TERMS = {
    "central_bank": (
        "rate", "inflation", "monetary", "fomc", "policy", "powell",
        "balance sheet", "liquidity", "economic outlook", "minutes",
    ),
    "inflation": ("consumer price", "cpi", "inflation", "core", "shelter"),
    "labor": (
        "employment", "payroll", "unemployment", "wage", "earnings", "jobs",
        "job openings", "jolts", "quits",
    ),
    "growth_pce": (
        "personal income", "personal consumption", "pce", "gross domestic",
        "gdp", "corporate profits",
    ),
    "energy": (
        "oil", "crude", "opec", "petroleum", "gas", "middle east", "iran",
        "russia", "sanction", "shipping",
    ),
    "markets": (
        "treasury", "yield", "dollar", "fed", "rate", "inflation", "tariff",
        "sanction", "war", "oil", "risk", "gold",
    ),
    "economy": (
        "fed", "inflation", "jobs", "gdp", "recession", "tariff", "treasury",
        "yield", "dollar", "debt", "shutdown",
    ),
    "gold": (
        "gold", "xau", "bullion", "central bank", "dollar", "yield", "fed",
        "inflation", "war",
    ),
    "geopolitics": (
        "war", "attack", "missile", "ceasefire", "sanction", "tariff",
        "iran", "israel", "russia", "ukraine", "china", "taiwan", "red sea",
        "shipping", "oil", "nuclear", "trade",
    ),
}


def _strip_tags(value: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", value or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    return " ".join(re.sub(r"<[^>]+>", " ", text).split())


def _child_text(element: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if child.tag.split("}", 1)[-1].lower() in wanted:
            return _strip_tags(child.text or "")
    return ""


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _headline_score(title: str, description: str, feed: dict[str, Any]) -> int:
    text = f"{title} {description}".lower()
    matches = sum(term in text for term in CHANNEL_TERMS[feed["channel"]])
    if matches == 0:
        return 0
    return matches * 3 + (4 - int(feed["tier"])) * 4


def _fetch_feed(
    feed: dict[str, Any],
    per_feed_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        response = requests.get(
            feed["url"],
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as exc:
        return [], {
            "source": feed["name"],
            "status": "error",
            "detail": str(exc)[:180],
        }

    items = [
        element
        for element in root.iter()
        if element.tag.split("}", 1)[-1].lower() in {"item", "entry"}
    ]
    articles: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for item in items:
        title = _child_text(item, "title")
        description = _child_text(item, "description", "summary", "content")
        published_raw = _child_text(item, "pubDate", "published", "updated", "date")
        published = _parse_datetime(published_raw)
        if published is None or published > now + timedelta(hours=1):
            continue
        if published and now - published > timedelta(days=feed["max_age_days"]):
            continue
        link = _child_text(item, "link")
        if not link:
            for child in list(item):
                if child.tag.split("}", 1)[-1].lower() == "link":
                    link = child.attrib.get("href", "")
                    break
        score = _headline_score(title, description, feed)
        publishers = feed.get("publisher_allowlist")
        if publishers and not any(
            publisher in title.lower() for publisher in publishers
        ):
            continue
        if not title or score <= 0:
            continue
        articles.append(
            {
                "title": title[:240],
                "description": description[:320],
                "source": feed["name"],
                "source_tier": feed["tier"],
                "channel": feed["channel"],
                "published_at": published.isoformat() if published else published_raw,
                "age_hours": round((now - published).total_seconds() / 3600, 1),
                "url": link,
                "relevance_score": score,
            }
        )
    articles.sort(
        key=lambda item: (
            -(item["relevance_score"]),
            str(item["published_at"]),
        )
    )
    return articles[:per_feed_limit], {
        "source": feed["name"],
        "status": "ok",
        "accepted": min(len(articles), per_feed_limit),
    }


def fetch_headlines(
    max_total: int = 26,
    per_feed_limit: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch a balanced digest; no source may dominate the prompt."""
    articles: list[dict[str, Any]] = []
    health: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_feed, feed, per_feed_limit): feed
            for feed in RSS_FEEDS
        }
        for future in as_completed(futures):
            feed_articles, feed_health = future.result()
            articles.extend(feed_articles)
            health.append(feed_health)

    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    channel_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    articles.sort(
        key=lambda item: (
            item["source_tier"],
            -item["relevance_score"],
            str(item["published_at"]),
        )
    )
    for article in articles:
        key = re.sub(r"[^a-z0-9]+", " ", article["title"].lower()).strip()
        channel = article["channel"]
        source = article["source"]
        channel_cap = 4 if channel in {"central_bank", "geopolitics"} else 3
        if (
            not key
            or key in seen
            or channel_counts.get(channel, 0) >= channel_cap
            or source_counts.get(source, 0) >= 3
        ):
            continue
        seen.add(key)
        selected.append(article)
        channel_counts[channel] = channel_counts.get(channel, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= max_total:
            break
    health.sort(key=lambda item: item["source"])
    return selected, health


def _series_observations(content: bytes) -> dict[str, list[tuple[str, float]]]:
    files: list[tuple[str, bytes]]
    if zipfile.is_zipfile(io.BytesIO(content)):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = [
                (name, archive.read(name))
                for name in archive.namelist()
                if name.lower().endswith(".csv")
            ]
    else:
        files = [("fred.csv", content)]

    observations = {series_id: [] for series_id in FRED_SERIES}
    for _, raw in files:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        for row in reader:
            date = row.get("observation_date", "")
            for series_id in FRED_SERIES:
                try:
                    value = float(row.get(series_id, ""))
                except (TypeError, ValueError):
                    continue
                observations[series_id].append((date, value))
    return observations


def fetch_cross_asset_snapshot() -> dict[str, Any]:
    """Retrieve measured opportunity-cost, inflation, fear, and oil drivers."""
    try:
        response = requests.get(
            FRED_URL,
            params={"id": ",".join(FRED_SERIES)},
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        observations = _series_observations(response.content)
    except Exception as exc:
        return {"status": "error", "source": "FRED", "error": str(exc)[:180]}

    series: dict[str, Any] = {}
    for series_id, metadata in FRED_SERIES.items():
        values = observations.get(series_id) or []
        if not values:
            continue
        date, value = values[-1]
        prior_value = values[-6][1] if len(values) >= 6 else values[0][1]
        series[metadata["name"]] = {
            "value": round(value, 4),
            "change_5_observations": round(value - prior_value, 4),
            "observation_date": date,
            "units": metadata["units"],
            "series_id": series_id,
        }
    return {
        "status": "ok" if series else "empty",
        "source": "Federal Reserve Economic Data (FRED)",
        "series": series,
    }


def fetch_gold_positioning() -> dict[str, Any]:
    """Fetch official weekly COMEX gold managed-money positioning."""
    try:
        response = requests.get(
            CFTC_URL,
            params={
                "$limit": 2,
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$where": "contract_market_name='GOLD'",
            },
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError("CFTC returned no GOLD rows")
    except Exception as exc:
        return {"status": "error", "source": "CFTC COT", "error": str(exc)[:180]}

    latest = rows[0]
    long_positions = float(latest.get("m_money_positions_long_all") or 0)
    short_positions = float(latest.get("m_money_positions_short_all") or 0)
    previous_net = None
    if len(rows) > 1:
        previous_net = float(rows[1].get("m_money_positions_long_all") or 0) - float(
            rows[1].get("m_money_positions_short_all") or 0
        )
    net = long_positions - short_positions
    return {
        "status": "ok",
        "source": "CFTC Disaggregated COT, futures only",
        "report_date": latest.get("report_date_as_yyyy_mm_dd"),
        "managed_money_long_contracts": int(long_positions),
        "managed_money_short_contracts": int(short_positions),
        "managed_money_net_contracts": int(net),
        "weekly_net_change_contracts": (
            int(net - previous_net) if previous_net is not None else None
        ),
        "note": "Weekly Tuesday positions published Friday; context, not intraday timing.",
    }


def _api_number(value: Any) -> float | None:
    try:
        if value in (None, "", "null"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_liquidity_snapshot() -> dict[str, Any]:
    """Fetch Treasury cash/auction pressure and New York Fed repo usage."""
    snapshot: dict[str, Any] = {
        "status": "ok",
        "sources": ["U.S. Treasury Fiscal Data", "Federal Reserve Bank of New York"],
    }
    errors: list[str] = []

    try:
        response = requests.get(
            TGA_URL,
            params={
                "filter": (
                    "account_type:eq:Treasury General Account (TGA) Closing Balance"
                ),
                "sort": "-record_date",
                "page[size]": 6,
            },
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
        values = [
            (
                row.get("record_date"),
                # Fiscal Data currently places this row's amount in open_today_bal.
                _api_number(row.get("close_today_bal"))
                or _api_number(row.get("open_today_bal")),
            )
            for row in rows
        ]
        values = [(date, value) for date, value in values if date and value is not None]
        if values:
            latest_date, latest_value = values[0]
            comparison = values[-1][1]
            snapshot["treasury_general_account"] = {
                "balance_usd_millions": round(latest_value, 2),
                "change_available_observations_usd_millions": round(
                    latest_value - comparison, 2
                ),
                "observation_date": latest_date,
            }
    except Exception as exc:
        errors.append(f"TGA: {str(exc)[:120]}")

    try:
        today = datetime.now(timezone.utc).date().isoformat()
        response = requests.get(
            AUCTIONS_URL,
            params={
                "filter": (
                    f"auction_date:lte:{today},security_type:in:(Note,Bond)"
                ),
                "sort": "-auction_date",
                "page[size]": 1,
            },
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
        if rows:
            row = rows[0]
            accepted = _api_number(row.get("total_accepted"))
            indirect = _api_number(row.get("indirect_bidder_accepted"))
            snapshot["latest_coupon_auction"] = {
                "auction_date": row.get("auction_date"),
                "security_term": row.get("original_security_term")
                or row.get("security_term"),
                "high_yield_pct": _api_number(row.get("high_yield")),
                "bid_to_cover": _api_number(row.get("bid_to_cover_ratio")),
                "offering_usd": _api_number(row.get("offering_amt")),
                "indirect_award_pct": (
                    round(indirect / accepted * 100, 2)
                    if indirect is not None and accepted
                    else None
                ),
            }
    except Exception as exc:
        errors.append(f"auction: {str(exc)[:120]}")

    try:
        response = requests.get(
            NY_FED_REPO_URL,
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SEC,
        )
        response.raise_for_status()
        operations = ((response.json().get("repo") or {}).get("operations") or [])
        latest_by_type: dict[str, dict[str, Any]] = {}
        for operation in operations:
            operation_type = str(operation.get("operationType") or "")
            if operation_type and operation_type not in latest_by_type:
                latest_by_type[operation_type] = {
                    "operation_date": operation.get("operationDate"),
                    "submitted_usd": _api_number(operation.get("totalAmtSubmitted")),
                    "accepted_usd": _api_number(operation.get("totalAmtAccepted")),
                    "last_updated": operation.get("lastUpdated"),
                }
        snapshot["new_york_fed_repo"] = latest_by_type
    except Exception as exc:
        errors.append(f"repo: {str(exc)[:120]}")

    if errors:
        snapshot["errors"] = errors
        snapshot["status"] = "partial" if len(snapshot) > 3 else "error"
    return snapshot


def _load_calendar_bytes() -> bytes:
    if CALENDAR_CACHE_PATH.exists():
        age = time.time() - CALENDAR_CACHE_PATH.stat().st_mtime
        if age <= CALENDAR_CACHE_MAX_AGE_SEC:
            return CALENDAR_CACHE_PATH.read_bytes()
    response = requests.get(
        CALENDAR_URL,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT_SEC,
    )
    response.raise_for_status()
    CALENDAR_CACHE_PATH.write_bytes(response.content)
    return response.content


def fetch_usd_calendar(max_events: int = 18) -> dict[str, Any]:
    """Return dated USD high/medium events without fabricated fallback dates."""
    try:
        root = ET.fromstring(_load_calendar_bytes())
    except Exception as exc:
        return {
            "status": "unavailable",
            "source": "Forex Factory calendar",
            "events": [],
            "error": str(exc)[:180],
        }

    events: list[dict[str, Any]] = []
    for element in root.iter():
        if element.tag.split("}", 1)[-1].lower() != "event":
            continue
        country = _child_text(element, "country").upper()
        impact = _child_text(element, "impact").lower()
        if country != "USD" or impact not in {"high", "medium"}:
            continue
        events.append(
            {
                "date": _child_text(element, "date"),
                "time": _child_text(element, "time"),
                "title": _child_text(element, "title"),
                "impact": impact,
                "forecast": _child_text(element, "forecast") or None,
                "previous": _child_text(element, "previous") or None,
            }
        )
    impact_order = {"high": 0, "medium": 1}
    events.sort(
        key=lambda event: (
            impact_order[event["impact"]],
            event["date"],
            event["time"],
        )
    )
    return {
        "status": "ok",
        "source": "Forex Factory calendar (nfs.faireconomy.media)",
        "events": events[:max_events],
    }


def _read_cache(max_age_sec: int) -> dict[str, Any] | None:
    try:
        age = time.time() - CACHE_PATH.stat().st_mtime
        if age > max_age_sec:
            return None
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return cached if isinstance(cached, dict) else None
    except (OSError, ValueError):
        return None


def collect_macro_context(dxy: dict[str, Any]) -> dict[str, Any]:
    """Build the bounded macro payload consumed by Gemini."""
    cached = _read_cache(CACHE_MAX_AGE_SEC)
    if cached is not None:
        cached["dxy"] = dxy
        cached["cache_status"] = "fresh"
        return cached

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            cross_asset_future = executor.submit(fetch_cross_asset_snapshot)
            positioning_future = executor.submit(fetch_gold_positioning)
            liquidity_future = executor.submit(fetch_liquidity_snapshot)
            calendar_future = executor.submit(fetch_usd_calendar)
            headlines_future = executor.submit(fetch_headlines)
            headlines, source_health = headlines_future.result()
            context = {
                "as_of_utc": datetime.now(timezone.utc).isoformat(),
                "dxy": dxy,
                "cross_asset_snapshot": cross_asset_future.result(),
                "gold_futures_positioning": positioning_future.result(),
                "fiscal_liquidity_snapshot": liquidity_future.result(),
                "usd_calendar": calendar_future.result(),
                "headlines": headlines,
                "source_health": source_health,
                "cache_status": "live",
            }
        CACHE_PATH.write_text(
            json.dumps(context, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
        return context
    except Exception as exc:
        stale = _read_cache(CACHE_FALLBACK_MAX_AGE_SEC)
        if stale is not None:
            stale["dxy"] = dxy
            stale["cache_status"] = "stale_fallback"
            stale["collection_error"] = str(exc)[:180]
            return stale
        return {
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
            "dxy": dxy,
            "cross_asset_snapshot": {"status": "unavailable"},
            "gold_futures_positioning": {"status": "unavailable"},
            "fiscal_liquidity_snapshot": {"status": "unavailable"},
            "usd_calendar": {"status": "unavailable", "events": []},
            "headlines": [],
            "source_health": [],
            "cache_status": "failed",
            "collection_error": str(exc)[:180],
        }
