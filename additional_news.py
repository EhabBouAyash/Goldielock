"""
Public macro/news feeds for the intraday gold agent.

GNews on the free plan delays articles by about 12 hours, so these feeds carry the
same-day picture: gold desks (FXStreet, Mining.com), the Fed's own releases and
speeches, data prints with actual-vs-expected (ForexLive), geopolitics, and
Google News searches that surface wire headlines in near real time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import time
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

FF_CALENDAR_CACHE_PATH = Path(__file__).resolve().parent / ".ff_calendar_thisweek_cache.xml"
FF_CALENDAR_CACHE_MAX_AGE_SEC = 6 * 60 * 60


def _first_friday(year: int, month: int) -> datetime:
    day = datetime(year, month, 1, tzinfo=timezone.utc)
    # Monday=0 ... Friday=4
    while day.weekday() != 4:
        day = day.replace(day=day.day + 1)
    return day


def _fallback_usd_high_impact_schedule() -> dict:
    """
    Deterministic BLS-style fallback when FF XML is rate-limited/unavailable.
    NFP is first Friday of the month (holiday shifts exist but are rare).
    """
    now = datetime.now(timezone.utc)
    fridays = [_first_friday(now.year, now.month)]
    if now.month == 12:
        fridays.append(_first_friday(now.year + 1, 1))
    else:
        fridays.append(_first_friday(now.year, now.month + 1))

    upcoming = [d for d in fridays if d.date() >= now.date()]
    events = []
    for friday in upcoming[:2]:
        date_str = friday.strftime("%m-%d-%Y")
        for title in (
            "Non-Farm Employment Change",
            "Unemployment Rate",
            "Average Hourly Earnings m/m",
        ):
            events.append({
                "date": date_str,
                "time": "12:30pm",
                "impact": "high",
                "title": title,
                "forecast": "n/a",
                "previous": "n/a",
                "published_at": f"{date_str} 12:30pm",
                "source": "BLS schedule fallback (first Friday; FF feed unavailable)",
            })

    nfp_related = [
        e for e in events
        if "non-farm" in e["title"].lower() or "unemployment" in e["title"].lower()
        or "hourly" in e["title"].lower()
    ]
    label_bits = [
        f"{e['date']} {e['time']} {e['title']}"
        for e in events
        if e["title"] == "Non-Farm Employment Change"
    ]
    return {
        "status": "fallback_bls_schedule",
        "source": "code: first-Friday NFP fallback",
        "events": events,
        "high_impact_count": len(events),
        "nfp_related": nfp_related,
        "label_for_prompt": (
            "USD high-impact calendar FALLBACK (FF unavailable): "
            + "; ".join(label_bits)
            + " — treat as known window, not UNKNOWN"
        ),
    }


GOOGLE_NEWS_SEARCH_URL = "https://news.google.com/rss/search?q={query}+when:1d&hl=en-US&gl=US&ceid=US:en"


def google_news_feed(name: str, query: str, category: str) -> dict:
    return {
        "name": name,
        "url": GOOGLE_NEWS_SEARCH_URL.format(query=quote_plus(query)),
        "category": category,
        "kind": "google_news",
        "max_age_days": 1,
    }


# What each feed is for, in gold terms:
# - gold desk: daily gold write-ups that already tie the move to yields/dollar/Fed
# - fed_policy: the Fed's own statements and speeches (rate expectations)
# - fx_rates_macro: data prints as "actual vs expected" within minutes
# - geopolitics: safe-haven demand (wars, strikes, sanctions)
RSS_FEEDS = [
    {
        "name": "FXStreet News",
        "url": "https://www.fxstreet.com/rss/news",
        "category": "fx_rates_macro",
        "max_age_days": 2,
    },
    {
        "name": "FXStreet Analysis",
        "url": "https://www.fxstreet.com/rss/analysis",
        "category": "gold_commodities",
        "max_age_days": 2,
    },
    {
        "name": "Mining.com Gold",
        "url": "https://www.mining.com/commodity/gold/feed/",
        "category": "gold_commodities",
        "max_age_days": 3,
        # Drop mine-site stories (drill results, output) that do not move the price.
        "require_any": (
            "gold price", "prices", "fed", "dollar", "yield", "inflation", "rate",
            "central bank", "etf", "record", "rally", "slump", "falls", "rises", "safe haven",
        ),
    },
    google_news_feed("Google News: gold", "gold price OR XAUUSD OR \"spot gold\"", "gold_commodities"),
    google_news_feed("Google News: Fed", "Federal Reserve rate OR FOMC OR \"Fed's\"", "fed_policy"),
    google_news_feed(
        "Google News: geopolitics",
        "war OR missile OR sanctions OR ceasefire OR strike Iran OR Russia OR China",
        "geopolitics",
    ),
    {
        "name": "Investing.com Commodities",
        "url": "https://www.investing.com/rss/news_301.rss",
        "category": "gold_commodities",
        "max_age_days": 3,
        "require_any": ("gold", "silver", "metal", "bullion", "xau"),
    },
    {
        "name": "Investing.com Economy",
        "url": "https://www.investing.com/rss/news_14.rss",
        "category": "economics",
        "max_age_days": 3,
    },
    {
        "name": "Federal Reserve: monetary policy",
        "url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "category": "fed_policy",
        "max_age_days": 21,
    },
    {
        "name": "Federal Reserve: speeches",
        "url": "https://www.federalreserve.gov/feeds/speeches.xml",
        "category": "fed_policy",
        "max_age_days": 7,
        "title_prefix": "Fed speech: ",
    },
    {
        "name": "CNBC Economy",
        "url": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "category": "economics",
        "max_age_days": 3,
    },
    {
        "name": "CNBC Politics",
        "url": "https://www.cnbc.com/id/10000113/device/rss/rss.html",
        "category": "politics",
        "max_age_days": 3,
    },
    {
        "name": "BBC World",
        "url": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "category": "geopolitics",
        "max_age_days": 2,
    },
    {
        "name": "ForexLive",
        "url": "https://www.forexlive.com/feed/news/",
        "category": "fx_rates_macro",
        "max_age_days": 3,
    },
    {
        "name": "Forex Factory Calendar",
        "url": "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
        "category": "economic_calendar",
        "kind": "forex_factory_calendar",
        "max_age_days": 7,
        # The full calendar has its own prompt block. In the headline mix a few
        # entries are enough, otherwise scheduled events crowd out real news.
        "max_in_mix": 6,
    },
]

LAST_FEED_HEALTH: list[dict] = []


RELEVANCE_TERMS = {
    "gold": 5,
    "xau": 5,
    "xauusd": 6,
    "comex": 4,
    "futures": 2,
    "usd": 3,
    "fed": 4,
    "federal reserve": 5,
    "powell": 5,
    "fomc": 5,
    "rate cut": 5,
    "rate hike": 5,
    "inflation": 4,
    "cpi": 5,
    "pce": 5,
    "payroll": 5,
    "jobs": 3,
    "treasury": 4,
    "yield": 4,
    "dollar": 4,
    "dxy": 5,
    "tariff": 4,
    "sanction": 4,
    "war": 4,
    "ceasefire": 4,
    "risk": 2,
    "china": 2,
    "oil": 2,
    "safe haven": 5,
    "safe-haven": 5,
    "real yield": 5,
    "tips": 3,
    "rate expectations": 4,
    "jobless claims": 4,
    "unemployment": 4,
    "retail sales": 4,
    "ppi": 4,
    "gdp": 4,
    "etf": 3,
    "central bank": 3,
    "missile": 4,
    "iran": 3,
    "russia": 3,
    "ukraine": 3,
    "israel": 3,
}

# Sources whose whole purpose is gold get a bonus so they are not outscored by
# general economics headlines that only mention "risk" or "China" once.
GOLD_DESK_BONUS = 4

SURPRISE_RE = re.compile(
    r"([+-]?\d+(?:\.\d+)?)\s*(%|k|m|bn|b)?\s+vs\.?\s+([+-]?\d+(?:\.\d+)?)\s*(%|k|m|bn|b)?\s*"
    r"(?:expected|estimate|est\.?|exp\.?|consensus|forecast)",
    re.IGNORECASE,
)
# A higher number in these releases means a weaker economy.
LABOUR_WEAKNESS_TERMS = ("jobless", "claims", "unemployment")
GROWTH_OR_INFLATION_TERMS = (
    "cpi", "pce", "ppi", "inflation", "payroll", "nonfarm", "non-farm", "retail sales",
    "gdp", "ism", "pmi", "philly", "empire", "durable", "confidence", "sentiment",
    "earnings", "wages", "housing", "home sales", "industrial production",
)


def data_surprise(title: str) -> dict | None:
    """Read "actual vs expected" from a data headline.

    The gold reading is the textbook one from the research on US releases. It is
    a hint for the model, and code never turns it into a trade direction.
    """
    match = SURPRISE_RE.search(title or "")
    if not match:
        return None
    try:
        actual = float(match.group(1))
        expected = float(match.group(3))
    except ValueError:
        return None
    if actual > expected:
        side = "ABOVE"
    elif actual < expected:
        side = "BELOW"
    else:
        side = "IN_LINE"
    lowered = title.lower()
    is_us = bool(re.match(r"\s*(us|u\.s\.|united states)\b", lowered))
    read = "no textbook gold read"
    if not is_us:
        read = "non-US release; gold effect is indirect"
    elif side == "IN_LINE":
        read = "in line; little surprise for gold"
    elif any(term in lowered for term in LABOUR_WEAKNESS_TERMS):
        read = (
            "weaker labour market than expected; textbook read is supportive for gold"
            if side == "ABOVE"
            else "stronger labour market than expected; textbook read is a headwind for gold"
        )
    elif any(term in lowered for term in GROWTH_OR_INFLATION_TERMS):
        read = (
            "hotter/stronger than expected; textbook read is a headwind for gold (higher yields, firmer dollar)"
            if side == "ABOVE"
            else "cooler/weaker than expected; textbook read is supportive for gold (lower yields, softer dollar)"
        )
    return {
        "actual": actual,
        "expected": expected,
        "vs_expected": side,
        "us_release": is_us,
        "textbook_gold_read": read,
        "is_trade_direction": False,
    }


def _strip_tags(text: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _text_from_child(item: ET.Element, child_name: str) -> str:
    for child in list(item):
        local_name = child.tag.split("}", 1)[-1].lower()
        if local_name == child_name.lower():
            return _strip_tags(child.text or "")
    return ""


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _relevance_score(title: str, summary: str, category: str) -> int:
    lowered = f"{title} {summary}".lower()

    category_required_terms = {
        "fed_policy": [
            "fomc", "monetary", "powell", "rate", "inflation", "balance sheet",
            "minutes", "beige book", "economic projections", "statement",
        ],
        "gold_commodities": [
            "gold", "silver", "metals", "comex", "dollar", "fed", "yield",
            "inflation", "cpi", "pce", "oil", "war", "sanction",
        ],
        "politics": [
            "tariff", "sanction", "war", "ceasefire", "iran", "china",
            "russia", "debt", "shutdown", "treasury", "oil", "fed",
        ],
        "geopolitics": [
            "war", "missile", "strike", "attack", "sanction", "ceasefire", "nuclear",
            "iran", "israel", "russia", "ukraine", "china", "taiwan", "oil", "refiner",
            "troops", "military", "invasion",
        ],
        "economics": [
            "fed", "inflation", "cpi", "pce", "ppi", "jobs", "payroll", "unemployment",
            "claims", "gdp", "retail sales", "yield", "treasury", "dollar", "rate",
            "tariff", "recession", "consumer", "gold",
        ],
        "economic_calendar": [
            "usd", "fomc", "fed", "powell", "cpi", "pce", "nfp", "payroll",
            "jobs", "unemployment", "retail sales", "ppi", "gdp", "ism",
            "pmi", "consumer confidence",
        ],
    }
    required_terms = category_required_terms.get(category)
    if required_terms and not any(term in lowered for term in required_terms):
        return 0

    score = sum(weight for term, weight in RELEVANCE_TERMS.items() if term in lowered)
    if score > 0 and category in {"gold_commodities", "fed_policy", "fx_rates_macro"}:
        score += 3
    if score > 0 and category == "gold_commodities" and any(
        term in lowered for term in ("gold", "xau", "bullion")
    ):
        score += GOLD_DESK_BONUS
    return score


def _fetch_rss_feed(feed: dict, per_feed_limit: int) -> list[dict]:
    response = requests.get(feed["url"], headers=REQUEST_HEADERS, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = [
        elem
        for elem in root.iter()
        if elem.tag.split("}", 1)[-1].lower() in {"item", "entry"}
    ]

    articles = []
    # Scan past the first few items: relevance filtering drops most of a general feed.
    for item in items[: per_feed_limit * 5]:
        if len(articles) >= per_feed_limit:
            break
        title = _text_from_child(item, "title")
        publisher = ""
        if feed.get("kind") == "google_news":
            publisher = _text_from_child(item, "source")
            if publisher and title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3].strip()
        link = _text_from_child(item, "link")
        if not link:
            for child in list(item):
                if child.tag.split("}", 1)[-1].lower() == "link":
                    link = child.attrib.get("href", "")
                    break
        published_raw = (
            _text_from_child(item, "pubDate")
            or _text_from_child(item, "published")
            or _text_from_child(item, "updated")
        )
        published_at = _parse_datetime(published_raw)
        if published_at:
            max_age_days = feed.get("max_age_days", 7)
            age = datetime.now(timezone.utc) - published_at.astimezone(timezone.utc)
            if age.days > max_age_days:
                continue
        summary = (
            _text_from_child(item, "description")
            or _text_from_child(item, "summary")
            or _text_from_child(item, "content")
        )
        if not title:
            continue

        if feed.get("kind") == "google_news":
            # Google's summary is just the headline again as HTML links.
            summary = ""
        if feed.get("title_prefix"):
            title = f"{feed['title_prefix']}{title}"
        require_any = feed.get("require_any")
        if require_any and not any(term in f"{title} {summary}".lower() for term in require_any):
            continue
        score = _relevance_score(title, summary, feed["category"])
        if score <= 0:
            continue

        surprise = data_surprise(title)
        description = summary[:500]
        if surprise:
            score += 6 if surprise["us_release"] else 1
            description = (
                f"DATA SURPRISE: actual {surprise['actual']:g} vs expected {surprise['expected']:g} "
                f"({surprise['vs_expected']}). {surprise['textbook_gold_read']}. "
                + description
            ).strip()

        article = {
            "title": title,
            "description": description,
            "source": f"{feed['name']} / {publisher}" if publisher else feed["name"],
            "category": feed["category"],
            "published_at": published_at.isoformat() if published_at else published_raw,
            "url": link,
            "relevance_score": score,
            "feed": feed["name"],
        }
        if surprise:
            article["data_surprise"] = surprise
        articles.append(article)

    return articles


def search_google_news(queries: list[str], per_query: int = 4) -> list[dict]:
    """Run the scout's own search queries against Google News (last 24 hours)."""
    found: list[dict] = []
    for raw in queries:
        query = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", raw or "")
        query = " ".join(query.split())
        if not query:
            continue
        feed = google_news_feed(f"Google News search: {query[:60]}", query, "gold_commodities")
        try:
            items = _fetch_rss_feed(feed, per_query)
        except Exception as exc:
            found.append({
                "title": f"Google News search failed for query: {query}",
                "description": str(exc),
                "source": "Google News",
                "category": "search",
                "published_at": datetime.now(timezone.utc).isoformat(),
                "url": feed["url"],
                "relevance_score": 0,
                "fetch_error": True,
                "query": raw,
            })
            continue
        for item in items:
            item["query"] = raw
            found.append(item)
    return found


def _load_ff_calendar_xml(url: str) -> bytes:
    """Fetch FF this-week XML with a local cache to avoid 429 rate limits."""
    if FF_CALENDAR_CACHE_PATH.exists():
        age = time.time() - FF_CALENDAR_CACHE_PATH.stat().st_mtime
        if age <= FF_CALENDAR_CACHE_MAX_AGE_SEC:
            return FF_CALENDAR_CACHE_PATH.read_bytes()

    response = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
    if response.status_code == 429 and FF_CALENDAR_CACHE_PATH.exists():
        return FF_CALENDAR_CACHE_PATH.read_bytes()
    response.raise_for_status()
    FF_CALENDAR_CACHE_PATH.write_bytes(response.content)
    return response.content


def _fetch_forex_factory_calendar(feed: dict, per_feed_limit: int) -> list[dict]:
    """
    Parse the full this-week FF calendar XML.

    Important: do NOT truncate to early-week events only. NFP/CPI often land later
    in the chronologically ordered feed; scanning only the first N events drops them
    and the model then invents "timing UNKNOWN".
    """
    content = _load_ff_calendar_xml(feed["url"])
    root = ET.fromstring(content)
    events = [
        elem
        for elem in root.iter()
        if elem.tag.split("}", 1)[-1].lower() == "event"
    ]

    articles = []
    for event in events:
        title = _text_from_child(event, "title")
        country = _text_from_child(event, "country")
        impact = _text_from_child(event, "impact")
        date = _text_from_child(event, "date")
        event_time = _text_from_child(event, "time")
        forecast = _text_from_child(event, "forecast")
        previous = _text_from_child(event, "previous")

        if not title:
            continue

        impact_lower = impact.lower()
        event_text = f"{country} {title} {impact}".lower()
        is_usd = country.upper() == "USD" or "usd" in event_text
        is_high_value = impact_lower in {"high", "medium"}
        relevance = _relevance_score(f"{country} {title}", impact, feed["category"])
        if not is_usd and relevance <= 0:
            continue
        if not is_high_value and relevance < 5:
            continue

        score = relevance
        if impact_lower == "high":
            score += 6
        elif impact_lower == "medium":
            score += 3
        # Prefer USD high-impact and payroll/CPI-class events in ranking.
        if is_usd and impact_lower == "high":
            score += 8
        if any(k in title.lower() for k in (
            "non-farm", "nonfarm", "payroll", "unemployment", "cpi", "pce",
            "fomc", "ism", "gdp", "retail sales",
        )):
            score += 5

        articles.append({
            "title": f"Forex Factory {impact or 'scheduled'} impact: {country} {title}",
            "description": (
                f"Calendar event for {date} {event_time}. "
                f"Forecast: {forecast or 'n/a'}; Previous: {previous or 'n/a'}."
            ),
            "source": feed["name"],
            "category": feed["category"],
            "published_at": f"{date} {event_time}".strip(),
            "url": "https://www.forexfactory.com/calendar",
            "relevance_score": score,
            "calendar_country": country.upper(),
            "calendar_impact": impact_lower,
            "calendar_date": date,
            "calendar_time": event_time,
            "calendar_title": title,
            "calendar_forecast": forecast,
            "calendar_previous": previous,
        })

    # Sort: USD High first, then score, then chronological.
    impact_rank = {"high": 0, "medium": 1, "low": 2}
    articles.sort(
        key=lambda item: (
            0 if item.get("calendar_country") == "USD" else 1,
            impact_rank.get(item.get("calendar_impact", ""), 9),
            -(item.get("relevance_score") or 0),
            str(item.get("calendar_date") or ""),
            str(item.get("calendar_time") or ""),
        )
    )
    # Keep a larger calendar slice so later-week NFP/CPI survive the digest.
    return articles[: max(per_feed_limit * 3, 24)]


def fetch_usd_high_impact_schedule(max_events: int = 20) -> dict:
    """
    Structured USD high/medium calendar for prompt gating.

    Returns dated events with FF times so the model must not invent UNKNOWN
    when the schedule is already published.
    """
    feed = next(
        (item for item in RSS_FEEDS if item.get("kind") == "forex_factory_calendar"),
        None,
    )
    if not feed:
        return {
            "status": "missing_feed",
            "events": [],
            "label_for_prompt": "USD high-impact calendar: unavailable",
        }

    try:
        articles = _fetch_forex_factory_calendar(feed, per_feed_limit=12)
    except Exception as exc:
        fallback = _fallback_usd_high_impact_schedule()
        fallback["error"] = str(exc)
        return fallback

    events = []
    for article in articles:
        if article.get("calendar_country") != "USD":
            continue
        if article.get("calendar_impact") not in {"high", "medium"}:
            continue
        events.append({
            "date": article.get("calendar_date"),
            "time": article.get("calendar_time"),
            "impact": article.get("calendar_impact"),
            "title": article.get("calendar_title"),
            "forecast": article.get("calendar_forecast"),
            "previous": article.get("calendar_previous"),
            "published_at": article.get("published_at"),
            "source": "Forex Factory Calendar (nfs.faireconomy.media)",
        })

    # Chronological for gating windows.
    events.sort(key=lambda item: (str(item.get("date") or ""), str(item.get("time") or "")))
    events = events[:max_events]

    high = [e for e in events if e.get("impact") == "high"]
    nfp_like = [
        e for e in events
        if any(k in str(e.get("title", "")).lower() for k in (
            "non-farm", "nonfarm", "payroll", "unemployment rate", "average hourly",
        ))
    ]
    label_bits = []
    for event in high[:8]:
        label_bits.append(
            f"{event.get('date')} {event.get('time')} {event.get('title')} "
            f"(forecast={event.get('forecast') or 'n/a'})"
        )
    label = (
        "USD high-impact calendar (FF this week): " + "; ".join(label_bits)
        if label_bits
        else "USD high-impact calendar: no High-impact USD events in feed"
    )

    return {
        "status": "ok",
        "source": feed["url"],
        "events": events,
        "high_impact_count": len(high),
        "nfp_related": nfp_like,
        "label_for_prompt": label,
    }


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _balanced_mix(
    by_feed: dict[str, list[dict]],
    max_total: int,
    per_feed_floor: int = 2,
    floor_min_score: int = 8,
) -> list[dict]:
    """Every working feed gets a couple of slots first, then the rest go by score.

    Without this, one high-scoring source (usually the calendar) fills the list.
    """
    caps = {feed["name"]: feed.get("max_in_mix") for feed in RSS_FEEDS}
    ranked = {
        name: sorted(items, key=lambda a: -(a.get("relevance_score") or 0))
        for name, items in by_feed.items()
    }
    chosen: list[dict] = []
    taken: dict[str, int] = {}
    rest: list[tuple[str, dict]] = []
    for name, items in ranked.items():
        for index, article in enumerate(items):
            guaranteed = index < per_feed_floor and (article.get("relevance_score") or 0) >= floor_min_score
            if guaranteed:
                chosen.append(article)
                taken[name] = taken.get(name, 0) + 1
            else:
                rest.append((name, article))
    rest.sort(key=lambda pair: -(pair[1].get("relevance_score") or 0))
    for name, article in rest:
        if len(chosen) >= max_total:
            break
        cap = caps.get(name)
        if cap is not None and taken.get(name, 0) >= cap:
            continue
        chosen.append(article)
        taken[name] = taken.get(name, 0) + 1
    chosen.sort(key=lambda a: -(a.get("relevance_score") or 0))
    return chosen[:max_total]


def fetch_all(max_total: int = 40, per_feed_limit: int = 8) -> list[dict]:
    """Fetch public macro/gold headlines from several non-GNews sources."""
    errors: list[dict] = []
    by_feed: dict[str, list[dict]] = {}
    seen: set[str] = set()
    health: list[dict] = []

    for feed in RSS_FEEDS:
        try:
            if feed.get("kind") == "forex_factory_calendar":
                feed_articles = _fetch_forex_factory_calendar(feed, per_feed_limit)
            else:
                feed_articles = _fetch_rss_feed(feed, per_feed_limit)
        except Exception as exc:
            health.append({"feed": feed["name"], "ok": False, "kept": 0, "error": str(exc)[:200]})
            errors.append({
                "title": f"{feed['name']} feed failed",
                "description": str(exc),
                "source": feed["name"],
                "category": feed["category"],
                "published_at": datetime.now(timezone.utc).isoformat(),
                "url": feed["url"],
                "relevance_score": 0,
                "fetch_error": True,
            })
            continue

        kept = []
        for article in feed_articles:
            key = _title_key(article["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(article)
        by_feed[feed["name"]] = kept
        health.append({"feed": feed["name"], "ok": True, "kept": len(kept), "error": None})

    LAST_FEED_HEALTH[:] = health
    articles = _balanced_mix(by_feed, max_total) + errors
    from macro_relevance import canonicalize_articles

    received = datetime.now(timezone.utc).isoformat()
    return canonicalize_articles(articles, received)


def feed_health_text() -> str:
    """One line per feed so the model knows which channels were silent this cycle."""
    if not LAST_FEED_HEALTH:
        return "(feed health not recorded)"
    lines = []
    for row in LAST_FEED_HEALTH:
        if row["ok"]:
            lines.append(f"- {row['feed']}: ok, {row['kept']} relevant")
        else:
            lines.append(f"- {row['feed']}: FAILED ({row['error']})")
    return "\n".join(lines)


def format_for_claude(articles, max_articles=20):
    """Format articles for Claude prompt."""
    if not articles:
        return "(Additional public macro/news feeds returned no relevant articles.)"

    formatted = []
    for article in articles[:max_articles]:
        if not isinstance(article, dict):
            formatted.append(f"- {article}")
            continue

        error_marker = " [FETCH ERROR]" if article.get("fetch_error") else ""
        formatted.append(
            f"- {article.get('title', '')}{error_marker}\n"
            f"  Source: {article.get('source', 'unknown')} / {article.get('category', 'n/a')}\n"
            f"  Published: {article.get('published_at', 'n/a')}\n"
            f"  Relevance score: {article.get('relevance_score', 'n/a')}\n"
            f"  Summary: {article.get('description', '')}\n"
            f"  URL: {article.get('url', '')}"
        )

    return "\n".join(formatted) if formatted else "(No additional articles)"


if __name__ == "__main__":
    articles = fetch_all()
    print("FEED HEALTH")
    print(feed_health_text())
    print()
    print(format_for_claude(articles, max_articles=40))
