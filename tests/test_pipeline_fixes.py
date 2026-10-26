"""Checks for the repaired gold advisory. These do not call a broker or a model."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from broker_account_feed import unknown_account
from decision_contract import legacy_signal_label, render_published_text, screen_signal_name
from fetch_intraday import annotate_candle_lifecycle, build_independent_chart_assessment
from fetch_market_data import describe_external_quote
from macro_relevance import (
    admit_observation,
    assess_macro,
    canonicalize_articles,
    label_series,
    reactivation_allowed,
)
from performance_ledger import review_historical_rows
from pipeline_runner import (
    account_context_from_broker,
    evaluate_proposal,
    freeze_snapshot,
    publish_evaluation,
)
from prop_account_tracker import load_trade_backlog_rows
from trade_plan_state import publish_plan
from trade_validation import loss_headroom, net_reward_to_risk, server_trading_day, validate_proposal

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def _account() -> dict:
    return {
        "known": True,
        "equity": 25000.0,
        "daily_start_equity": 25000.0,
        "equity_floor": 23500.0,
        "daily_loss_hard": 750.0,
        "additional_loss_to_stops": 0.0,
        "internal_reserve": 25.0,
        "floating_pnl": 0.0,
        "symbol_spec": {"contract_size": 100.0, "volume_min": 0.01, "volume_step": 0.01},
    }


def _profile() -> dict:
    return {
        "programme": "FundedNext Stellar 1-Step",
        "automation_permissions": "prohibited",
        "order_submission": False,
        "server_reset_schedule": {"timezone": "Europe/Athens", "hour": 0, "minute": 0},
    }


def _quote(now: datetime, ask: float = 110.0) -> dict:
    return {
        "source": "mt5",
        "role": "broker_executable",
        "execution_eligible": True,
        "is_fallback": False,
        "bid": ask - 0.1,
        "ask": ask,
        "last": ask,
        "observation_time": now.isoformat(),
        "received_time": now.isoformat(),
    }


def _proposal(**overrides) -> dict:
    proposal = {
        "schema_version": "gold-decision-1",
        "snapshot_id": "snap-1",
        "decision": "PROPOSE_LONG",
        "side": "LONG",
        "setup_family": "trend_pullback_continuation",
        "proposed_holding_horizon": "intraday",
        "entry_low": 110.0,
        "entry_high": 110.0,
        "trigger": "LIMIT_PULLBACK",
        "stop_reference": 100.0,
        "target_reference": 132.0,
        "stop_evidence_id": "stop-1",
        "target_evidence_id": "target-1",
        "requested_validity_seconds": 600,
        "invalidation_conditions": "A finished 15-minute bar back through the level.",
        "chart_assessment_id": "chart-1",
        "macro_assessment_id": "macro-1",
        "macro_role": "BACKGROUND_ONLY",
        "relevant_event_ids": [],
        "observed_response_evidence_ids": [],
        "supporting_evidence_ids": ["stop-1"],
        "contradicting_evidence_ids": [],
        "macro_chart_relationship": "INDEPENDENT",
        "reconciliation_summary": "The chart setup does not need a macro push.",
        "confidence_label": "moderate",
    }
    proposal.update(overrides)
    return proposal


def _check(proposal, now=NOW, **kwargs):
    snapshot = {"snapshot_id": "snap-1", "frozen_at": now.isoformat()}
    chart = {
        "chart_assessment_id": "chart-1",
        "evidence_ids": ["stop-1", "target-1", "evt-1"],
        "forming_evidence_ids": ["forming-15m-0"],
    }
    macro = {
        "macro_assessment_id": "macro-1",
        "evidence_ids": ["evt-1"],
        "macro_role": "BACKGROUND_ONLY",
        "execution_restriction": False,
    }
    macro.update(kwargs.pop("macro", {}))
    chart.update(kwargs.pop("chart", {}))
    snapshot.update(kwargs.pop("snapshot", {}))
    return validate_proposal(
        proposal,
        snapshot=snapshot,
        chart=chart,
        macro=macro,
        account=kwargs.pop("account", _account()),
        quote=kwargs.pop("quote", _quote(now)),
        now=now,
        mode=kwargs.pop("mode", "APPROVED_MANUAL_ADVISORY"),
        profile=kwargs.pop("profile", _profile()),
        known_plan_expiry=kwargs.pop("known_plan_expiry", None),
    )


class DecisionTests(unittest.TestCase):
    def test_valid_plan_can_be_advisory(self):
        result = _check(_proposal())
        self.assertEqual(result["status"], "VALIDATED_ADVISORY")
        self.assertGreaterEqual(result["net_rr"], 2.0)
        self.assertEqual(result["legacy_signal"], "LONG")
        self.assertNotIn("BACKGROUND", " ".join(result["reasons"]))

    def test_missing_broker_still_prints_the_screen_signal(self):
        text = render_published_text({
            "status": "RESEARCH_NOTE",
            "proposal": {
                "decision": "PROPOSE_LONG",
                "entry_low": 4100,
                "entry_high": 4102,
                "stop_reference": 4088,
                "target_reference": 4130,
                "setup_family": "trend_pullback_continuation",
            },
            "blocking_reasons": ["DATA_UNAVAILABLE", "ACCOUNT_UNVERIFIED"],
            "legacy_signal": "NEUTRAL",
        })
        self.assertEqual(screen_signal_name({
            "status": "RESEARCH_NOTE",
            "proposal": {"decision": "PROPOSE_LONG"},
        }), "LONG")
        self.assertIn("SIGNAL: LONG", text)
        self.assertIn("No broker is connected", text)
        self.assertNotIn("This is not an active recommendation", text)

    def test_neutral_card_includes_the_models_reason(self):
        text = render_published_text({
            "status": "MODEL_NO_TRADE",
            "proposal": {
                "decision": "NO_TRADE",
                "reconciliation_summary": "The 15-minute chart has no fresh retest, so the short is too late.",
                "confidence_label": "HIGH confidence in staying out",
            },
            "legacy_signal": "NEUTRAL",
        })
        self.assertIn("SIGNAL: NO TRADE", text)
        self.assertIn("REASON", text)
        self.assertIn("The 15-minute chart has no fresh retest, so the short is too late.", text)
        self.assertIn("Confidence: HIGH confidence in staying out", text)

    def test_malformed_output_is_not_a_card(self):
        evaluated = evaluate_proposal(
            "the model wrote a story instead of JSON",
            snapshot={"snapshot_id": "snap-1", "frozen_at": NOW.isoformat()},
            chart={"chart_assessment_id": "chart-1", "evidence_ids": []},
            macro={"macro_assessment_id": "macro-1", "evidence_ids": []},
            account=_account(),
            quote=_quote(NOW),
            now=NOW,
            mode="APPROVED_MANUAL_ADVISORY",
            profile=_profile(),
        )
        self.assertEqual(evaluated["validation"]["status"], "NON_ACTIONABLE")
        text = render_published_text({
            "status": "NON_ACTIONABLE",
            "blocking_reasons": ["MODEL_OUTPUT_INVALID"],
            "legacy_signal": "NEUTRAL",
        })
        self.assertIn("not an active recommendation", text)

    def test_unknown_evidence_is_rejected(self):
        result = _check(_proposal(stop_evidence_id="invented-level"))
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("MODEL_OUTPUT_INVALID", result["reasons"])

    def test_snapshot_mismatch_is_rejected(self):
        result = _check(_proposal(snapshot_id="snap-other"))
        self.assertIn("SNAPSHOT_MISMATCH", result["reasons"])

    def test_model_size_cannot_override_the_engine(self):
        evaluated = evaluate_proposal(
            json.dumps(_proposal(risk_usd=99999, ounces=40)),
            snapshot={"snapshot_id": "snap-1", "frozen_at": NOW.isoformat()},
            chart={"chart_assessment_id": "chart-1", "evidence_ids": ["stop-1", "target-1"], "forming_evidence_ids": []},
            macro={"macro_assessment_id": "macro-1", "evidence_ids": [], "execution_restriction": False},
            account=_account(),
            quote=_quote(NOW),
            now=NOW,
            mode="APPROVED_MANUAL_ADVISORY",
            profile=_profile(),
        )
        sizing = evaluated["validation"]["sizing"]
        self.assertLess(sizing["risk_usd"], 1000)
        self.assertEqual(evaluated["validation"]["status"], "VALIDATED_ADVISORY")
        forced = _check(_proposal(sizing_authoritative=True, risk_usd=99999))
        self.assertIn("MODEL_OUTPUT_INVALID", forced["reasons"])
        self.assertEqual(forced["legacy_signal"], "NEUTRAL")

    def test_external_price_is_not_executable(self):
        quote = describe_external_quote(110.0, source="GC=F", is_fallback=True, received_time=NOW.isoformat())
        result = _check(_proposal(), quote=quote)
        self.assertNotEqual(result["status"], "VALIDATED_ADVISORY")
        self.assertIn("DATA_UNAVAILABLE", result["reasons"])
        self.assertFalse(quote["execution_eligible"])

    def test_forming_bar_cannot_confirm_a_level(self):
        result = _check(_proposal(stop_evidence_id="forming-15m-0"))
        self.assertIn("STRUCTURE_UNSUPPORTED", result["reasons"])

    def test_unknown_account_is_not_zero_exposure(self):
        broker = unknown_account("not connected")
        context = account_context_from_broker(broker, _profile(), {"current_equity_usd": 23840})
        self.assertFalse(context["known"])
        self.assertIsNone(context["floating_pnl"])
        self.assertIsNone(context["additional_loss_to_stops"])
        self.assertFalse(context["manual_equity_is_broker_equity"])
        result = _check(_proposal(), account=context)
        self.assertIn("ACCOUNT_UNVERIFIED", result["reasons"])
        self.assertNotEqual(result["status"], "VALIDATED_ADVISORY")


class TimingAndRiskTests(unittest.TestCase):
    def test_stale_plan_fails_even_when_reward_looks_good(self):
        frozen = NOW - timedelta(minutes=20)
        result = _check(_proposal(), now=NOW, snapshot={"frozen_at": frozen.isoformat()})
        self.assertIn("PLAN_EXPIRED", result["reasons"])
        self.assertGreaterEqual(net_reward_to_risk("LONG", 110, 100, 132), 2.0)

    def test_entry_outside_the_zone_is_rejected(self):
        result = _check(_proposal(trigger="MARKET_NOW"), quote=_quote(NOW, ask=200))
        self.assertIn("ENTRY_OUTSIDE_ZONE", result["reasons"])

    def test_retry_cannot_extend_the_deadline(self):
        original = (NOW + timedelta(seconds=30)).isoformat()
        plan_id = "snap-1:PROPOSE_LONG:trend_pullback_continuation"
        result = _check(_proposal(), known_plan_expiry={plan_id: original})
        self.assertIn("DUPLICATE_OPPORTUNITY", result["reasons"])
        self.assertEqual(result["expires_at"], original)

    def test_net_reward_below_two_is_rejected(self):
        result = _check(_proposal(target_reference=120))
        self.assertIn("NET_RR_BELOW_MINIMUM", result["reasons"])
        self.assertLess(net_reward_to_risk("LONG", 110, 100, 120), 2.0)

    def test_existing_exposure_reduces_size(self):
        account = _account()
        account["additional_loss_to_stops"] = 720
        result = _check(_proposal(), account=account)
        self.assertIn("RISK_HEADROOM_INSUFFICIENT", result["reasons"])

    def test_floating_loss_is_not_subtracted_twice(self):
        room = loss_headroom({
            "known": True,
            "equity": 24800.0,
            "daily_start_equity": 25000.0,
            "equity_floor": 23500.0,
            "daily_loss_hard": 750.0,
            "additional_loss_to_stops": 150.0,
            "internal_reserve": 25.0,
            "floating_pnl": -200.0,
        })
        self.assertAlmostEqual(room, 375.0)

    def test_reset_uses_the_broker_clock(self):
        late_new_york = datetime(2026, 10, 12, 23, 30, tzinfo=ZoneInfo("America/New_York"))
        broker_day = server_trading_day(late_new_york, {"timezone": "Europe/Athens", "hour": 0, "minute": 0})
        self.assertEqual(broker_day, "2026-10-13")
        self.assertNotEqual(broker_day, late_new_york.date().isoformat())


class MacroTests(unittest.TestCase):
    def test_important_news_cannot_force_direction(self):
        assessed = assess_macro(
            [{"title": "CPI surprises higher", "published_at": NOW.isoformat(), "source": "test"}],
            [],
            ["DFII10"],
            now=NOW,
            received_time=NOW.isoformat(),
        )
        self.assertIsNone(assessed["direction"])
        self.assertFalse(any(item["can_force_direction"] for item in assessed["items"]))

    def test_repeated_headline_does_not_add_weight(self):
        articles = canonicalize_articles(
            [
                {"title": "Fed speaker repeats the same line", "published_at": "2026-10-12T14:00:00+00:00"},
                {"title": "Fed speaker repeats the same line", "published_at": "2026-10-12T14:05:00+00:00"},
            ],
            NOW.isoformat(),
        )
        self.assertEqual(sum(1 for item in articles if item["counts_as_new_evidence"]), 1)
        self.assertFalse(articles[1]["counts_as_new_evidence"])

    def test_missing_response_stays_unknown(self):
        assessed = assess_macro(
            [{"title": "Payrolls released", "published_at": NOW.isoformat()}],
            [],
            [],
            now=NOW,
            received_time=NOW.isoformat(),
        )
        self.assertEqual(assessed["items"][0]["observed_response"], "unknown")

    def test_background_macro_does_not_reject_a_valid_setup(self):
        result = _check(_proposal())
        self.assertEqual(result["status"], "VALIDATED_ADVISORY")

    def test_execution_risk_blocks_even_when_direction_agrees(self):
        result = _check(
            _proposal(macro_chart_relationship="AGREE", macro_role="EXECUTION_RISK"),
            macro={"execution_restriction": True, "macro_role": "EXECUTION_RISK"},
        )
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("EVENT_RESTRICTION", result["reasons"])
        self.assertTrue(result["directional_thesis_potentially_valid"])

    def test_disagreement_is_not_an_automatic_no_trade(self):
        result = _check(_proposal(
            macro_chart_relationship="DIVERGE",
            reconciliation_summary="Price is holding the break even though the dollar is firm.",
        ))
        self.assertEqual(result["status"], "VALIDATED_ADVISORY")
        self.assertNotIn("MODEL_WAIT_MACRO_CHART_DIVERGENCE", result["reasons"])

    def test_disagreement_without_an_explanation_is_rejected(self):
        result = _check(_proposal(macro_chart_relationship="DIVERGE", reconciliation_summary=""))
        self.assertIn("MODEL_OUTPUT_INVALID", result["reasons"])

    def test_related_yields_are_one_family(self):
        assessed = assess_macro([], [], ["DFII10", "DGS10", "T10YIE"], now=NOW, received_time=NOW.isoformat())
        self.assertFalse(assessed["related_groups"][0]["counts_as_independent_votes"])
        self.assertFalse(label_series("DFII10")["may_confirm_five_minute_entry"])

    def test_old_event_needs_new_evidence_to_come_back(self):
        prior = {"event_id": "evt-1", "observation_time": "2026-10-12T12:00:00+00:00"}
        reprint = {"event_id": "evt-1", "observation_time": "2026-10-12T12:00:00+00:00", "counts_as_new_evidence": False}
        fresh = {
            "event_id": "evt-1",
            "observation_time": "2026-10-12T16:00:00+00:00",
            "counts_as_new_evidence": True,
            "new_evidence_note": "A new release, not a reprint.",
        }
        self.assertFalse(reactivation_allowed(prior, reprint))
        self.assertTrue(reactivation_allowed(prior, fresh))

    def test_future_observation_cannot_enter_an_earlier_snapshot(self):
        future = {"observation_time": (NOW + timedelta(hours=2)).isoformat()}
        self.assertFalse(admit_observation(NOW, future))
        frozen = freeze_snapshot({}, {"intervals": {}}, [future], now=NOW, sequence=1)
        self.assertEqual(frozen["events"], [])
        self.assertTrue(frozen["legacy_comparison_only"]["note"])


class ChartAndLedgerTests(unittest.TestCase):
    def test_forming_bar_is_separate_from_finished_bars(self):
        start = NOW - timedelta(minutes=2)
        records, _warning = annotate_candle_lifecycle(
            [{
                "ts": start.isoformat(),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 0,
            }],
            "5m",
            now=NOW,
        )
        self.assertFalse(records[0]["is_closed"])
        finished, _warning = annotate_candle_lifecycle(
            [{
                "ts": (NOW - timedelta(minutes=10)).isoformat(),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 0,
            }],
            "5m",
            now=NOW,
        )
        self.assertTrue(finished[0]["is_closed"])
        chart = build_independent_chart_assessment({
            "intervals": {"5m": {"candles": records + finished}}
        }, now=NOW)
        self.assertIn("forming-5m-0", chart["forming_evidence_ids"])
        self.assertTrue(chart["evidence_ids"])
        self.assertFalse(chart["news_used"])
        self.assertFalse(chart["execution_eligible"])

    def test_historical_ledger_keeps_the_known_gaps(self):
        rows = load_trade_backlog_rows(ROOT / "trade_backlog.txt")
        review = review_historical_rows(rows)
        by_trade = {item.get("trade_id") or item.get("to_trade"): item for item in review["findings"]}
        self.assertFalse(review["invented_breach"])
        self.assertIsNone(review["breach_sequence"])
        self.assertFalse(review["original_files_modified"])
        self.assertFalse(by_trade["TRADE_024"]["model_attribution"])
        self.assertEqual(by_trade["TRADE_024"]["classification"], "recorded_manual_win")
        self.assertTrue(by_trade["TRADE_010"]["risk_field_unreliable"])
        self.assertFalse(by_trade["TRADE_010"]["timely"])
        self.assertGreater(by_trade["TRADE_010"]["gross_r"], 2.0)
        jump = by_trade["TRADE_019"]
        self.assertEqual(jump["classification"], "unreconciled")
        self.assertEqual(jump["unexplained_residual"], 4841.0)
        self.assertIsNone(jump["invented_cause"])

    def test_older_run_cannot_replace_a_newer_card(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = publish_plan({"sequence": 2, "status": "VALIDATED_ADVISORY", "plan_id": "new"}, root=root)
            second = publish_plan({"sequence": 1, "status": "VALIDATED_ADVISORY", "plan_id": "old"}, root=root)
            self.assertTrue(first["published"])
            self.assertFalse(second["published"])
            self.assertEqual(second["reason"], "OLDER_RUN")
            saved = json.loads((root / "gold_authoritative_plan.json").read_text())
            self.assertEqual(saved["sequence"], 2)

    def test_chat_file_cannot_publish_its_own_card(self):
        text = (ROOT / "day_trading_insights_chat.py").read_text(encoding="utf-8")
        self.assertIn("CHAT_CAN_PUBLISH = False", text)
        self.assertNotIn("def generate_trade_signal", text)
        self.assertNotIn("order_send", text)
        broker = (ROOT / "broker_account_feed.py").read_text(encoding="utf-8")
        self.assertNotIn("order_send", broker)
        self.assertNotIn("verify=False", broker)

    def test_publish_keeps_raw_ideas_out_of_the_advisory_pile(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            publish_evaluation(
                {
                    "sequence": 5,
                    "status": "REJECTED",
                    "plan_id": None,
                    "snapshot_id": "snap-x",
                    "proposal": {"decision": "PROPOSE_LONG"},
                    "blocking_reasons": ["NET_RR_BELOW_MINIMUM"],
                    "legacy_signal": "NEUTRAL",
                    "text": "not a card",
                    "expiry_registry": {},
                },
                root=folder,
            )
            advisory = folder / "performance_ledger" / "validated_advisory.jsonl"
            raw = folder / "performance_ledger" / "raw_proposal.jsonl"
            self.assertFalse(advisory.exists())
            self.assertTrue(raw.exists())
            card = json.loads((folder / "gold_authoritative_plan.json").read_text())
            self.assertEqual(card["status"], "REJECTED")
            rendered = render_published_text(card)
            self.assertIn("not an active recommendation", rendered)
            self.assertEqual(legacy_signal_label("PROPOSE_LONG", actionable=False), "NEUTRAL")


class NewsSourceTests(unittest.TestCase):
    def test_hot_us_inflation_print_reads_as_a_gold_headwind(self):
        from additional_news import data_surprise

        surprise = data_surprise("US September PPI 6.3% vs 5.3% expected")
        self.assertEqual(surprise["vs_expected"], "ABOVE")
        self.assertTrue(surprise["us_release"])
        self.assertIn("headwind for gold", surprise["textbook_gold_read"])
        self.assertFalse(surprise["is_trade_direction"])

    def test_higher_jobless_claims_read_as_supportive(self):
        from additional_news import data_surprise

        surprise = data_surprise("US initial jobless claims 230K vs 210K estimate")
        self.assertEqual(surprise["vs_expected"], "ABOVE")
        self.assertIn("supportive for gold", surprise["textbook_gold_read"])

    def test_non_us_print_is_marked_indirect(self):
        from additional_news import data_surprise

        surprise = data_surprise("Canada August manufacturing sales +1.5% vs +1.1% expected")
        self.assertFalse(surprise["us_release"])
        self.assertIn("indirect", surprise["textbook_gold_read"])
        self.assertIsNone(data_surprise("Gold slips as dollar firms"))

    def test_calendar_cannot_crowd_out_the_news(self):
        from additional_news import _balanced_mix

        calendar = [{"title": f"cal {i}", "relevance_score": 40} for i in range(20)]
        gold = [{"title": f"gold {i}", "relevance_score": 12} for i in range(5)]
        fed = [{"title": "Fed speech", "relevance_score": 9}]
        mix = _balanced_mix(
            {"Forex Factory Calendar": calendar, "FXStreet Analysis": gold, "Federal Reserve: speeches": fed},
            max_total=15,
        )
        titles = [a["title"] for a in mix]
        self.assertLessEqual(sum(t.startswith("cal") for t in titles), 6)
        self.assertIn("Fed speech", titles)
        self.assertEqual(sum(t.startswith("gold") for t in titles), 5)


class AutopilotTests(unittest.TestCase):
    def _pending(self, **overrides):
        pending = {
            "side": "LONG", "entry_low": 4140.0, "entry_high": 4142.0, "stop": 4130.0, "target": 4165.0,
            "trigger": "LIMIT_PULLBACK", "setup_family": "trend_pullback_continuation",
            "expires_at": "2026-10-19T18:15:00+00:00", "plan_id": "p", "snapshot_id": "snap-x",
            "reason": "Pullback into support, held the 1H trend.",
        }
        pending.update(overrides)
        return pending

    def test_recheck_levels_from_the_cards_wording(self):
        from decision_contract import recheck_levels

        levels = recheck_levels({
            "invalidation_conditions": (
                "No pending order; reassess after a completed XAUUSD-equivalent 15M close above $4167.70 "
                "or below $4129.40 using a refreshed proxy basis, or at the next 15M close."
            )
        })
        self.assertEqual(levels, {"above": 4167.70, "below": 4129.40})

    def test_structured_recheck_levels_win_over_prose(self):
        from decision_contract import DECISION_JSON_SCHEMA, recheck_levels

        self.assertIn("recheck_above", DECISION_JSON_SCHEMA["required"])
        levels = recheck_levels({
            "recheck_above": 4170, "recheck_below": 4120,
            "invalidation_conditions": "close above $4,200.00 or below $4,100.00",
        })
        self.assertEqual(levels, {"above": 4170.0, "below": 4120.0})
        self.assertEqual(recheck_levels({"recheck_above": 4100, "recheck_below": 4200}), {"above": None, "below": None})

    def test_card_shows_the_recheck_line(self):
        text = render_published_text({
            "status": "MODEL_WAIT",
            "proposal": {"decision": "WAIT", "recheck_above": 4167.7, "recheck_below": 4129.4},
        })
        self.assertIn("RECHECK: run again when a 15M candle closes above 4167.7 or below 4129.4", text)

    def test_paper_fill_rules(self):
        from paper_trader import fill_price

        pending = self._pending()
        self.assertEqual(fill_price(pending, 4141.0), 4141.0)
        self.assertIsNone(fill_price(pending, 4150.0))
        self.assertEqual(fill_price(pending, 4135.0), 4135.0)
        self.assertIsNone(fill_price(pending, 4129.0))
        breakout = self._pending(trigger="STOP_BREAKOUT")
        self.assertIsNone(fill_price(breakout, 4135.0))
        self.assertEqual(fill_price(breakout, 4145.0), 4145.0)
        short = self._pending(side="SHORT", entry_low=4140, entry_high=4142, stop=4152, target=4115)
        self.assertEqual(fill_price(short, 4146.0), 4146.0)

    def test_paper_exit_and_journal_row(self):
        import tempfile
        import paper_trader
        from prop_account_tracker import compute_prop_account_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            original = paper_trader.BACKLOG_CSV
            paper_trader.BACKLOG_CSV = Path(tmp) / "trade_backlog.csv"
            paper_trader.BACKLOG_CSV.write_text(
                "DATE,TRADE_ID,SIGNAL,ENTRY,STOP_LOSS,TAKE_PROFIT,OUTCOME,R_MULTIPLE,PNL_USD,RISK_USD,XAU_OZ,"
                "TV_LEVERAGE,EFFECTIVE_LEVERAGE,CUMULATIVE_PNL,EQUITY,DAILY_PNL,NOTES\n"
                "2026-09-15,TRADE_028,LONG,4372.50,4352.50,4412.50,LOSS,2.0,-180.00,,,,,4330.36,23840,-348.00,\n",
                encoding="utf-8",
            )
            try:
                trade = paper_trader.open_trade(self._pending(), 4140.0, NOW, paper_trader.next_trade_id())
                self.assertEqual(trade["trade_id"], "PAPER_001")
                self.assertEqual(trade["ounces"], 12.0)
                self.assertIsNone(paper_trader.check_exit(trade, 4150.0))
                self.assertEqual(paper_trader.check_exit(trade, 4128.0), ("SL", 4128.0))
                self.assertEqual(paper_trader.check_exit(trade, 4170.0), ("TP", 4165.0))
                row = paper_trader.close_trade(trade, "TP", 4165.0, NOW)
                self.assertEqual(row["OUTCOME"], "WIN")
                self.assertEqual(row["PNL_USD"], f"{25 * 12 - 0.40 * 12:.2f}")
                self.assertEqual(paper_trader.next_trade_id(), "PAPER_002")
                text = paper_trader.BACKLOG_CSV.read_text(encoding="utf-8")
                self.assertIn("PAPER_001", text)
                self.assertIn("TRADE_028", text)

                rows = load_trade_backlog_rows(paper_trader.BACKLOG_CSV)
                snapshot = compute_prop_account_snapshot(rows, today="2026-10-19")
                self.assertEqual(snapshot["current_equity_usd"], 23840.0)
                self.assertIn("Closed paper trades: 1", paper_trader.paper_record_text())
            finally:
                paper_trader.BACKLOG_CSV = original

    def test_market_hours(self):
        from gold_autopilot import market_open

        ny = ZoneInfo("America/New_York")
        self.assertTrue(market_open(datetime(2026, 10, 19, 10, 0, tzinfo=ny)))
        self.assertFalse(market_open(datetime(2026, 10, 19, 17, 30, tzinfo=ny)))
        self.assertFalse(market_open(datetime(2026, 10, 17, 12, 0, tzinfo=ny)))
        self.assertFalse(market_open(datetime(2026, 10, 18, 12, 0, tzinfo=ny)))
        self.assertTrue(market_open(datetime(2026, 10, 18, 18, 30, tzinfo=ny)))
        self.assertFalse(market_open(datetime(2026, 10, 23, 17, 5, tzinfo=ny)))

    def test_signals_route_to_the_right_phase(self):
        import gold_autopilot

        gold_autopilot._ledger = lambda *args, **kwargs: None
        wait = gold_autopilot.after_signal({"publication": {
            "status": "MODEL_WAIT",
            "proposal": {"decision": "WAIT", "recheck_above": 4167.7, "recheck_below": 4129.4},
        }}, NOW)
        self.assertEqual((wait["phase"], wait["above"], wait["below"]), ("WATCH", 4167.7, 4129.4))

        long = gold_autopilot.after_signal({"publication": {
            "status": "RESEARCH_NOTE",
            "expires_at": "2026-10-12T15:15:00+00:00",
            "snapshot_id": "snap-1",
            "proposal": {
                "decision": "PROPOSE_LONG", "side": "LONG", "entry_low": 4140, "entry_high": 4142,
                "stop_reference": 4130, "target_reference": 4165, "trigger": "LIMIT_PULLBACK",
            },
        }}, NOW)
        self.assertEqual(long["phase"], "PENDING")
        self.assertEqual(long["pending"]["stop"], 4130.0)

        failed = gold_autopilot.after_signal({"publication": {"status": "NON_ACTIONABLE"}}, NOW)
        self.assertEqual(failed["phase"], "WATCH")
        self.assertIsNone(failed["above"])

    def test_forex_factory_times_now_trigger_the_news_block(self):
        macro = assess_macro(
            [], [{"title": "USD CPI m/m", "impact": "high", "published_at": "10-12-2026 3:20pm"}], [],
            now=NOW, received_time=NOW.isoformat(),
        )
        self.assertTrue(macro["execution_restriction"])


if __name__ == "__main__":
    unittest.main()
