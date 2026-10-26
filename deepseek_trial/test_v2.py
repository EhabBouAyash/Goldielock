"""Checks for the GOLD-ORB-v2 research track. These do not prove the rules make money."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

from deepseek_trial.additional_news_v2 import dedupe_headlines, load_calendar
from deepseek_trial.backtest_engine_v2 import run_backtest
from deepseek_trial.compliance_engine_v2 import news_blocks_entry
from deepseek_trial.day_trading_insights_v2 import build_sample_session, run_cycle
from deepseek_trial.fetch_intraday_v2 import deterministic_features, prepare
from deepseek_trial.fetch_market_data_v2 import bars_from_rates, fetch_executable, label_context_feed, label_real_yield
from deepseek_trial.outcome_evaluator_v2 import Ledger, compare_signals
from deepseek_trial.prop_account_tracker_v2 import annotations_from_text, from_mt5, research_snapshot
from deepseek_trial.research_types_v2 import (
    UTC,
    AccountSnapshot,
    CalendarState,
    CostModel,
    MarketData,
    NewsEvent,
    Quote,
)
from deepseek_trial.risk_engine_v2 import assess, risk_budget


def _calendar() -> CalendarState:
    return CalendarState(loaded=True, events=[])


def _profit(_side, entry, stop, lots):
    return abs(entry - stop) * 100.0 * lots


def _live_account(now) -> AccountSnapshot:
    return AccountSnapshot(
        source="mt5",
        as_of=now,
        authoritative=True,
        research_only=False,
        balance=25_000,
        equity=25_000,
        free_margin=25_000,
        margin_used=0,
        daily_trade_pnl=0,
        open_risk_usd=0,
        pending_risk_usd=0,
        open_positions=0,
        pending_orders=0,
        withdrawals_usd=0,
        automation_permitted=False,
        missing_stop=False,
        reconciliation_ok=True,
    )


def _live_market(sample, now) -> MarketData:
    return MarketData(
        source="mt5",
        tradable=True,
        reason="",
        m5=sample["through_retest"],
        h1=sample["h1"],
        as_of=now,
        quote=Quote(time=now, bid=2312.0, ask=2312.15, source="mt5"),
    )


class OrbReplayTests(unittest.TestCase):
    def test_long_retest_fills_next_bar_and_reaches_target(self):
        sample = build_sample_session()
        now = sample["retest_end"]
        report = run_backtest(sample["m5"], sample["h1"], _calendar(), research_snapshot(now))
        self.assertEqual(report["metrics"]["trades"], 1)
        trade = report["trades"][0]
        self.assertEqual(trade["side"], "LONG")
        self.assertEqual(trade["exit_reason"], "TARGET")
        self.assertGreater(trade["pnl"], 0)
        self.assertGreater(trade["payout_eligible_pnl"], 0)
        self.assertLessEqual(trade["risk_usd"], 52.50 + 1e-6)
        self.assertFalse(report["metrics"]["this_path_passed_challenge"])
        self.assertIn("not a reason to raise risk", report["plain"])

    def test_same_bar_stop_and_target_uses_the_stop(self):
        sample = build_sample_session()
        for index, item in enumerate(sample["m5"]):
            if item.open == 2312 and item.low == 2310:
                sample["m5"][index] = type(item)(item.start, item.end, 2312, 3000, 2000, 2313)
                break
        report = run_backtest(sample["m5"], sample["h1"], _calendar(), research_snapshot(sample["retest_end"]))
        self.assertEqual(report["trades"][0]["exit_reason"], "STOP")
        self.assertLess(report["trades"][0]["pnl"], 0)
        self.assertIn("QUICK_STRIKE", report["trades"][0]["rule_violations"])

    def test_trend_filter_blocks_a_long_in_a_downtrend(self):
        sample = build_sample_session("down")
        now = sample["retest_end"]
        blocked = run_backtest(sample["m5"], sample["h1"], _calendar(), research_snapshot(now), variant="orb_h1_trend")
        allowed = run_backtest(sample["m5"], sample["h1"], _calendar(), research_snapshot(now), variant="orb_only")
        self.assertEqual(blocked["metrics"]["trades"], 0)
        self.assertTrue(any(row["reason"] == "TREND_FILTER" for row in blocked["signals"]))
        self.assertEqual(allowed["metrics"]["trades"], 1)

    def test_missing_calendar_and_news_blackout(self):
        sample = build_sample_session()
        now = sample["retest_end"]
        missing = run_backtest(sample["m5"], sample["h1"], CalendarState(loaded=False), research_snapshot(now))
        self.assertEqual(missing["rejected"], "CALENDAR_UNAVAILABLE")
        event = NewsEvent(
            start=datetime(2026, 10, 5, 7, 40, tzinfo=UTC),
            end=datetime(2026, 10, 5, 7, 40, tzinfo=UTC),
            title="Test print",
        )
        blocked = run_backtest(
            sample["m5"], sample["h1"], CalendarState(loaded=True, events=[event]), research_snapshot(now)
        )
        self.assertEqual(blocked["metrics"]["trades"], 0)
        self.assertTrue(any(row["reason"] == "NEWS_BLACKOUT" for row in blocked["signals"]))

    def test_news_gain_is_not_payout_profit(self):
        sample = build_sample_session()
        for index, bar in enumerate(sample["m5"]):
            local = bar.start.astimezone(UTC)
            # 07:45–07:55 UTC is 08:45–08:55 London. Keep these under the target.
            if datetime(2026, 10, 5, 7, 45, tzinfo=UTC) <= local < datetime(2026, 10, 5, 8, 0, tzinfo=UTC):
                sample["m5"][index] = type(bar)(bar.start, bar.end, 2316, 2320, 2314, 2318)
        # 09:15 London = 08:15 UTC. The entry at 08:40 London is still outside the blackout.
        event = NewsEvent(
            start=datetime(2026, 10, 5, 8, 15, tzinfo=UTC),
            end=datetime(2026, 10, 5, 8, 15, tzinfo=UTC),
            title="Later print",
        )
        now = sample["retest_end"]
        report = run_backtest(
            sample["m5"], sample["h1"], CalendarState(loaded=True, events=[event]), research_snapshot(now)
        )
        self.assertEqual(report["metrics"]["trades"], 1, report["signals"])
        trade = report["trades"][0]
        self.assertEqual(trade["exit_reason"], "NEWS_EXIT")
        self.assertGreater(trade["pnl"], 0)
        self.assertEqual(trade["payout_eligible_pnl"], 0)
        self.assertIn("NEWS_WINDOW_PROFIT", trade["rule_violations"])

    def test_wide_costs_and_tight_daily_room_reject(self):
        sample = build_sample_session()
        now = sample["retest_end"]
        costly = run_backtest(
            sample["m5"], sample["h1"], _calendar(), research_snapshot(now), costs=CostModel(spread=5.0)
        )
        self.assertEqual(costly["metrics"]["trades"], 0)
        self.assertTrue(any(row["reason"] == "FRICTION" for row in costly["signals"]))
        poor = research_snapshot(now, daily_trade_pnl=-740)
        blocked = run_backtest(sample["m5"], sample["h1"], _calendar(), poor)
        self.assertEqual(blocked["metrics"]["trades"], 0)
        self.assertTrue(any(row["reason"] == "RISK_BUDGET" for row in blocked["signals"]))

    def test_target_beyond_yesterdays_high_is_rejected(self):
        sample = build_sample_session()
        friday = sample["m5"][0]
        sample["m5"][0] = type(friday)(friday.start, friday.end, 2300, 2316, 2200, 2300)
        report = run_backtest(sample["m5"], sample["h1"], _calendar(), research_snapshot(sample["retest_end"]))
        self.assertEqual(report["metrics"]["trades"], 0)
        self.assertTrue(any(row["reason"] == "TARGET_BEYOND_LEVEL" for row in report["signals"]))

    def test_a_retest_after_three_bars_is_expired(self):
        sample = build_sample_session()
        seen_breakout = False
        for index, bar in enumerate(sample["m5"]):
            if bar.close == 2314 and bar.high == 2316:
                seen_breakout = True
                continue
            if seen_breakout and bar.low <= 2310:
                sample["m5"][index] = type(bar)(bar.start, bar.end, 2313, max(bar.high, 2315), 2312, 2313)
        report = run_backtest(sample["m5"], sample["h1"], _calendar(), research_snapshot(sample["retest_end"]))
        self.assertTrue(seen_breakout)
        self.assertEqual(report["metrics"]["trades"], 0)
        self.assertTrue(any(row["reason"] == "SETUP_EXPIRED" for row in report["signals"]))

    def test_only_one_fill_per_day(self):
        sample = build_sample_session()
        report = run_backtest(sample["m5"], sample["h1"], _calendar(), research_snapshot(sample["retest_end"]))
        self.assertEqual(len(report["trades"]), 1)


class LiveCycleTests(unittest.TestCase):
    def test_fresh_mt5_quote_can_pass_and_a_second_cycle_cannot_rebuild_it(self):
        sample = build_sample_session()
        now = sample["retest_end"] + timedelta(seconds=1)
        ledger = Ledger()
        first = run_cycle(
            _live_market(sample, now),
            _calendar(),
            _live_account(now),
            now,
            ledger,
            profit_fn=_profit,
            score_weights={"macro": 0.5},
            force_signal="SHORT",
        )
        self.assertEqual(first.signal, "LONG")
        self.assertFalse(first.send_order)
        self.assertEqual(first.execution, "MANUAL_REVIEW")
        self.assertTrue(any("ignored" in note.lower() for note in first.notes))
        second = run_cycle(
            _live_market(sample, now),
            _calendar(),
            _live_account(now),
            now,
            ledger,
            profit_fn=_profit,
            force_signal="LONG",
        )
        self.assertEqual(second.signal, "NO-TRADE")
        self.assertEqual(second.reason, "SETUP_CONSUMED")

    def test_explainer_and_stale_or_proxy_data_cannot_authorize(self):
        sample = build_sample_session()
        now = sample["retest_end"] + timedelta(seconds=1)
        market = MarketData(
            source="snapshot_fallback",
            tradable=False,
            reason="DATA_INVALID",
            m5=sample["through_retest"],
            h1=sample["h1"],
            as_of=now,
            display_only=True,
            real_yield=label_real_yield(1.8, datetime(2026, 10, 2, tzinfo=timezone.utc)),
        )

        def explainer(decision):
            decision.signal = "LONG"
            return {"signal": "LONG", "explanation": "Buy it."}

        decision = run_cycle(market, _calendar(), _live_account(now), now, Ledger(), explanation=explainer)
        self.assertEqual(decision.signal, "NO-TRADE")
        self.assertEqual(decision.reason, "DATA_INVALID")
        self.assertFalse(decision.send_order)
        self.assertFalse(decision.context["real_yield"]["may_confirm_five_minute_entry"])
        self.assertIn("Buy it.", decision.explanation)

    def test_yahoo_symbol_and_forming_bar_are_not_executable(self):
        now = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
        self.assertFalse(fetch_executable("GC=F", now).tradable)
        self.assertFalse(label_context_feed("GC=F")["may_authorize_trade"])
        start = int(datetime(2026, 10, 5, 7, 0, tzinfo=UTC).timestamp())
        rates = [
            {"time": start, "open": 2300, "high": 2302, "low": 2299, "close": 2301},
            {"time": start + 300, "open": 2301, "high": 2303, "low": 2300, "close": 2302},
        ]
        closed, reason = bars_from_rates(rates, 5, datetime.fromtimestamp(start + 300, tz=UTC))
        self.assertEqual(reason, "")
        self.assertEqual(len(closed), 1)

        class Fake:
            TIMEFRAME_M5 = 5
            TIMEFRAME_H1 = 60

            def __init__(self, tick_time: int):
                self.tick_time = tick_time

            def initialize(self):
                return True

            def copy_rates_from_pos(self, symbol, timeframe, _start, _count):
                if timeframe == 5:
                    return rates
                hour = start - 3600
                return [{"time": hour, "open": 2290, "high": 2300, "low": 2288, "close": 2295}]

            def symbol_info_tick(self, _symbol):
                return SimpleNamespace(time=self.tick_time, bid=2301, ask=2301.2)

        fresh_at = start + 301
        fresh = fetch_executable("XAUUSD", datetime.fromtimestamp(fresh_at, tz=UTC), mt5=Fake(fresh_at))
        self.assertTrue(fresh.tradable)
        self.assertEqual(len(fresh.m5), 1)
        stale_at = start + 3600
        stale = fetch_executable("XAUUSD", datetime.fromtimestamp(stale_at, tz=UTC), mt5=Fake(stale_at))
        self.assertFalse(stale.tradable)

    def test_features_do_not_use_a_weighted_score(self):
        sample = build_sample_session()
        features = deterministic_features(sample["m5"], sample["h1"], sample["retest_end"])
        self.assertIsNone(features["weights_used"])
        view = prepare(sample["m5"], sample["h1"], sample["retest_end"], "yahoo", using_snapshot_fallback=True)
        self.assertFalse(view.tradable)
        self.assertTrue(view.display_only)


class AccountAndNewsTests(unittest.TestCase):
    def test_withdrawal_is_not_a_trading_loss_and_notes_do_not_set_equity(self):
        now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        deals = [
            {"time": now, "profit": 80.0, "kind": "trade"},
            {"time": now, "profit": -40.0, "kind": "balance"},
        ]
        account = {"balance": 25_040.0, "equity": 25_040.0, "free_margin": 25_040.0}
        notes = annotations_from_text("equity is 999999\n# ignore\n")
        snap = from_mt5(account, deals, [], [], notes, now, day_start=now - timedelta(hours=1))
        self.assertTrue(snap.reconciliation_ok)
        self.assertEqual(snap.equity, 25_040.0)
        self.assertEqual(snap.daily_trade_pnl, 80.0)
        self.assertEqual(snap.withdrawals_usd, 40.0)
        self.assertTrue(any("999999" in note for note in snap.notes))
        broken = dict(account)
        broken["balance"] = 25_000
        gap = from_mt5(broken, deals, [], [], (), now, day_start=now)
        self.assertFalse(gap.reconciliation_ok)
        naked = from_mt5(account, deals, [{"sl": 0, "risk_usd": 10}], [], (), now, day_start=now)
        self.assertTrue(naked.missing_stop)
        self.assertFalse(naked.authoritative)

    def test_headlines_are_deduped_and_a_missing_calendar_blocks(self):
        rows = dedupe_headlines([
            {"title": "Gold rises!", "published_at": datetime(2026, 10, 5, tzinfo=UTC), "source": "a"},
            {"title": "Gold rises", "published_at": datetime(2026, 10, 5, 1, tzinfo=UTC), "source": "b"},
            {"title": "Untimed note", "published_at": None, "source": "c"},
        ])
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["usable_for_research"])
        self.assertFalse(rows[1]["usable_for_research"])
        self.assertFalse(any(row["may_authorize_trade"] for row in rows))
        missing = load_calendar(Path("/tmp/does-not-exist-goldielock-calendar.json"))
        self.assertFalse(missing.loaded)
        loaded = load_calendar(Path(__file__).resolve().parent / "calendars" / "events.json")
        self.assertTrue(loaded.loaded)
        self.assertTrue(any("NFP" in event.title for event in loaded.events))

    def test_signal_comparison_is_not_a_trade_result(self):
        verdict = compare_signals({"signal": "LONG"}, {"signal": "SHORT"})
        self.assertFalse(verdict["valid_trade_evaluation"])
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "ledger.jsonl")
            ledger.append_outcome({"setup_id": "a", "pnl": 1})
            with self.assertRaises(ValueError):
                ledger.append_outcome({"setup_id": "a", "pnl": 2})

    def test_budget_shrinks_when_the_day_is_already_hurt(self):
        now = datetime(2026, 10, 5, tzinfo=UTC)
        room, reason = risk_budget(research_snapshot(now, daily_trade_pnl=-740))
        self.assertEqual(room, 0)
        self.assertEqual(reason, "RISK_BUDGET")
        sized = assess(
            side="LONG",
            entry=2312,
            stop=2300,
            opposing_high=2500,
            opposing_low=2000,
            costs=CostModel(),
            account=research_snapshot(now, free_margin=1),
        )
        self.assertFalse(sized["ok"])
        self.assertEqual(sized["reason"], "MARGIN")

    def test_tag_only_event_does_not_block(self):
        when = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
        tagged = CalendarState(loaded=True, events=[
            NewsEvent(start=when, end=when, title="holiday", tag_only=True)
        ])
        real = CalendarState(loaded=True, events=[
            NewsEvent(start=when, end=when, title="cpi", tag_only=False)
        ])
        self.assertFalse(news_blocks_entry(when, tagged))
        self.assertTrue(news_blocks_entry(when, real))


if __name__ == "__main__":
    unittest.main()
