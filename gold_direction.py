"""
Gold direction dashboard: the drivers that usually move gold, in one block.

Each item carries the textbook reading for gold. Those readings are context for
the model. Code never adds them up into a direction or a trade.

Sources (all free, no key):
- Yahoo chart API: live 10Y yield, VIX, S&P futures, oil, USD/JPY, EUR/USD,
  silver, copper, GLD, and Fed funds futures
- FRED CSV: effective fed funds rate
- CFTC public reporting: weekly managed-money positioning in COMEX gold
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from fetch_market_data import _yahoo_chart

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
CFTC_DISAGGREGATED = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
CFTC_GOLD_CODE = "088691"
ZQ_MONTH_CODES = "FGHJKMNQUVXZ"

# symbol, label, unit, threshold for a move that matters, how a rise usually reads for gold
CROSS_ASSETS = (
    ("^TNX", "US 10Y yield (live)", "bp", 2.0, "headwind"),
    ("^VIX", "VIX fear index", "pct", 5.0, "supportive"),
    ("ES=F", "S&P 500 futures", "pct", 0.5, "mild headwind (risk-on)"),
    ("CL=F", "WTI crude oil", "pct", 1.5, "two-way (inflation lifts yields; supply shock adds haven demand)"),
    ("USDJPY=X", "USD/JPY", "pct", 0.3, "headwind (firmer dollar)"),
    ("EURUSD=X", "EUR/USD", "pct", 0.2, "supportive (softer dollar)"),
    ("SI=F", "Silver futures", "pct", 1.0, "confirms a metals bid"),
    ("HG=F", "Copper futures", "pct", 1.0, "growth signal; mixed for gold"),
)

_OPPOSITE = {
    "headwind": "supportive",
    "supportive": "headwind",
    "mild headwind (risk-on)": "mildly supportive (risk-off)",
    "headwind (firmer dollar)": "supportive (softer dollar)",
    "supportive (softer dollar)": "headwind (firmer dollar)",
    "confirms a metals bid": "confirms metals selling",
}


def _closes(chart: dict) -> list[float]:
    quote = (chart.get("indicators", {}).get("quote") or [{}])[0]
    return [float(x) for x in (quote.get("close") or []) if x is not None]


def _read_move(change: float, threshold: float, rise_reading: str) -> str:
    if abs(change) < threshold:
        return "small move; little signal"
    if change > 0:
        return rise_reading
    return _OPPOSITE.get(rise_reading, f"opposite of: {rise_reading}")


def cross_asset_snapshot() -> list[dict[str, Any]]:
    rows = []
    for symbol, label, unit, threshold, rise_reading in CROSS_ASSETS:
        # range=1d: Yahoo's chartPreviousClose is the prior session close only for a 1-day window.
        chart = _yahoo_chart(symbol, "15m", "1d")
        if not chart:
            rows.append({"symbol": symbol, "label": label, "available": False})
            continue
        meta = chart.get("meta") or {}
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose")
        closes = _closes(chart)
        hour_ago = closes[-5] if len(closes) >= 5 else None
        if last is None or prev in (None, 0):
            rows.append({"symbol": symbol, "label": label, "available": False})
            continue
        if unit == "bp":
            day = (last - prev) * 100
            hour = (last - hour_ago) * 100 if hour_ago else None
        else:
            day = (last - prev) / prev * 100
            hour = (last - hour_ago) / hour_ago * 100 if hour_ago else None
        stamp = meta.get("regularMarketTime")
        rows.append({
            "symbol": symbol,
            "label": label,
            "available": True,
            "last": round(float(last), 4),
            "unit": unit,
            "day_change": round(day, 2),
            "last_hour_change": None if hour is None else round(hour, 2),
            "as_of": datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp else None,
            "gold_read_today": _read_move(day, threshold, rise_reading),
            "gold_read_last_hour": None if hour is None else _read_move(hour, threshold / 2, rise_reading),
        })
    return rows


def _fred_latest(series: str) -> tuple[str, float] | None:
    response = requests.get(FRED_CSV, params={"id": series}, timeout=20)
    response.raise_for_status()
    for line in reversed(response.text.strip().splitlines()):
        stamp, _, value = line.partition(",")
        try:
            return stamp, float(value)
        except ValueError:
            continue
    return None


def _zq_symbol(months_ahead: int, now: datetime) -> str:
    index = now.month - 1 + months_ahead
    year = now.year + index // 12
    return f"ZQ{ZQ_MONTH_CODES[index % 12]}{year % 100:02d}.CBT"


def rate_expectations(now: datetime | None = None) -> dict[str, Any]:
    """What Fed funds futures say the policy rate will average a few months out."""
    now = now or datetime.now(timezone.utc)
    effective = _fred_latest("DFF")
    out: dict[str, Any] = {
        "effective_fed_funds": effective[1] if effective else None,
        "effective_as_of": effective[0] if effective else None,
        "contracts": [],
    }
    for months in (1, 3, 6):
        symbol = _zq_symbol(months, now)
        chart = _yahoo_chart(symbol, "15m", "1d")
        meta = (chart or {}).get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose")
        if not price:
            continue
        implied = 100 - float(price)
        row = {
            "symbol": symbol,
            "months_ahead": months,
            "implied_rate": round(implied, 3),
            "implied_change_today_bp": round((float(prev) - float(price)) * 100, 1) if prev else None,
        }
        if effective:
            gap = (implied - effective[1]) * 100
            row["priced_change_vs_today_bp"] = round(gap, 1)
            if gap >= 10:
                row["gold_read"] = "market prices hikes; headwind for gold"
            elif gap <= -10:
                row["gold_read"] = "market prices cuts; supportive for gold"
            else:
                row["gold_read"] = "little change priced"
        today = row.get("implied_change_today_bp")
        if today is not None and abs(today) >= 2:
            row["today_repricing"] = (
                "hawkish repricing today; headwind for gold" if today > 0
                else "dovish repricing today; supportive for gold"
            )
        out["contracts"].append(row)
    return out


def cot_positioning() -> dict[str, Any]:
    """Weekly CFTC managed-money positioning in COMEX gold. Context, not timing."""
    response = requests.get(
        CFTC_DISAGGREGATED,
        params={
            "cftc_contract_market_code": CFTC_GOLD_CODE,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 1,
        },
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return {"available": False}
    row = rows[0]
    long_ = float(row.get("m_money_positions_long_all") or 0)
    short = float(row.get("m_money_positions_short_all") or 0)
    d_long = float(row.get("change_in_m_money_long_all") or 0)
    d_short = float(row.get("change_in_m_money_short_all") or 0)
    net_change = d_long - d_short
    return {
        "available": True,
        "report_date": str(row.get("report_date_as_yyyy_mm_dd", ""))[:10],
        "managed_money_long": int(long_),
        "managed_money_short": int(short),
        "managed_money_net": int(long_ - short),
        "net_change_week": int(net_change),
        "long_pct_open_interest": row.get("pct_of_oi_m_money_long_all"),
        "gold_read": (
            "funds added to net longs last week (supportive flow)" if net_change > 0
            else "funds cut net longs last week (selling pressure)" if net_change < 0
            else "no change in fund positioning"
        ),
        "note": "Weekly, as of Tuesday. Background for crowding and flow, not an entry trigger.",
    }


def gld_flow_proxy() -> dict[str, Any]:
    """GLD volume against its 20-day average, a rough read on fund flows."""
    chart = _yahoo_chart("GLD", "1d", "2mo")
    if not chart:
        return {"available": False}
    quote = (chart.get("indicators", {}).get("quote") or [{}])[0]
    volumes = [v for v in (quote.get("volume") or []) if v]
    closes = [c for c in (quote.get("close") or []) if c]
    if len(volumes) < 21 or len(closes) < 2:
        return {"available": False}
    average = sum(volumes[-21:-1]) / 20
    ratio = volumes[-1] / average if average else None
    change = (closes[-1] - closes[-2]) / closes[-2] * 100
    heavy = ratio is not None and ratio >= 1.5
    return {
        "available": True,
        "last_volume_vs_20d": None if ratio is None else round(ratio, 2),
        "last_day_change_pct": round(change, 2),
        "gold_read": (
            ("heavy buying in the gold ETF" if change > 0 else "heavy selling in the gold ETF")
            if heavy else "normal ETF activity"
        ),
    }


def build_direction_dashboard() -> dict[str, Any]:
    dashboard: dict[str, Any] = {"built_at": datetime.now(timezone.utc).isoformat()}
    for key, builder in (
        ("cross_assets", cross_asset_snapshot),
        ("rate_expectations", rate_expectations),
        ("cot_positioning", cot_positioning),
        ("gld_flow", gld_flow_proxy),
    ):
        try:
            dashboard[key] = builder()
        except Exception as exc:
            dashboard[key] = {"available": False, "error": str(exc)[:200]}
    return dashboard


def format_dashboard(dashboard: dict[str, Any] | None) -> str:
    if not dashboard:
        return "(Gold direction dashboard not built this cycle.)"
    lines = [
        "Textbook readings are context. A reading is not a trade direction and these rows are not votes.",
        "",
        "CROSS-ASSET (day change / last hour):",
    ]
    for row in dashboard.get("cross_assets") or []:
        if not isinstance(row, dict) or not row.get("available"):
            lines.append(f"- {row.get('label', row.get('symbol'))}: unavailable")
            continue
        unit = "bp" if row["unit"] == "bp" else "%"
        hour = row.get("last_hour_change")
        lines.append(
            f"- {row['label']}: {row['last']} | day {row['day_change']:+}{unit} -> {row['gold_read_today']}"
            + (f" | last hour {hour:+}{unit} -> {row['gold_read_last_hour']}" if hour is not None else "")
        )

    rates = dashboard.get("rate_expectations") or {}
    lines += ["", "FED RATE EXPECTATIONS (Fed funds futures):"]
    if rates.get("effective_fed_funds") is not None:
        lines.append(f"- Effective fed funds today: {rates['effective_fed_funds']}% (as of {rates.get('effective_as_of')})")
    for row in rates.get("contracts") or []:
        text = f"- {row['months_ahead']}m out ({row['symbol']}): implied {row['implied_rate']}%"
        if row.get("priced_change_vs_today_bp") is not None:
            text += f", {row['priced_change_vs_today_bp']:+}bp vs today -> {row.get('gold_read')}"
        if row.get("today_repricing"):
            text += f"; {row['today_repricing']}"
        lines.append(text)
    if rates.get("error"):
        lines.append(f"- unavailable ({rates['error']})")

    cot = dashboard.get("cot_positioning") or {}
    lines += ["", "FUND POSITIONING (CFTC, weekly):"]
    if cot.get("available"):
        lines.append(
            f"- Managed money net {cot['managed_money_net']:+,} contracts (week {cot['net_change_week']:+,}), "
            f"report {cot['report_date']} -> {cot['gold_read']}. {cot['note']}"
        )
    else:
        lines.append(f"- unavailable{(' (' + cot['error'] + ')') if cot.get('error') else ''}")

    gld = dashboard.get("gld_flow") or {}
    lines += ["", "GOLD ETF ACTIVITY (GLD):"]
    if gld.get("available"):
        lines.append(
            f"- Volume {gld['last_volume_vs_20d']}x its 20-day average, price {gld['last_day_change_pct']:+}% -> {gld['gold_read']}"
        )
    else:
        lines.append("- unavailable")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_dashboard(build_direction_dashboard()))
