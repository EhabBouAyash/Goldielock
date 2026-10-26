"""
Paper trades for the gold autopilot. Nothing here talks to a broker.

A LONG or SHORT screen signal becomes one simulated trade:
- it fills when the live spot price is inside the model's entry zone before the
  card expires (a MARKET_NOW card fills at once if price is already in the zone);
- it stays open until the spot price reaches the stop or the target;
- the result is written to trade_backlog.csv as a PAPER_### row.

Prices come from gold-api.com spot, polled every few seconds. A wick that touches
a level between two polls can be missed. Exits are recorded conservatively: a stop
fills at the stop or worse, a target fills at the target and never better.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from prop_account_tracker import TRADE_BACKLOG_COLUMNS, PROP_ACCOUNT_SIZE
from trade_validation import DEFAULT_COST_PRICE

ROOT = Path(__file__).resolve().parent
BACKLOG_CSV = ROOT / "trade_backlog.csv"
PAPER_PREFIX = "PAPER_"
GOLD_API_URL = "https://api.gold-api.com/price/XAU"
MAX_PRICE_AGE_SECONDS = 120


def paper_risk_usd() -> float:
    try:
        return float(os.getenv("GOLD_PAPER_RISK_USD") or 125.0)
    except ValueError:
        return 125.0


def spot_price(now: datetime | None = None) -> dict[str, Any] | None:
    """Live XAUUSD spot from gold-api. None if missing or stale."""
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    now = now or datetime.now(timezone.utc)
    try:
        response = requests.get(GOLD_API_URL, timeout=15, verify=False)
        data = response.json() if response.status_code == 200 else {}
    except Exception:
        return None
    price = data.get("price")
    if not price:
        return None
    observed = data.get("updatedAt")
    try:
        stamp = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
    except ValueError:
        stamp = now
    if (now - stamp).total_seconds() > MAX_PRICE_AGE_SECONDS:
        return None
    return {"price": round(float(price), 2), "observed_at": stamp.isoformat(), "source": "gold-api XAU spot"}


def pending_from_publication(publication: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a LONG/SHORT card into a waiting paper order."""
    proposal = publication.get("proposal") or {}
    side = proposal.get("side")
    try:
        entry_low = float(proposal.get("entry_low"))
        entry_high = float(proposal.get("entry_high"))
        stop = float(proposal.get("stop_reference"))
        target = float(proposal.get("target_reference"))
    except (TypeError, ValueError):
        return None
    if side not in {"LONG", "SHORT"} or min(entry_low, entry_high, stop, target) <= 0:
        return None
    if side == "LONG" and not stop < entry_low <= entry_high < target:
        return None
    if side == "SHORT" and not target < entry_low <= entry_high < stop:
        return None
    return {
        "side": side,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "target": target,
        "trigger": proposal.get("trigger") or "LIMIT_PULLBACK",
        "setup_family": proposal.get("setup_family"),
        "expires_at": publication.get("expires_at"),
        "plan_id": publication.get("plan_id"),
        "snapshot_id": publication.get("snapshot_id"),
        "status": publication.get("status"),
        "reason": str(proposal.get("reconciliation_summary") or "")[:400],
        "net_rr": publication.get("net_rr"),
    }


def fill_price(pending: dict[str, Any], price: float) -> float | None:
    """Where the paper order fills at this price, or None if it would not fill yet."""
    side = pending["side"]
    low, high = pending["entry_low"], pending["entry_high"]
    stop, target = pending["stop"], pending["target"]
    if low <= price <= high:
        return price
    trigger = pending.get("trigger")
    # A resting limit would already have filled if price moved through the zone
    # toward the stop. A breakout stop order fills at market once price is past it.
    if side == "LONG":
        if trigger == "LIMIT_PULLBACK" and stop < price < low:
            return price
        if trigger == "STOP_BREAKOUT" and high < price < target:
            return price
    else:
        if trigger == "LIMIT_PULLBACK" and high < price < stop:
            return price
        if trigger == "STOP_BREAKOUT" and target < price < low:
            return price
    return None


def open_trade(pending: dict[str, Any], price: float, now: datetime, trade_id: str) -> dict[str, Any]:
    distance = abs(price - pending["stop"])
    ounces = max(1.0, float(int(paper_risk_usd() / distance))) if distance > 0 else 1.0
    return {
        **pending,
        "trade_id": trade_id,
        "entry": round(price, 2),
        "ounces": ounces,
        "risk_usd": round(distance * ounces, 2),
        "opened_at": now.isoformat(),
    }


def check_exit(trade: dict[str, Any], price: float) -> tuple[str, float] | None:
    """("TP"|"SL", exit price) once a level is reached."""
    if trade["side"] == "LONG":
        if price <= trade["stop"]:
            return "SL", min(price, trade["stop"])
        if price >= trade["target"]:
            return "TP", trade["target"]
    else:
        if price >= trade["stop"]:
            return "SL", max(price, trade["stop"])
        if price <= trade["target"]:
            return "TP", trade["target"]
    return None


def _paper_rows() -> list[dict[str, str]]:
    if not BACKLOG_CSV.exists():
        return []
    reader = csv.DictReader(StringIO(BACKLOG_CSV.read_text(encoding="utf-8")))
    return [row for row in reader if str(row.get("TRADE_ID") or "").startswith(PAPER_PREFIX)]


def next_trade_id() -> str:
    numbers = []
    for row in _paper_rows():
        try:
            numbers.append(int(str(row["TRADE_ID"])[len(PAPER_PREFIX):]))
        except ValueError:
            continue
    return f"{PAPER_PREFIX}{(max(numbers) + 1) if numbers else 1:03d}"


def _paper_cumulative() -> float:
    total = 0.0
    for row in _paper_rows():
        try:
            total += float(str(row.get("PNL_USD") or "0").replace("$", "").replace(",", ""))
        except ValueError:
            continue
    return total


def _append_backlog_row(row: dict[str, Any]) -> None:
    existing = BACKLOG_CSV.read_text(encoding="utf-8") if BACKLOG_CSV.exists() else ""
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if not existing.strip():
        writer.writerow(TRADE_BACKLOG_COLUMNS)
    writer.writerow([row.get(column, "") for column in TRADE_BACKLOG_COLUMNS])
    prefix = existing if (not existing or existing.endswith("\n")) else existing + "\n"
    BACKLOG_CSV.write_text(prefix + buffer.getvalue(), encoding="utf-8")


def _note(text: str) -> str:
    return " ".join(str(text).replace(",", ";").split())


def close_trade(trade: dict[str, Any], reason: str, exit_price: float, now: datetime) -> dict[str, Any]:
    direction = 1 if trade["side"] == "LONG" else -1
    ounces = trade["ounces"]
    gross = (exit_price - trade["entry"]) * direction * ounces
    pnl = round(gross - DEFAULT_COST_PRICE * ounces, 2)
    risk = trade["risk_usd"] or 1.0
    cumulative = round(_paper_cumulative() + pnl, 2)
    outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"
    notional = trade["entry"] * ounces
    row = {
        "DATE": datetime.fromisoformat(trade["opened_at"]).astimezone().date().isoformat(),
        "TRADE_ID": trade["trade_id"],
        "SIGNAL": trade["side"],
        "ENTRY": f"{trade['entry']:.2f}",
        "STOP_LOSS": f"{trade['stop']:.2f}",
        "TAKE_PROFIT": f"{trade['target']:.2f}",
        "OUTCOME": outcome,
        "R_MULTIPLE": f"{pnl / risk:.2f}",
        "PNL_USD": f"{pnl:.2f}",
        "RISK_USD": f"{trade['risk_usd']:.2f}",
        "XAU_OZ": f"{ounces:.0f}",
        "TV_LEVERAGE": "",
        "EFFECTIVE_LEVERAGE": f"{notional / PROP_ACCOUNT_SIZE:.2f}",
        "CUMULATIVE_PNL": f"{cumulative:.2f}",
        "EQUITY": f"{PROP_ACCOUNT_SIZE + cumulative:.2f}",
        "DAILY_PNL": "",
        "NOTES": _note(
            f"PAPER autopilot (not a real fill) | {trade.get('setup_family') or 'setup n/a'} "
            f"{trade.get('trigger')} zone {trade['entry_low']:.2f}-{trade['entry_high']:.2f} | "
            f"opened {trade['opened_at'][:16]}Z closed {now.isoformat()[:16]}Z | "
            f"exit {reason} at {exit_price:.2f} | plan {trade.get('snapshot_id')} | "
            f"paper equity starts at {PROP_ACCOUNT_SIZE} | why: {trade.get('reason') or 'n/a'}"
        ),
    }
    _append_backlog_row(row)
    return row


def record_missed(pending: dict[str, Any], now: datetime, last_price: float | None) -> dict[str, Any]:
    row = {column: "" for column in TRADE_BACKLOG_COLUMNS}
    row.update({
        "DATE": now.astimezone().date().isoformat(),
        "TRADE_ID": next_trade_id(),
        "SIGNAL": pending["side"],
        "ENTRY": f"{pending['entry_low']:.2f}-{pending['entry_high']:.2f}",
        "STOP_LOSS": f"{pending['stop']:.2f}",
        "TAKE_PROFIT": f"{pending['target']:.2f}",
        "OUTCOME": "MISSED_UNFILLED",
        "NOTES": _note(
            f"PAPER autopilot | price never reached the {pending.get('trigger')} zone before the card "
            f"expired at {pending.get('expires_at')} | last spot {last_price} | plan {pending.get('snapshot_id')}"
        ),
    })
    _append_backlog_row(row)
    return row


def paper_record_text(max_rows: int = 6) -> str:
    rows = _paper_rows()
    if not rows:
        return "(No paper trades yet.)"
    closed = [r for r in rows if r.get("OUTCOME") in {"WIN", "LOSS", "BREAKEVEN"}]
    wins = sum(1 for r in closed if r.get("OUTCOME") == "WIN")
    missed = sum(1 for r in rows if r.get("OUTCOME") == "MISSED_UNFILLED")
    lines = [
        f"Closed paper trades: {len(closed)} | wins {wins} | losses {len(closed) - wins} | "
        f"missed (never filled) {missed} | paper P&L ${_paper_cumulative():.2f}",
        "Learn from these like the manual journal: entries that ran straight to the stop were usually late.",
    ]
    for row in rows[-max_rows:]:
        lines.append(
            f"- {row.get('TRADE_ID')} {row.get('DATE')} {row.get('SIGNAL')} entry {row.get('ENTRY')} "
            f"SL {row.get('STOP_LOSS')} TP {row.get('TAKE_PROFIT')} -> {row.get('OUTCOME')} "
            f"{row.get('PNL_USD') or ''} ({row.get('R_MULTIPLE') or 'n/a'}R)"
        )
    return "\n".join(lines)
