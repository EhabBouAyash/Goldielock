"""
FundedNext Stellar 1-Step prop account tracking from trade_backlog.txt.

Loads manual trade rows, backfills PnL from notes when missing, computes running
account columns, aggregates snapshot metrics for the agent prompt, and writes
the enriched CSV back to disk on each refresh.
"""

from __future__ import annotations

import csv
import re
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any

PROP_ACCOUNT_SIZE = 25_000
PROP_DAILY_LOSS_SOFT_USD = 625
PROP_DAILY_LOSS_HARD_USD = 750
PROP_MAX_LOSS_SOFT_USD = 1_250
PROP_MAX_LOSS_HARD_USD = 1_500
PROP_EQUITY_FLOOR_USD = 23_500
PROP_EQUITY_SOFT_FLOOR_USD = 23_750
PROP_PROFIT_TARGET_USD = 2_500
PROP_MIN_TRADING_DAYS = 2
PROP_MAX_SINGLE_TRADE_RISK_USD = 250
PROP_PREFERRED_TV_LEVERAGE = 50

LEGACY_TRADE_COLUMNS = (
    "DATE",
    "TRADE_ID",
    "SIGNAL",
    "ENTRY",
    "TAKE_PROFIT",
    "OUTCOME",
    "R_MULTIPLE",
    "NOTES",
)

TRADE_BACKLOG_COLUMNS = (
    "DATE",
    "TRADE_ID",
    "SIGNAL",
    "ENTRY",
    "STOP_LOSS",
    "TAKE_PROFIT",
    "OUTCOME",
    "R_MULTIPLE",
    "PNL_USD",
    "RISK_USD",
    "XAU_OZ",
    "TV_LEVERAGE",
    "EFFECTIVE_LEVERAGE",
    "CUMULATIVE_PNL",
    "EQUITY",
    "DAILY_PNL",
    "NOTES",
)

RESOLVED_OUTCOMES = frozenset({"WIN", "LOSS", "BREAKEVEN", "CANCELLED"})

_PNL_NOTE_PATTERNS = (
    re.compile(r"PnL(?:\s+(?:gain|loss|of))?\s*[:\s]*([+-]?\$?\d+(?:\.\d+)?)", re.I),
    re.compile(r"([+-]\$?\d+(?:\.\d+)?)\s*(?:PnL|pnl)", re.I),
)


def _strip(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_float(value: Any) -> float | None:
    text = _strip(value).replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_pnl_from_notes(notes: str) -> float | None:
    if not re.search(r"\bPnL\b", notes, re.I):
        return None
    for pattern in _PNL_NOTE_PATTERNS:
        match = pattern.search(notes)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None:
                return parsed
    return None


def _normalize_header(fields: list[str]) -> list[str]:
    return [_strip(field).upper().replace(" ", "_") for field in fields]


def _empty_row() -> dict[str, str]:
    return {column: "" for column in TRADE_BACKLOG_COLUMNS}


def _row_from_legacy(fields: list[str]) -> dict[str, str]:
    row = _empty_row()
    if len(fields) < 7:
        return row
    row["DATE"] = _strip(fields[0])
    row["TRADE_ID"] = _strip(fields[1])
    row["SIGNAL"] = _strip(fields[2]).upper()
    row["ENTRY"] = _strip(fields[3])
    row["TAKE_PROFIT"] = _strip(fields[4])
    row["OUTCOME"] = _strip(fields[5]).upper()
    row["R_MULTIPLE"] = _strip(fields[6])
    row["NOTES"] = _strip(fields[7]) if len(fields) > 7 else ""
    return row


def _row_from_fields(fields: list[str], header: list[str]) -> dict[str, str]:
    row = _empty_row()
    for idx, column in enumerate(header):
        if idx >= len(fields):
            break
        key = column if column in TRADE_BACKLOG_COLUMNS else None
        if key:
            row[key] = _strip(fields[idx])
    return row


def _estimate_risk_usd(entry: float | None, stop: float | None, xau_oz: float | None) -> float | None:
    if entry is None or stop is None or xau_oz is None:
        return None
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return None
    return round(stop_distance * xau_oz, 2)


def _estimate_effective_leverage(entry: float | None, xau_oz: float | None) -> float | None:
    if entry is None or xau_oz is None or xau_oz <= 0:
        return None
    notional = entry * xau_oz
    if notional <= 0:
        return None
    return round(notional / PROP_ACCOUNT_SIZE, 2)


def _infer_xau_oz_from_pnl(
    entry: float | None,
    take_profit: float | None,
    pnl_usd: float | None,
) -> float | None:
    if entry is None or take_profit is None or pnl_usd is None:
        return None
    move = abs(take_profit - entry)
    if move <= 0:
        return None
    return round(abs(pnl_usd) / move, 4)


def _enrich_trade_row(row: dict[str, str]) -> dict[str, str]:
    notes = row.get("NOTES", "")
    outcome = row.get("OUTCOME", "").upper()
    entry = _safe_float(row.get("ENTRY"))
    stop = _safe_float(row.get("STOP_LOSS"))
    take_profit = _safe_float(row.get("TAKE_PROFIT"))
    pnl = _safe_float(row.get("PNL_USD"))
    if pnl is None and outcome in RESOLVED_OUTCOMES:
        pnl = _parse_pnl_from_notes(notes)
        if pnl is not None:
            row["PNL_USD"] = f"{pnl:.2f}"

    xau_oz = _safe_float(row.get("XAU_OZ"))
    if xau_oz is None and pnl is not None and outcome == "WIN":
        xau_oz = _infer_xau_oz_from_pnl(entry, take_profit, pnl)
        if xau_oz is not None:
            row["XAU_OZ"] = f"{xau_oz:.4f}"

    risk = _safe_float(row.get("RISK_USD"))
    if risk is None:
        risk = _estimate_risk_usd(entry, stop, xau_oz)
        if risk is not None:
            row["RISK_USD"] = f"{risk:.2f}"

    effective = _safe_float(row.get("EFFECTIVE_LEVERAGE"))
    if effective is None:
        effective = _estimate_effective_leverage(entry, xau_oz)
        if effective is not None:
            row["EFFECTIVE_LEVERAGE"] = f"{effective:.2f}"

    tv_leverage = _safe_float(row.get("TV_LEVERAGE"))
    if tv_leverage is None and effective is not None:
        if effective <= 1:
            tv_leverage = 1
        elif effective <= 10:
            tv_leverage = 10
        elif effective <= 50:
            tv_leverage = PROP_PREFERRED_TV_LEVERAGE
        else:
            tv_leverage = 100
        row["TV_LEVERAGE"] = str(int(tv_leverage))

    return row


def _apply_running_totals(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    cumulative = 0.0
    daily_totals: dict[str, float] = {}

    for row in rows:
        outcome = row.get("OUTCOME", "").upper()
        pnl = _safe_float(row.get("PNL_USD"))
        trade_date = row.get("DATE", "")

        if outcome in RESOLVED_OUTCOMES and pnl is not None:
            cumulative += pnl
            daily_totals[trade_date] = daily_totals.get(trade_date, 0.0) + pnl
            row["CUMULATIVE_PNL"] = f"{cumulative:.2f}"
            row["EQUITY"] = f"{PROP_ACCOUNT_SIZE + cumulative:.2f}"
            row["DAILY_PNL"] = f"{daily_totals[trade_date]:.2f}"
        elif cumulative != 0.0 or daily_totals:
            row["CUMULATIVE_PNL"] = f"{cumulative:.2f}"
            row["EQUITY"] = f"{PROP_ACCOUNT_SIZE + cumulative:.2f}"
            if trade_date in daily_totals:
                row["DAILY_PNL"] = f"{daily_totals[trade_date]:.2f}"

    return rows


def load_trade_backlog_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    data_lines = [
        line
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]
    if not data_lines:
        return []

    reader = csv.reader(StringIO("\n".join(data_lines)))
    rows_raw = list(reader)
    if not rows_raw:
        return []

    header = _normalize_header(rows_raw[0])
    body = rows_raw[1:] if "DATE" in header else rows_raw

    rows: list[dict[str, str]] = []
    if header == list(LEGACY_TRADE_COLUMNS):
        for fields in body:
            rows.append(_row_from_legacy(fields))
    elif "DATE" in header:
        for fields in body:
            rows.append(_row_from_fields(fields, header))
    else:
        for fields in rows_raw:
            if _strip(fields[0]).upper() == "DATE":
                continue
            rows.append(_row_from_legacy(fields))

    return [_enrich_trade_row(row) for row in rows if _strip(row.get("DATE"))]


def write_trade_backlog_rows(path: Path, rows: list[dict[str, str]]) -> None:
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(TRADE_BACKLOG_COLUMNS)
    for row in rows:
        writer.writerow([row.get(column, "") for column in TRADE_BACKLOG_COLUMNS])
    path.write_text(buffer.getvalue(), encoding="utf-8")


def compute_prop_account_snapshot(
    rows: list[dict[str, str]],
    *,
    today: str | None = None,
    floating_pnl_usd: float = 0.0,
) -> dict[str, Any]:
    today = today or date.today().isoformat()
    resolved_rows = [
        row
        for row in rows
        if row.get("OUTCOME", "").upper() in RESOLVED_OUTCOMES
        and _safe_float(row.get("PNL_USD")) is not None
    ]

    total_pnl = sum(_safe_float(row.get("PNL_USD")) or 0.0 for row in resolved_rows)
    equity = PROP_ACCOUNT_SIZE + total_pnl + floating_pnl_usd

    daily_closed_pnl = sum(
        _safe_float(row.get("PNL_USD")) or 0.0
        for row in resolved_rows
        if row.get("DATE") == today
    )
    daily_pnl = daily_closed_pnl + floating_pnl_usd

    daily_loss_room_hard = PROP_DAILY_LOSS_HARD_USD + daily_pnl
    daily_loss_room_soft = PROP_DAILY_LOSS_SOFT_USD + daily_pnl
    max_loss_room_hard = equity - PROP_EQUITY_FLOOR_USD
    max_loss_room_soft = equity - PROP_EQUITY_SOFT_FLOOR_USD
    profit_remaining = PROP_PROFIT_TARGET_USD - total_pnl

    wins = [
        row for row in resolved_rows
        if row.get("OUTCOME", "").upper() == "WIN"
        and (_safe_float(row.get("PNL_USD")) or 0.0) > 0
    ]
    losses = [
        row for row in resolved_rows
        if row.get("OUTCOME", "").upper() == "LOSS"
        or (_safe_float(row.get("PNL_USD")) or 0.0) < 0
    ]
    win_pnls = [_safe_float(row.get("PNL_USD")) or 0.0 for row in wins]
    loss_pnls = [_safe_float(row.get("PNL_USD")) or 0.0 for row in losses]

    trading_days = len({
        row.get("DATE")
        for row in rows
        if row.get("DATE") and row.get("OUTCOME", "").upper() in RESOLVED_OUTCOMES
    })
    min_trading_days_met = trading_days >= PROP_MIN_TRADING_DAYS

    if max_loss_room_hard <= 400 or daily_loss_room_hard <= 250:
        leverage_mode = "DEFENSIVE"
        recommended_tv_leverage = 10
    elif total_pnl < 0 and abs(total_pnl) >= 500:
        leverage_mode = "CAUTIOUS"
        recommended_tv_leverage = 10
    elif daily_loss_room_soft >= 300 and max_loss_room_soft >= 500:
        leverage_mode = "STANDARD_50X"
        recommended_tv_leverage = PROP_PREFERRED_TV_LEVERAGE
    else:
        leverage_mode = "BALANCED"
        recommended_tv_leverage = 10

    recommended_max_risk_usd = min(
        PROP_MAX_SINGLE_TRADE_RISK_USD,
        max(0.0, daily_loss_room_soft * 0.5),
        max(0.0, max_loss_room_soft * 0.25),
    )
    if leverage_mode == "STANDARD_50X" and recommended_max_risk_usd >= 150:
        recommended_min_risk_usd = 150.0
    elif leverage_mode == "DEFENSIVE":
        recommended_min_risk_usd = 0.0
    else:
        recommended_min_risk_usd = 100.0

    return {
        "baseline_account_usd": PROP_ACCOUNT_SIZE,
        "total_closed_pnl_usd": round(total_pnl, 2),
        "floating_pnl_usd": round(floating_pnl_usd, 2),
        "current_equity_usd": round(equity, 2),
        "profit_target_usd": PROP_PROFIT_TARGET_USD,
        "profit_remaining_usd": round(profit_remaining, 2),
        "challenge_passed": total_pnl >= PROP_PROFIT_TARGET_USD and min_trading_days_met,
        "daily_closed_pnl_usd": round(daily_closed_pnl, 2),
        "daily_pnl_usd": round(daily_pnl, 2),
        "daily_loss_room_soft_usd": round(daily_loss_room_soft, 2),
        "daily_loss_room_hard_usd": round(daily_loss_room_hard, 2),
        "max_loss_room_soft_usd": round(max_loss_room_soft, 2),
        "max_loss_room_hard_usd": round(max_loss_room_hard, 2),
        "equity_floor_hard_usd": PROP_EQUITY_FLOOR_USD,
        "equity_floor_soft_usd": PROP_EQUITY_SOFT_FLOOR_USD,
        "resolved_trades": len(resolved_rows),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win_usd": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0,
        "avg_loss_usd": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0,
        "trading_days_with_trades": trading_days,
        "min_trading_days_required": PROP_MIN_TRADING_DAYS,
        "min_trading_days_met": min_trading_days_met,
        "leverage_mode": leverage_mode,
        "recommended_tv_leverage": recommended_tv_leverage,
        "preferred_tv_leverage": PROP_PREFERRED_TV_LEVERAGE,
        "recommended_max_risk_usd": round(recommended_max_risk_usd, 2),
        "recommended_min_risk_usd": round(recommended_min_risk_usd, 2),
        "recommended_max_risk_pct": round(
            recommended_max_risk_usd / PROP_ACCOUNT_SIZE * 100, 2
        ),
        "daily_breached_soft": daily_pnl <= -PROP_DAILY_LOSS_SOFT_USD,
        "daily_breached_hard": daily_pnl <= -PROP_DAILY_LOSS_HARD_USD,
        "max_loss_breached_soft": equity <= PROP_EQUITY_SOFT_FLOOR_USD,
        "max_loss_breached_hard": equity <= PROP_EQUITY_FLOOR_USD,
        "today": today,
    }


def format_prop_account_snapshot(snapshot: dict[str, Any]) -> str:
    return f"""- Baseline account: ${snapshot['baseline_account_usd']:,}
- Total closed PnL: ${snapshot['total_closed_pnl_usd']:,.2f}
- Floating PnL (open trades): ${snapshot['floating_pnl_usd']:,.2f}
- Current equity: ${snapshot['current_equity_usd']:,.2f}
- Profit target remaining: ${snapshot['profit_remaining_usd']:,.2f} / ${snapshot['profit_target_usd']:,}
- Challenge passed: {snapshot['challenge_passed']}
- Today ({snapshot['today']}) closed PnL: ${snapshot['daily_closed_pnl_usd']:,.2f}
- Today total PnL incl. float: ${snapshot['daily_pnl_usd']:,.2f}
- Daily loss room: ${snapshot['daily_loss_room_soft_usd']:,.2f} soft / ${snapshot['daily_loss_room_hard_usd']:,.2f} hard
- Max drawdown room: ${snapshot['max_loss_room_soft_usd']:,.2f} soft / ${snapshot['max_loss_room_hard_usd']:,.2f} hard (floor ${snapshot['equity_floor_hard_usd']:,})
- Record: {snapshot['wins']} wins / {snapshot['losses']} losses over {snapshot['trading_days_with_trades']} days ({snapshot['resolved_trades']} resolved trades)
- Avg win: ${snapshot['avg_win_usd']:,.2f} | Avg loss: ${snapshot['avg_loss_usd']:,.2f}
- Min trading days met: {snapshot['min_trading_days_met']} ({snapshot['trading_days_with_trades']}/{snapshot['min_trading_days_required']})
- Leverage mode: {snapshot['leverage_mode']}
- Recommended TradingView leverage: {snapshot['recommended_tv_leverage']}:1 (preferred aligned default: {snapshot['preferred_tv_leverage']}:1)
- Recommended next-trade risk: ${snapshot['recommended_min_risk_usd']:,.0f}–${snapshot['recommended_max_risk_usd']:,.0f} ({snapshot['recommended_max_risk_pct']:.2f}% max)
- Breach flags: daily_soft={snapshot['daily_breached_soft']} daily_hard={snapshot['daily_breached_hard']} max_soft={snapshot['max_loss_breached_soft']} max_hard={snapshot['max_loss_breached_hard']}"""


def format_trade_backlog_table(rows: list[dict[str, str]], *, max_rows: int = 15) -> str:
    if not rows:
        return "(No trades logged yet.)"
    display_rows = rows[-max_rows:]
    header = " | ".join(TRADE_BACKLOG_COLUMNS)
    lines = [header, "-" * min(len(header), 180)]
    for row in display_rows:
        lines.append(" | ".join(_strip(row.get(column, "")) for column in TRADE_BACKLOG_COLUMNS))
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} older trades omitted)")
    return "\n".join(lines)


def refresh_prop_account_state(
    path: Path,
    *,
    today: str | None = None,
    floating_pnl_usd: float = 0.0,
    write_back: bool = True,
) -> dict[str, Any]:
    rows = load_trade_backlog_rows(path)
    rows = _apply_running_totals(rows)
    snapshot = compute_prop_account_snapshot(rows, today=today, floating_pnl_usd=floating_pnl_usd)

    if write_back and rows:
        write_trade_backlog_rows(path, rows)

    return {
        "snapshot": snapshot,
        "rows": rows,
        "table_text": format_trade_backlog_table(rows),
        "snapshot_text": format_prop_account_snapshot(snapshot),
    }


def trade_backlog_template() -> str:
    header = ",".join(TRADE_BACKLOG_COLUMNS)
    example = (
        "2026-05-29,TRADE_010,LONG,4465.0,4445.0,4505.0,WIN,2.0,350.00,175.00,8.7500,50,1.58,"
        "350.00,25350.00,350.00,Example row — fill PNL_USD RISK_USD XAU_OZ TV_LEVERAGE after each trade"
    )
    return f"""# Manual Trade Backlog — FundedNext Stellar 1-Step ($25K)
# One CSV row per executed trade. Running totals are auto-recomputed each agent cycle.
# Required on each new trade: DATE, TRADE_ID, SIGNAL, ENTRY, STOP_LOSS, TAKE_PROFIT, OUTCOME,
# R_MULTIPLE, PNL_USD, RISK_USD, XAU_OZ, TV_LEVERAGE (use 50 when permitted).
# Auto-filled columns: EFFECTIVE_LEVERAGE, CUMULATIVE_PNL, EQUITY, DAILY_PNL
{header}
{example}
"""
