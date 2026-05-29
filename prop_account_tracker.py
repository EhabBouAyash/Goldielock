"""
FundedNext Stellar 1-Step prop account tracking from trade_backlog.txt ONLY.

trade_backlog.txt is the sole source of truth for equity, PnL, and win/loss stats.
The last row's CUMULATIVE_PNL, EQUITY, and DAILY_PNL (when dated today) are
authoritative. Derived metrics (win rate, rooms, leverage caps) are computed from
this file only — never from model_trade_log or other files.
"""

from __future__ import annotations

import csv
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
PROP_MAX_SINGLE_TRADE_RISK_USD = 250  # legacy reference; daily-budget model overrides
PROP_PREFERRED_TV_LEVERAGE = 100
MT5_ACCOUNT_LEVERAGE = 100
# One trade at a time: size each trade to ~full remaining daily loss room (not fixed 1%).
PROP_DAILY_ROOM_USE_PCT = 0.95
PROP_DAILY_ROOM_HARD_CAP_PCT = 0.90
PROP_MAX_DRAWDOWN_SINGLE_TRADE_PCT = 0.50

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
DECIDED_OUTCOMES = frozenset({"WIN", "LOSS"})


def _strip(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_float(value: Any) -> float | None:
    text = _strip(value).replace(",", "").replace("$", "").replace("+", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
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
        if column in TRADE_BACKLOG_COLUMNS:
            row[column] = _strip(fields[idx])
    return row


def _estimate_effective_leverage(entry: float | None, xau_oz: float | None) -> float | None:
    if entry is None or xau_oz is None or xau_oz <= 0:
        return None
    notional = entry * xau_oz
    if notional <= 0:
        return None
    return round(notional / PROP_ACCOUNT_SIZE, 2)


def load_trade_backlog_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    data_lines = [
        line for line in lines if line.strip() and not line.strip().startswith("#")
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

    return [row for row in rows if _strip(row.get("DATE"))]


def compute_prop_account_snapshot(
    rows: list[dict[str, str]],
    *,
    today: str | None = None,
) -> dict[str, Any]:
    today = today or date.today().isoformat()
    last_row = rows[-1] if rows else {}

    # Authoritative values from the LAST row of trade_backlog.txt
    last_trade_id = last_row.get("TRADE_ID", "")
    last_trade_date = last_row.get("DATE", "")
    last_cumulative = _safe_float(last_row.get("CUMULATIVE_PNL"))
    last_equity = _safe_float(last_row.get("EQUITY"))
    last_daily = _safe_float(last_row.get("DAILY_PNL"))

    # Never re-sum or override — last row cells are the only account truth.
    total_pnl = last_cumulative if last_cumulative is not None else 0.0
    equity = last_equity if last_equity is not None else float(PROP_ACCOUNT_SIZE)
    today_trade_rows = [
        row
        for row in rows
        if row.get("DATE") == today and row.get("OUTCOME", "").upper() in RESOLVED_OUTCOMES
    ]
    trades_closed_today = len([
        row for row in today_trade_rows if row.get("OUTCOME", "").upper() in DECIDED_OUTCOMES
    ])
    if last_trade_date == today and last_daily is not None:
        daily_closed_pnl = last_daily
    else:
        daily_closed_pnl = sum(
            pnl
            for row in today_trade_rows
            if (pnl := _safe_float(row.get("PNL_USD"))) is not None
        )
    last_row_complete = last_cumulative is not None and last_equity is not None

    daily_pnl = daily_closed_pnl
    daily_loss_room_hard = PROP_DAILY_LOSS_HARD_USD + daily_pnl
    daily_loss_room_soft = PROP_DAILY_LOSS_SOFT_USD + daily_pnl
    max_loss_room_hard = equity - PROP_EQUITY_FLOOR_USD
    max_loss_room_soft = equity - PROP_EQUITY_SOFT_FLOOR_USD
    profit_remaining = PROP_PROFIT_TARGET_USD - total_pnl

    # Win/loss stats from OUTCOME column only (trade_backlog.txt)
    decided_rows = [
        row for row in rows if row.get("OUTCOME", "").upper() in DECIDED_OUTCOMES
    ]
    wins = sum(1 for row in decided_rows if row.get("OUTCOME", "").upper() == "WIN")
    losses = sum(1 for row in decided_rows if row.get("OUTCOME", "").upper() == "LOSS")
    win_rate_pct = round(100.0 * wins / (wins + losses), 2) if (wins + losses) else 0.0

    win_pnls = [
        _safe_float(row.get("PNL_USD"))
        for row in rows
        if row.get("OUTCOME", "").upper() == "WIN"
        and _safe_float(row.get("PNL_USD")) is not None
    ]
    loss_pnls = [
        _safe_float(row.get("PNL_USD"))
        for row in rows
        if row.get("OUTCOME", "").upper() == "LOSS"
        and _safe_float(row.get("PNL_USD")) is not None
    ]

    trades_with_pnl = sum(
        1
        for row in rows
        if _safe_float(row.get("PNL_USD")) is not None
        and row.get("OUTCOME", "").upper() in RESOLVED_OUTCOMES
    )

    trading_days = len({
        row.get("DATE")
        for row in rows
        if row.get("DATE") and row.get("OUTCOME", "").upper() in RESOLVED_OUTCOMES
    })
    min_trading_days_met = trading_days >= PROP_MIN_TRADING_DAYS

    if daily_pnl <= -PROP_DAILY_LOSS_SOFT_USD:
        daily_risk_session = "DAILY_LIMIT_REACHED"
        recommended_max_risk_usd = 0.0
        recommended_min_risk_usd = 0.0
        leverage_mode = "DEFENSIVE"
        recommended_tv_leverage = 10
    elif max_loss_room_hard <= 400 or daily_loss_room_hard <= 50:
        daily_risk_session = "LOW_ROOM"
        recommended_max_risk_usd = max(0.0, min(
            daily_loss_room_soft * PROP_DAILY_ROOM_USE_PCT,
            max_loss_room_soft * 0.25,
        ))
        recommended_min_risk_usd = 0.0
        leverage_mode = "DEFENSIVE"
        recommended_tv_leverage = 10
    elif trades_closed_today == 0:
        # First trade of the day — use ~full daily soft room (one trade at a time).
        daily_risk_session = "FIRST_TRADE_OF_DAY"
        recommended_max_risk_usd = min(
            daily_loss_room_soft * PROP_DAILY_ROOM_USE_PCT,
            daily_loss_room_hard * PROP_DAILY_ROOM_HARD_CAP_PCT,
            max_loss_room_soft * PROP_MAX_DRAWDOWN_SINGLE_TRADE_PCT,
        )
        recommended_min_risk_usd = recommended_max_risk_usd * 0.85
        leverage_mode = "STANDARD_100X"
        recommended_tv_leverage = PROP_PREFERRED_TV_LEVERAGE
    elif daily_pnl > 0:
        # Prior trade won today — FundedNext expands daily loss budget; size next trade to full remaining room.
        daily_risk_session = "POST_WIN_ADD_ON"
        recommended_max_risk_usd = min(
            daily_loss_room_soft * PROP_DAILY_ROOM_USE_PCT,
            daily_loss_room_hard * PROP_DAILY_ROOM_HARD_CAP_PCT,
            max_loss_room_soft * PROP_MAX_DRAWDOWN_SINGLE_TRADE_PCT,
        )
        recommended_min_risk_usd = recommended_max_risk_usd * 0.75
        leverage_mode = "STANDARD_100X"
        recommended_tv_leverage = PROP_PREFERRED_TV_LEVERAGE
    else:
        # Prior trade lost today — only risk what is left before daily breach.
        daily_risk_session = "POST_LOSS_RECOVERY"
        recommended_max_risk_usd = min(
            daily_loss_room_soft * PROP_DAILY_ROOM_USE_PCT,
            max_loss_room_soft * PROP_MAX_DRAWDOWN_SINGLE_TRADE_PCT,
        )
        recommended_min_risk_usd = max(0.0, recommended_max_risk_usd * 0.50)
        leverage_mode = "CAUTIOUS" if recommended_max_risk_usd < 200 else "STANDARD_100X"
        recommended_tv_leverage = (
            PROP_PREFERRED_TV_LEVERAGE if leverage_mode == "STANDARD_100X" else 10
        )

    daily_breached_soft = daily_pnl <= -PROP_DAILY_LOSS_SOFT_USD

    return {
        "authoritative_source": "trade_backlog.txt",
        "read_only": True,
        "file_never_modified_by_agent": True,
        "last_row_complete": last_row_complete,
        "authoritative_last_row": {
            "trade_id": last_trade_id,
            "date": last_trade_date,
            "cumulative_pnl_usd": last_cumulative,
            "equity_usd": last_equity,
            "daily_pnl_usd": last_daily,
            "pnl_usd": _safe_float(last_row.get("PNL_USD")),
            "outcome": last_row.get("OUTCOME", ""),
        },
        "baseline_account_usd": PROP_ACCOUNT_SIZE,
        "total_closed_pnl_usd": round(total_pnl, 2),
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
        "total_trades": len(rows),
        "decided_trades": len(decided_rows),
        "trades_with_pnl_usd": trades_with_pnl,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate_pct,
        "avg_win_usd": round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0,
        "avg_loss_usd": round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0,
        "trading_days_with_trades": trading_days,
        "min_trading_days_required": PROP_MIN_TRADING_DAYS,
        "min_trading_days_met": min_trading_days_met,
        "leverage_mode": leverage_mode,
        "recommended_tv_leverage": recommended_tv_leverage,
        "preferred_tv_leverage": PROP_PREFERRED_TV_LEVERAGE,
        "mt5_account_leverage": MT5_ACCOUNT_LEVERAGE,
        "recommended_max_risk_usd": round(recommended_max_risk_usd, 2),
        "recommended_min_risk_usd": round(recommended_min_risk_usd, 2),
        "recommended_max_risk_pct": (
            round(recommended_max_risk_usd / equity * 100, 2) if equity > 0 else 0.0
        ),
        "trades_closed_today": trades_closed_today,
        "daily_risk_session": daily_risk_session,
        "daily_room_use_pct": PROP_DAILY_ROOM_USE_PCT * 100,
        "daily_breached_soft": daily_pnl <= -PROP_DAILY_LOSS_SOFT_USD,
        "daily_breached_hard": daily_pnl <= -PROP_DAILY_LOSS_HARD_USD,
        "max_loss_breached_soft": equity <= PROP_EQUITY_SOFT_FLOOR_USD,
        "max_loss_breached_hard": equity <= PROP_EQUITY_FLOOR_USD,
        "today": today,
    }


def format_prop_account_snapshot(snapshot: dict[str, Any]) -> str:
    last = snapshot.get("authoritative_last_row") or {}
    return f"""AUTHORITATIVE SOURCE: trade_backlog.txt ONLY — READ ONLY, never rewritten by the agent
LAST ROW ({last.get('trade_id', 'n/a')} on {last.get('date', 'n/a')}) — cite these exact values:
  CUMULATIVE_PNL: ${last.get('cumulative_pnl_usd', 'n/a')}
  EQUITY: ${last.get('equity_usd', 'n/a')}
  DAILY_PNL: ${last.get('daily_pnl_usd', 'n/a')}
  Last trade PNL_USD: ${last.get('pnl_usd', 'n/a')} ({last.get('outcome', 'n/a')})
  Do NOT re-sum PNL_USD or override these with computed totals.

ACCOUNT STATE (from last row + trade_backlog-derived stats):
- Baseline account: ${snapshot['baseline_account_usd']:,}
- Total closed PnL: ${snapshot['total_closed_pnl_usd']:,.2f}
- Current equity: ${snapshot['current_equity_usd']:,.2f}
- Profit target remaining: ${snapshot['profit_remaining_usd']:,.2f} / ${snapshot['profit_target_usd']:,}
- Challenge passed: {snapshot['challenge_passed']}
- Today ({snapshot['today']}) closed PnL: ${snapshot['daily_closed_pnl_usd']:,.2f} | Trades closed today: {snapshot.get('trades_closed_today', 0)}
- Daily loss room: ${snapshot['daily_loss_room_soft_usd']:,.2f} soft / ${snapshot['daily_loss_room_hard_usd']:,.2f} hard
- Daily risk session: {snapshot.get('daily_risk_session', 'n/a')} (one trade at a time — size to ~{snapshot.get('daily_room_use_pct', 95):.0f}% of remaining daily room)
- Next trade risk budget: ${snapshot['recommended_min_risk_usd']:,.0f}–${snapshot['recommended_max_risk_usd']:,.0f} ({snapshot['recommended_max_risk_pct']:.2f}% of equity) — if stopped, must stay within daily limit
- Max drawdown room: ${snapshot['max_loss_room_soft_usd']:,.2f} soft / ${snapshot['max_loss_room_hard_usd']:,.2f} hard (floor ${snapshot['equity_floor_hard_usd']:,})
- Win rate: {snapshot['win_rate_pct']:.2f}% ({snapshot['wins']}W / {snapshot['losses']}L from OUTCOME column, {snapshot['decided_trades']} decided trades)
- Trades with PNL_USD filled: {snapshot['trades_with_pnl_usd']} / {snapshot['total_trades']}
- Avg win (PNL_USD on WIN rows): ${snapshot['avg_win_usd']:,.2f} | Avg loss: ${snapshot['avg_loss_usd']:,.2f}
- Trading days: {snapshot['trading_days_with_trades']} (min {snapshot['min_trading_days_required']} met: {snapshot['min_trading_days_met']})
- Leverage mode: {snapshot['leverage_mode']} | Recommended TV: {snapshot['recommended_tv_leverage']}:1 | MT5: {snapshot.get('mt5_account_leverage', MT5_ACCOUNT_LEVERAGE)}:1
- Breach flags: daily_soft={snapshot['daily_breached_soft']} daily_hard={snapshot['daily_breached_hard']} max_soft={snapshot['max_loss_breached_soft']} max_hard={snapshot['max_loss_breached_hard']}"""


def format_trade_backlog_table(rows: list[dict[str, str]], *, max_rows: int = 15) -> str:
    if not rows:
        return "(No trades logged yet.)"
    display_rows = rows[-max_rows:]
    header = " | ".join(TRADE_BACKLOG_COLUMNS)
    lines = [
        "SOURCE: trade_backlog.txt — last row is authoritative for EQUITY and CUMULATIVE_PNL",
        header,
        "-" * min(len(header), 180),
    ]
    for row in display_rows:
        lines.append(" | ".join(_strip(row.get(column, "")) for column in TRADE_BACKLOG_COLUMNS))
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} older trades omitted)")
    if rows:
        last = rows[-1]
        lines.append(
            f"\n>>> LAST ROW >>> {last.get('TRADE_ID')} | "
            f"CUMULATIVE_PNL={last.get('CUMULATIVE_PNL')} | "
            f"EQUITY={last.get('EQUITY')} | "
            f"DAILY_PNL={last.get('DAILY_PNL')}"
        )
    return "\n".join(lines)


def refresh_prop_account_state(
    path: Path,
    *,
    today: str | None = None,
) -> dict[str, Any]:
    """Read trade_backlog.txt only. This function never writes to the file."""
    rows = load_trade_backlog_rows(path)
    snapshot = compute_prop_account_snapshot(rows, today=today)

    return {
        "snapshot": snapshot,
        "rows": rows,
        "table_text": format_trade_backlog_table(rows),
        "snapshot_text": format_prop_account_snapshot(snapshot),
    }


def trade_backlog_template() -> str:
    header = ",".join(TRADE_BACKLOG_COLUMNS)
    example = (
        "2026-05-29,TRADE_010,LONG,4465.0,4445.0,4505.0,WIN,2.0,350.00,175.00,8.7500,100,1.58,"
        "350.00,25350.00,350.00,Example — you maintain all columns; agent never edits this file"
    )
    return f"""# Manual Trade Backlog — FundedNext Stellar 1-Step ($25K)
# SOLE SOURCE OF TRUTH for equity, PnL, win rate. Last row CUMULATIVE_PNL/EQUITY are authoritative.
# YOU edit this file manually after each trade. The agent reads it only — never writes or recomputes rows.
# Fill PNL_USD, CUMULATIVE_PNL, EQUITY, DAILY_PNL on every new row yourself.
{header}
{example}
"""
