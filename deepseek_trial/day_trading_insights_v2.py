"""One research cycle for the London opening-range rules.

The fixed checks decide. A written explanation, a weighted score, or an old
saved chart cannot turn a failed check into a trade. This program does not
send an order to a broker.

Replay a sample day:

    python3 -m deepseek_trial.day_trading_insights_v2 --self-check
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from deepseek_trial.backtest_engine_v2 import run_backtest
from deepseek_trial.compliance_engine_v2 import market_can_authorize, quote_is_fresh, screen
from deepseek_trial.fetch_market_data_v2 import label_real_yield
from deepseek_trial.outcome_evaluator_v2 import TERMINAL, Ledger
from deepseek_trial.prop_account_tracker_v2 import research_snapshot
from deepseek_trial.research_types_v2 import (
    LONDON,
    SPEC_STATUS,
    SPEC_VERSION,
    UTC,
    AccountSnapshot,
    Bar,
    CalendarState,
    CostModel,
    Decision,
    MarketData,
)
from deepseek_trial.risk_engine_v2 import assess
from deepseek_trial.strategy_engine_v2 import OpeningRangeRetest, london_daily_levels


def build_sample_session(direction: str = "up") -> dict:
    """A made-up Monday used to show the plumbing. It is not market history."""

    def at(day: str, hour: int, minute: int, o, h, l, c, minutes: int = 5) -> Bar:
        start = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), hour, minute, tzinfo=LONDON)
        start = start.astimezone(UTC)
        return Bar(start=start, end=start + timedelta(minutes=minutes), open=o, high=h, low=l, close=c)

    friday = at("2026-10-02", 12, 0, 2300, 2500, 2000, 2300)
    warmup = []
    cursor = datetime(2026, 10, 5, 4, 0, tzinfo=LONDON)
    while cursor < datetime(2026, 10, 5, 8, 0, tzinfo=LONDON):
        warmup.append(Bar(
            start=cursor.astimezone(UTC),
            end=(cursor + timedelta(minutes=5)).astimezone(UTC),
            open=2305, high=2306, low=2304, close=2305,
        ))
        cursor += timedelta(minutes=5)
    opening = []
    cursor = datetime(2026, 10, 5, 8, 0, tzinfo=LONDON)
    for _ in range(6):
        opening.append(Bar(
            start=cursor.astimezone(UTC),
            end=(cursor + timedelta(minutes=5)).astimezone(UTC),
            open=2305, high=2310, low=2300, close=2305,
        ))
        cursor += timedelta(minutes=5)
    breakout = at("2026-10-05", 8, 30, 2308, 2316, 2308, 2314)
    retest = at("2026-10-05", 8, 35, 2314, 2315, 2305, 2312)
    fill = at("2026-10-05", 8, 40, 2312, 2314, 2310, 2313)
    runner = at("2026-10-05", 8, 45, 2313, 2400, 2311, 2330)
    later = []
    cursor = datetime(2026, 10, 5, 8, 50, tzinfo=LONDON)
    while cursor < datetime(2026, 10, 5, 11, 0, tzinfo=LONDON):
        later.append(Bar(
            start=cursor.astimezone(UTC),
            end=(cursor + timedelta(minutes=5)).astimezone(UTC),
            open=2330, high=2332, low=2328, close=2330,
        ))
        cursor += timedelta(minutes=5)

    step = 2.0 if direction == "up" else -2.0
    price = 1800.0 if direction == "up" else 4000.0
    h1 = []
    end = datetime(2026, 10, 5, 8, 0, tzinfo=LONDON)
    cursor = end - timedelta(hours=60)
    for _ in range(60):
        close = price + step
        h1.append(Bar(
            start=cursor.astimezone(UTC),
            end=(cursor + timedelta(hours=1)).astimezone(UTC),
            open=price,
            high=max(price, close) + 0.5,
            low=min(price, close) - 0.5,
            close=close,
        ))
        price = close
        cursor += timedelta(hours=1)

    through = [friday, *warmup, *opening, breakout, retest]
    return {
        "m5": [*through, fill, runner, *later],
        "h1": h1,
        "through_retest": through,
        "retest_end": retest.end,
    }


def _explain(decision: Decision, explanation) -> Decision:
    locked = (
        decision.signal,
        decision.reason,
        decision.entry,
        decision.stop,
        decision.target,
        decision.lots,
        decision.risk_usd,
    )
    produced = None if explanation is None else explanation(decision)
    if isinstance(produced, dict):
        decision.notes.append("The explanation tried to return a signal. Only the note was kept.")
        produced = produced.get("explanation") or produced.get("note") or ""
    if produced is None:
        produced = _default_explanation(decision)
    decision.explanation = str(produced)
    (
        decision.signal,
        decision.reason,
        decision.entry,
        decision.stop,
        decision.target,
        decision.lots,
        decision.risk_usd,
    ) = locked
    decision.send_order = False
    decision.execution = "MANUAL_REVIEW" if decision.signal in ("LONG", "SHORT") else "NO_TRADE"
    return decision


def _default_explanation(decision: Decision) -> str:
    if decision.signal == "NO-TRADE":
        return f"No trade. Reason: {decision.reason}."
    return (
        f"{decision.signal} passed the fixed checks. Suggested entry {decision.entry:.2f}, "
        f"stop {decision.stop:.2f}, target {decision.target:.2f}, size {decision.lots:.2f} lots, "
        f"about {decision.risk_usd:.0f} dollars if the stop is hit. "
        "Nothing has been sent to the broker. You place the trade yourself only if the account rules allow it."
    )


def _finish(decision: Decision, market: MarketData, explanation) -> Decision:
    if market.real_yield:
        decision.context["real_yield"] = market.real_yield
        decision.notes.append("The daily real yield is context only. It did not confirm the entry.")
    return _explain(decision, explanation)


def run_cycle(
    market: MarketData,
    calendar: CalendarState,
    account: AccountSnapshot,
    now: datetime,
    ledger: Ledger | None = None,
    variant: str = "orb_h1_trend",
    explanation=None,
    score_weights=None,
    force_signal=None,
    profit_fn=None,
    event_filter: bool = True,
    costs: CostModel | None = None,
) -> Decision:
    """Live decision for the latest closed bar. A replay belongs in the backtest."""
    costs = costs or CostModel()
    notes = []
    if score_weights is not None:
        notes.append("Weighted timeframe scores are not a decision. They were ignored.")
    if force_signal:
        notes.append("A caller-supplied direction was ignored. The fixed checks decide.")

    def no_trade(reason: str, setup_id: str = "") -> Decision:
        decision = Decision(
            signal="NO-TRADE",
            reason=reason,
            setup_id=setup_id,
            notes=list(notes),
            as_of=now.isoformat(),
            send_order=False,
            execution="NO_TRADE",
        )
        return _finish(decision, market, explanation)

    if any(bar.end > now for bar in market.m5 + market.h1):
        return no_trade("DATA_INVALID")
    ok, reason = market_can_authorize(market, now)
    if not ok:
        return no_trade(reason or "DATA_INVALID")
    if market.quote is None or market.quote.source != "mt5" or not quote_is_fresh(market.quote.time, now):
        return no_trade("DATA_INVALID")

    strategy = OpeningRangeRetest(variant=variant)
    levels = london_daily_levels(market.m5)
    last_event = None
    for index in range(len(market.m5)):
        event = strategy.on_bar(index, market.m5, market.h1, levels)
        if event is not None:
            last_event = event
    if last_event is None or last_event.bar_index != len(market.m5) - 1:
        return no_trade("NO_SETUP")

    if ledger is not None and ledger.has_setup(last_event.setup_id):
        existing = ledger.get(last_event.setup_id)
        if existing and existing.get("state") in TERMINAL:
            return no_trade("SETUP_CONSUMED", last_event.setup_id)

    if last_event.status != "READY":
        _record(ledger, last_event, "EXPIRED" if last_event.status == "EXPIRED" else "REJECTED", now, reason=last_event.reason)
        return no_trade(last_event.reason or "NO_SETUP", last_event.setup_id)

    if profit_fn is None:
        _record(ledger, last_event, "REJECTED", now, reason="MT5_PROFIT_CALC_UNAVAILABLE")
        return no_trade("MT5_PROFIT_CALC_UNAVAILABLE", last_event.setup_id)

    entry = market.quote.ask + costs.slippage_per_side if last_event.side == "LONG" else market.quote.bid - costs.slippage_per_side
    ok, reason = screen(
        mode="live",
        calendar=calendar,
        account=account,
        when=now,
        event_filter=event_filter,
        ideas_open=account.open_positions + account.pending_orders,
    )
    sized = {"ok": False, "reason": reason}
    if ok:
        sized = assess(
            side=last_event.side,
            entry=entry,
            stop=last_event.stop,
            opposing_high=last_event.opposing_high,
            opposing_low=last_event.opposing_low,
            costs=costs,
            account=account,
            spread=market.quote.spread,
            profit_fn=profit_fn,
        )
        reason = "" if sized["ok"] else sized["reason"]
    if not ok or not sized["ok"]:
        _record(ledger, last_event, "REJECTED", now, reason=reason)
        return no_trade(reason, last_event.setup_id)

    _record(
        ledger,
        last_event,
        "SIGNALLED",
        now,
        reason="",
        extra={
            "entry": sized["entry"],
            "stop": sized["stop"],
            "target": sized["target"],
            "lots": sized["lots"],
            "risk_usd": sized["risk_usd"],
        },
    )
    decision = Decision(
        signal=last_event.side,
        reason="PASSED",
        setup_id=last_event.setup_id,
        entry=sized["entry"],
        stop=sized["stop"],
        target=sized["target"],
        lots=sized["lots"],
        risk_usd=sized["risk_usd"],
        friction_r=sized.get("friction_r"),
        r_budget=sized.get("r_budget"),
        notes=list(notes),
        as_of=now.isoformat(),
        send_order=False,
        execution="MANUAL_REVIEW",
    )
    return _finish(decision, market, explanation)


def _record(ledger: Ledger | None, event, state: str, now: datetime, reason: str, extra: dict | None = None) -> None:
    if ledger is None or ledger.has_setup(event.setup_id):
        return
    row = {
        "setup_id": event.setup_id,
        "state": state,
        "reason": reason,
        "side": event.side,
        "signal_time": event.signal_time.isoformat(),
        "recorded_at": now.isoformat(),
        "variant": event.variant,
    }
    if extra:
        row.update(extra)
    ledger.append_signal(row)


def self_check() -> int:
    sample = build_sample_session()
    now = sample["retest_end"] + timedelta(seconds=1)
    calendar = CalendarState(loaded=True, events=[])
    account = research_snapshot(now)
    report = run_backtest(sample["m5"], sample["h1"], calendar, account)
    refused_market = MarketData(
        source="yahoo",
        tradable=False,
        reason="DATA_INVALID",
        m5=sample["through_retest"],
        h1=sample["h1"],
        as_of=now,
        display_only=True,
        real_yield=label_real_yield(1.7, datetime(2026, 10, 2, tzinfo=timezone.utc)),
    )
    refused = run_cycle(
        refused_market,
        calendar,
        account,
        now,
        Ledger(),
        explanation=lambda _decision: {"signal": "LONG", "explanation": "I would buy."},
    )
    print(report["plain"])
    print(f"Sample replay filled trades: {report['metrics']['trades']}")
    print(f"Without the broker feed the decision is {refused.signal} ({refused.reason}).")
    print("A note that said to buy was stored and did not change that decision.")
    print(refused_market.real_yield["plain"])
    if refused.signal != "NO-TRADE" or refused.send_order:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GOLD-ORB-v2 research cycle. Does not send orders.")
    parser.add_argument("--self-check", action="store_true", help="Replay a made-up Monday and show a refused live cycle.")
    parser.add_argument("--spec", action="store_true", help="Print the specification id.")
    args = parser.parse_args(argv)
    if args.spec:
        print(f"{SPEC_VERSION} ({SPEC_STATUS})")
        return 0
    if args.self_check:
        return self_check()
    print(
        f"{SPEC_VERSION} is a research specification ({SPEC_STATUS}). "
        "It does not place trades. Run --self-check to see a sample replay and a refused live cycle."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
