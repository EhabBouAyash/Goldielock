"""Replay the frozen London-range rules on closed bars.

Fills use the next bar's open as a stand-in for the first quote after the
signal. That is a research assumption, not a broker fill. If the stop and the
target could both have been hit on the same bar, the stop is used.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from statistics import mean

from deepseek_trial.compliance_engine_v2 import (
    news_flatten_due,
    news_overlaps,
    screen,
)
from deepseek_trial.outcome_evaluator_v2 import payout_eligible
from deepseek_trial.research_types_v2 import (
    CONTRACT,
    DAILY_LOSS_HARD,
    EQUITY_FLOOR,
    FLAT_BY,
    INITIAL_BALANCE,
    LONDON,
    MIN_TRADING_DAYS,
    PROFIT_TARGET,
    QUICK_STRIKE,
    SPEC_STATUS,
    SPEC_VERSION,
    STOP_ATR_MULT,
    TARGET_R,
    AccountSnapshot,
    Bar,
    CalendarState,
    CostModel,
    SignalEvent,
    shift_time,
)
from deepseek_trial.risk_engine_v2 import assess
from deepseek_trial.strategy_engine_v2 import OpeningRangeRetest, london_daily_levels


@dataclass
class OpenTrade:
    setup_id: str
    side: str
    lots: float
    entry: float
    stop: float
    target: float
    risk_usd: float
    opened: datetime
    mae: float = 0.0
    mfe: float = 0.0


def _account_now(base: AccountSnapshot, equity: float, daily_pnl: float) -> AccountSnapshot:
    return replace(
        base,
        equity=equity,
        balance=equity,
        free_margin=equity,
        daily_trade_pnl=daily_pnl,
        open_positions=0,
        pending_orders=0,
        open_risk_usd=0.0,
        pending_risk_usd=0.0,
    )


def _excursions(trade: OpenTrade, bar: Bar) -> None:
    if trade.side == "LONG":
        trade.mae = max(trade.mae, max(0.0, trade.entry - bar.low))
        trade.mfe = max(trade.mfe, max(0.0, bar.high - trade.entry))
    else:
        trade.mae = max(trade.mae, max(0.0, bar.high - trade.entry))
        trade.mfe = max(trade.mfe, max(0.0, trade.entry - bar.low))


def _exit_price(side: str, mid: float, costs: CostModel) -> float:
    return costs.exit_from_mid(side, mid)


def manage(
    trade: OpenTrade,
    bar: Bar,
    costs: CostModel,
    calendar: CalendarState,
    event_filter: bool,
    flat_shift: int,
) -> tuple[float, str, datetime] | None:
    """Return exit price, reason, and time, or None to stay open."""
    _excursions(trade, bar)
    local_start = bar.start.astimezone(LONDON)
    local_end = bar.end.astimezone(LONDON)
    flat_at = shift_time(FLAT_BY, flat_shift)
    session_over = local_start.time() >= flat_at

    def stop_hit() -> tuple[float, str] | None:
        if trade.side == "LONG":
            if bar.open <= trade.stop:
                return _exit_price(trade.side, bar.open, costs), "STOP_GAP"
            if bar.low <= trade.stop:
                return _exit_price(trade.side, trade.stop, costs), "STOP"
        else:
            if bar.open >= trade.stop:
                return _exit_price(trade.side, bar.open, costs), "STOP_GAP"
            if bar.high >= trade.stop:
                return _exit_price(trade.side, trade.stop, costs), "STOP"
        return None

    stopped = stop_hit()
    if stopped:
        # Stop and target on the same bar: keep the stop. A gap uses the open.
        return stopped[0], stopped[1], bar.start

    if session_over:
        return _exit_price(trade.side, bar.open, costs), "TIME", bar.start

    if event_filter and news_flatten_due(bar.start, calendar):
        return _exit_price(trade.side, bar.open, costs), "NEWS_EXIT", bar.start

    if trade.side == "LONG" and bar.high >= trade.target:
        return _exit_price(trade.side, trade.target, costs), "TARGET", bar.start
    if trade.side == "SHORT" and bar.low <= trade.target:
        return _exit_price(trade.side, trade.target, costs), "TARGET", bar.start

    if local_end.time() >= flat_at and local_start.time() < flat_at:
        return _exit_price(trade.side, bar.close, costs), "TIME", bar.end
    return None


def _pnl(side: str, lots: float, entry: float, exit_px: float, costs: CostModel) -> float:
    direction = 1.0 if side == "LONG" else -1.0
    gross = direction * CONTRACT * lots * (exit_px - entry)
    commission = lots * CONTRACT * 2.0 * costs.commission_rate_per_side * entry
    return gross - commission


def _signal_row(event: SignalEvent, stage: str, reason: str) -> dict:
    return {
        "setup_id": event.setup_id,
        "stage": stage,
        "reason": reason,
        "side": event.side,
        "stop": event.stop,
        "signal_time": event.signal_time.isoformat(),
        "variant": event.variant,
        "bar_index": event.bar_index,
    }


def run_backtest(
    m5: list[Bar],
    h1: list[Bar],
    calendar: CalendarState,
    account: AccountSnapshot,
    variant: str = "orb_h1_trend",
    costs: CostModel | None = None,
    event_filter: bool = True,
    fill_delay_bars: int = 1,
    session_shift_minutes: int = 0,
    stop_atr_mult: float | None = None,
) -> dict:
    costs = costs or CostModel()
    if not calendar.loaded:
        return _empty("CALENDAR_UNAVAILABLE", variant)
    if fill_delay_bars < 1:
        raise ValueError("fill_delay_bars must be at least 1")
    strategy = OpeningRangeRetest(
        variant=variant,
        session_shift_minutes=session_shift_minutes,
        stop_atr_mult=stop_atr_mult if stop_atr_mult is not None else STOP_ATR_MULT,
    )
    levels = london_daily_levels(m5)
    pending: tuple[SignalEvent, int] | None = None
    position: OpenTrade | None = None
    signals: list[dict] = []
    trades: list[dict] = []
    equity = account.equity
    daily_pnl = 0.0
    day_key = ""
    pinned_day = ""
    if account.as_of is not None:
        pinned_day = account.as_of.astimezone(LONDON).date().isoformat()
    peak = equity
    max_dd = 0.0
    breached_daily = False
    breached_total = False
    killed = False

    for i, bar in enumerate(m5):
        local_day = bar.start.astimezone(LONDON).date().isoformat()
        if local_day != day_key:
            # A loss already booked today stays on today's date only.
            daily_pnl = account.daily_trade_pnl if local_day == pinned_day else 0.0
            day_key = local_day

        if pending is not None and pending[1] == i and position is None and not killed:
            event = pending[0]
            pending = None
            opened = _try_open(
                event, bar, costs, calendar, account, equity, daily_pnl, event_filter, signals
            )
            if opened is not None:
                strategy.mark_filled()
            position = opened

        if position is not None:
            decision = manage(position, bar, costs, calendar, event_filter, session_shift_minutes)
            if decision is not None:
                exit_px, reason, exit_time = decision
                pnl = _pnl(position.side, position.lots, position.entry, exit_px, costs)
                held = exit_time - position.opened
                violations = []
                if held < QUICK_STRIKE:
                    violations.append("QUICK_STRIKE")
                overlap = event_filter and news_overlaps(position.opened, exit_time, calendar)
                if overlap:
                    violations.append("NEWS_WINDOW_PROFIT")
                price_risk = abs(position.entry - position.stop)
                price_r = 0.0
                if price_risk:
                    direction = 1.0 if position.side == "LONG" else -1.0
                    price_r = direction * (exit_px - position.entry) / price_risk
                row = {
                    "setup_id": position.setup_id,
                    "side": position.side,
                    "entry": position.entry,
                    "exit": exit_px,
                    "stop": position.stop,
                    "target": position.target,
                    "opened": position.opened.isoformat(),
                    "closed": exit_time.isoformat(),
                    "exit_reason": reason,
                    "lots": position.lots,
                    "risk_usd": position.risk_usd,
                    "pnl": pnl,
                    "r_multiple": pnl / position.risk_usd if position.risk_usd else 0.0,
                    "price_r": price_r,
                    "mae": position.mae,
                    "mfe": position.mfe,
                    "costs_slippage_note": "spread and slippage are inside entry and exit; commission is subtracted in pnl",
                    "rule_violations": violations,
                    "payout_eligible_pnl": payout_eligible(pnl, violations),
                    "fill_model": "next_m5_open",
                }
                trades.append(row)
                equity += pnl
                daily_pnl += pnl
                position = None

        mark = equity
        if position is not None:
            direction = 1.0 if position.side == "LONG" else -1.0
            mark += direction * CONTRACT * position.lots * (bar.close - position.entry)
        peak = max(peak, mark)
        max_dd = max(max_dd, peak - mark)
        daily_mark = daily_pnl + (mark - equity)
        if daily_mark <= -DAILY_LOSS_HARD:
            breached_daily = True
            killed = True
        if mark <= EQUITY_FLOOR:
            breached_total = True
            killed = True

        event = strategy.on_bar(i, m5, h1, levels)
        if event is None or killed:
            continue
        if event.status == "READY" and position is None and pending is None:
            signals.append(_signal_row(event, "WAITING_FILL", ""))
            pending = (event, i + fill_delay_bars)
        elif event.status != "READY":
            signals.append(_signal_row(event, event.status, event.reason))

    report = summarize(trades, signals, equity, max_dd, breached_daily, breached_total, variant)
    report["spec"] = SPEC_VERSION
    report["spec_status"] = SPEC_STATUS
    report["fill_model"] = "next_m5_open"
    report["event_filter"] = event_filter
    report["session_shift_minutes"] = session_shift_minutes
    return report


def _try_open(event, bar, costs, calendar, account, equity, daily_pnl, event_filter, signals) -> OpenTrade | None:
    entry = costs.entry_from_mid(event.side, bar.open)
    live_account = _account_now(account, equity, daily_pnl)
    ok, reason = screen(
        mode="research",
        calendar=calendar,
        account=live_account,
        when=bar.start,
        event_filter=event_filter,
        ideas_open=0,
    )
    sized = {"ok": False, "reason": reason, "lots": 0.0, "risk_usd": 0.0, "target": None, "friction_r": None, "r_budget": None}
    if ok:
        sized = assess(
            side=event.side,
            entry=entry,
            stop=event.stop,
            opposing_high=event.opposing_high,
            opposing_low=event.opposing_low,
            costs=costs,
            account=live_account,
        )
        if not sized["ok"]:
            reason = sized["reason"]
    if not ok or not sized["ok"]:
        signals.append(_signal_row(event, "REJECTED", reason))
        return None
    signals.append(_signal_row(event, "SIGNALLED", ""))
    return OpenTrade(
        setup_id=event.setup_id,
        side=event.side,
        lots=sized["lots"],
        entry=sized["entry"],
        stop=sized["stop"],
        target=sized["target"],
        risk_usd=sized["risk_usd"],
        opened=bar.start,
    )


def _empty(reason: str, variant: str) -> dict:
    metrics = summarize([], [], INITIAL_BALANCE, 0.0, False, False, variant)["metrics"]
    return {
        "spec": SPEC_VERSION,
        "spec_status": SPEC_STATUS,
        "rejected": reason,
        "trades": [],
        "signals": [],
        "metrics": metrics,
        "plain": "No replay was run because a required input was missing. Missing data means no trade.",
    }


def summarize(trades, signals, equity, max_dd, breached_daily, breached_total, variant) -> dict:
    rs = [row["r_multiple"] for row in trades]
    wins = [row for row in trades if row["pnl"] > 0]
    losses = [row for row in trades if row["pnl"] <= 0]
    days = defaultdict(float)
    months = defaultdict(float)
    weeks = defaultdict(float)
    trade_days = set()
    for row in trades:
        day = row["closed"][:10]
        days[day] += row["pnl"]
        months[day[:7]] += row["pnl"]
        closed_at = datetime.fromisoformat(row["closed"])
        iso_week = closed_at.isocalendar()
        weeks[f"{iso_week.year}-W{iso_week.week:02d}"] += row["pnl"]
        trade_days.add(row["opened"][:10])
    streak = worst = 0
    for row in trades:
        if row["r_multiple"] <= 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    passed = (
        equity >= INITIAL_BALANCE + PROFIT_TARGET
        and len(trade_days) >= MIN_TRADING_DAYS
        and not breached_daily
        and not breached_total
    )
    metrics = {
        "trades": len(trades),
        "signals": len(signals),
        "expectancy_r": mean(rs) if rs else None,
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "avg_win_r": mean([row["r_multiple"] for row in wins]) if wins else None,
        "avg_loss_r": mean([row["r_multiple"] for row in losses]) if losses else None,
        "net_pnl": sum(row["pnl"] for row in trades),
        "payout_eligible_pnl": sum(row["payout_eligible_pnl"] for row in trades),
        "max_intraday_drawdown": max_dd,
        "worst_day": min(days.values()) if days else None,
        "worst_week": min(weeks.values()) if weeks else None,
        "worst_month": min(months.values()) if months else None,
        "monthly_pnl": dict(months),
        "longest_losing_streak": worst,
        "ending_equity": equity,
        "breached_daily": breached_daily,
        "breached_total": breached_total,
        "this_path_passed_challenge": passed,
        "challenge_note": "One historical path. Not a pass rate and not a forecast.",
        "expectancy_without_best": _without_best(trades),
        "day_block_mean_r": _bootstrap(trades),
    }
    plain = _plain(metrics)
    return {
        "variant": variant,
        "trades": trades,
        "signals": signals,
        "metrics": metrics,
        "plain": plain,
        "target_r": TARGET_R,
    }


def _without_best(trades: list[dict]) -> float | None:
    if not trades:
        return None
    ranked = sorted(trades, key=lambda row: row["pnl"], reverse=True)
    kept = ranked[1:]
    if not kept:
        return None
    return mean(row["r_multiple"] for row in kept)


def _bootstrap(trades: list[dict], draws: int = 400, seed: int = 7) -> dict | None:
    """Resample whole days, not single trades. Descriptive only."""
    by_day = defaultdict(list)
    for row in trades:
        by_day[row["opened"][:10]].append(row["r_multiple"])
    days = list(by_day)
    if not days:
        return None
    import random

    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        chosen = [rng.choice(days) for _ in days]
        values = [value for day in chosen for value in by_day[day]]
        means.append(sum(values) / len(values))
    means.sort()
    return {
        "p05": means[int(0.05 * (draws - 1))],
        "p50": means[int(0.50 * (draws - 1))],
        "p95": means[int(0.95 * (draws - 1))],
        "note": "Day-block resample of this same sample. Not proof of an edge.",
    }


def _plain(metrics: dict) -> str:
    count = metrics["trades"]
    if count == 0:
        return (
            "The replay finished with no filled trades. That only says these bars "
            "did not pass the rules. It does not show that the rules will make money."
        )
    expectancy = metrics["expectancy_r"]
    noun = "trade" if count == 1 else "trades"
    return (
        f"Research replay only, not a broker result. {count} closed {noun}, "
        f"average {expectancy:.3f} R. Payout-style profit after the news and "
        f"quick-exit flags is {metrics['payout_eligible_pnl']:.2f} dollars. "
        "This is not a reason to raise risk or to treat the method as proven."
    )


def ablation(m5, h1, calendar, account, costs: CostModel | None = None) -> dict:
    """Compare a few frozen switches. Do not mix them into one new strategy."""
    base_kwargs = dict(m5=m5, h1=h1, calendar=calendar, account=account, costs=costs)
    no_news = run_backtest(variant="orb_only", event_filter=False, **base_kwargs)
    with_news = run_backtest(variant="orb_only", event_filter=True, **base_kwargs)
    with_trend = run_backtest(variant="orb_h1_trend", event_filter=True, **base_kwargs)
    return {
        "mechanical_no_event_filter": no_news,
        "mechanical_with_event_filter": with_news,
        "mechanical_with_event_filter_and_h1_trend": with_trend,
        "macro_features": {
            "entries_changed": 0,
            "plain": "Daily macro numbers are stored as context. They do not change entries until a separate rule is written and tested.",
        },
        "claude_features": {
            "entries_changed": 0,
            "plain": "A written explanation does not add or remove trades in this version.",
        },
        "vwap": "not_added",
    }


def stress(m5, h1, calendar, account, costs: CostModel | None = None, variant: str = "orb_h1_trend") -> dict:
    costs = costs or CostModel()
    wider = replace(costs, spread=costs.spread * 2, slippage_per_side=costs.slippage_per_side * 2)
    base = run_backtest(m5, h1, calendar, account, variant=variant, costs=costs)
    return {
        "base": base["metrics"],
        "wider_spread": run_backtest(m5, h1, calendar, account, variant=variant, costs=wider)["metrics"],
        "delayed_one_extra_bar": run_backtest(
            m5, h1, calendar, account, variant=variant, costs=costs, fill_delay_bars=2
        )["metrics"],
        "session_shift_5m": run_backtest(
            m5, h1, calendar, account, variant=variant, costs=costs, session_shift_minutes=5
        )["metrics"],
        "stop_buffer_0.08": run_backtest(
            m5, h1, calendar, account, variant=variant, costs=costs, stop_atr_mult=0.08
        )["metrics"],
        "stop_buffer_0.12": run_backtest(
            m5, h1, calendar, account, variant=variant, costs=costs, stop_atr_mult=0.12
        )["metrics"],
        "without_best_trade_expectancy_r": base["metrics"]["expectancy_without_best"],
        "note": "Nearby settings are a sensitivity check. They are not a search for the best backtest.",
    }
