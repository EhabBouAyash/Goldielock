"""
Additional public macro/news feeds for the intraday gold agent.

The main agent already uses GNews. These feeds give Claude a second channel for
gold, rates, Fed, politics, and cross-asset headlines when GNews is rate-limited.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
import xml.etree.ElementTree as ET

import requests


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


RSS_FEEDS = [
    {
        "name": "Investing.com Commodities",
        "url": "https://www.investing.com/rss/news_301.rss",
        "category": "gold_commodities",
        "max_age_days": 7,
    },
    {
        "name": "Investing.com Economy",
        "url": "https://www.investing.com/rss/news_14.rss",
        "category": "economics",
        "max_age_days": 7,
    },
    {
        "name": "Federal Reserve",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "category": "fed_policy",
        "max_age_days": 21,
    },
    {
        "name": "CNBC Economy",
        "url": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "category": "economics",
        "max_age_days": 7,
    },
    {
        "name": "CNBC Politics",
        "url": "https://www.cnbc.com/id/10000113/device/rss/rss.html",
        "category": "politics",
        "max_age_days": 7,
    },
    {
        "name": "MarketWatch MarketPulse",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
        "category": "markets",
        "max_age_days": 7,
    },
    {
        "name": "ForexLive",
        "url": "https://www.forexlive.com/feed/news/",
        "category": "fx_rates_macro",
        "max_age_days": 7,
    },
    {
        "name": "Forex Factory Calendar",
        "url": "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
        "category": "economic_calendar",
        "kind": "forex_factory_calendar",
        "max_age_days": 7,
    },
]


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
    for item in items[:per_feed_limit]:
        title = _text_from_child(item, "title")
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

        score = _relevance_score(title, summary, feed["category"])
        if score <= 0:
            continue

        articles.append({
            "title": title,
            "description": summary[:500],
            "source": feed["name"],
            "category": feed["category"],
            "published_at": published_at.isoformat() if published_at else published_raw,
            "url": link,
            "relevance_score": score,
        })

    return articles


def _fetch_forex_factory_calendar(feed: dict, per_feed_limit: int) -> list[dict]:
    response = requests.get(feed["url"], headers=REQUEST_HEADERS, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    events = [
        elem
        for elem in root.iter()
        if elem.tag.split("}", 1)[-1].lower() == "event"
    ]

    articles = []
    for event in events[: per_feed_limit * 4]:
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
        })

    return articles[:per_feed_limit]


def fetch_all(max_total: int = 30, per_feed_limit: int = 8) -> list[dict]:
    """Fetch public macro/gold headlines from several non-GNews sources."""
    articles: list[dict] = []
    seen: set[str] = set()

    for feed in RSS_FEEDS:
        try:
            if feed.get("kind") == "forex_factory_calendar":
                feed_articles = _fetch_forex_factory_calendar(feed, per_feed_limit)
            else:
                feed_articles = _fetch_rss_feed(feed, per_feed_limit)
        except Exception as exc:
            articles.append({
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

        for article in feed_articles:
            key = re.sub(r"[^a-z0-9]+", " ", article["title"].lower()).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            articles.append(article)

    articles.sort(
        key=lambda item: (
            item.get("fetch_error") is True,
            -(item.get("relevance_score") or 0),
            str(item.get("published_at") or ""),
        )
    )
    return articles[:max_total]


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
    print(format_for_claude(articles))
