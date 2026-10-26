from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from supply_demand_strat.engine import (
    StrategyConfig,
    _candidate_zones,
    _pattern_in_zone,
    _recent_crossover,
    _trigger_state,
    candlestick_pattern,
    classify_regime,
    completed_candles,
    detect_zones,
    evaluate_setup,
)
from supply_demand_strat.pipeline import (
    _aggregate_one_hour_to_four_hour,
    _apply_freshness_gate,
    _apply_macro_gate,
    _apply_spot_basis,
    _gemini_visible_text,
    _macro_evaluation,
    _macro_request_payload,
)
from supply_demand_strat.macro_sources import (
    _headline_score,
    _series_observations,
)
from supply_demand_strat.structure import (
    add_structure_columns,
    classify_movement_state,
    dynamic_zones,
)


def candles_from_closes(
    closes: list[float],
    *,
    start: datetime | None = None,
    minutes: int = 60,
    half_range: float = 0.5,
) -> list[dict]:
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_ = previous
        candles.append(
            {
                "ts": (start + timedelta(minutes=index * minutes)).isoformat(),
                "open": open_,
                "high": max(open_, close) + half_range,
                "low": min(open_, close) - half_range,
                "close": close,
                "volume": 100 + index,
            }
        )
        previous = close
    return candles


class CompletedCandleTests(unittest.TestCase):
    def test_forming_last_candle_is_removed(self) -> None:
        now = datetime(2026, 1, 1, 12, 7, tzinfo=timezone.utc)
        records = candles_from_closes(
            [100, 101],
            start=datetime(2026, 1, 1, 11, 45, tzinfo=timezone.utc),
            minutes=15,
        )
        self.assertEqual(len(completed_candles(records, "15m", now=now)), 1)


class ZoneDetectionTests(unittest.TestCase):
    def test_detects_fresh_rally_base_rally_demand(self) -> None:
        closes = [100 + index * 0.08 for index in range(35)]
        candles = candles_from_closes(closes, half_range=0.45)
        last = candles[-1]["close"]
        base_start = len(candles)
        candles.extend(
            [
                {
                    "ts": datetime(2026, 1, 3, tzinfo=timezone.utc).isoformat(),
                    "open": last - 0.7,
                    "high": last - 0.5,
                    "low": last - 2.0,
                    "close": last - 1.0,
                    "volume": 90,
                },
                {
                    "ts": datetime(2026, 1, 3, 1, tzinfo=timezone.utc).isoformat(),
                    "open": last - 1.0,
                    "high": last + 2.0,
                    "low": last - 0.8,
                    "close": last + 1.8,
                    "volume": 180,
                },
                {
                    "ts": datetime(2026, 1, 3, 2, tzinfo=timezone.utc).isoformat(),
                    "open": last + 1.8,
                    "high": last + 3.6,
                    "low": last + 1.7,
                    "close": last + 3.4,
                    "volume": 190,
                },
                {
                    "ts": datetime(2026, 1, 3, 3, tzinfo=timezone.utc).isoformat(),
                    "open": last + 3.4,
                    "high": last + 4.8,
                    "low": last + 3.3,
                    "close": last + 4.6,
                    "volume": 200,
                },
                {
                    "ts": datetime(2026, 1, 3, 4, tzinfo=timezone.utc).isoformat(),
                    "open": last + 4.6,
                    "high": last + 5.0,
                    "low": last + 4.3,
                    "close": last + 4.8,
                    "volume": 120,
                },
            ]
        )
        zones = detect_zones(candles, "1h", StrategyConfig())
        demand = [zone for zone in zones if zone["type"] == "DEMAND"]
        self.assertTrue(demand)
        self.assertIn(demand[0]["formation"], {"RBR", "DBR"})
        self.assertFalse(demand[0]["invalidated"])
        self.assertGreaterEqual(demand[0]["departure_atr"], 1.2)
        self.assertLessEqual(demand[0]["departure_end_index"], base_start + 3)

    def test_consolidation_rejects_mid_range_zone(self) -> None:
        zone = {
            "id": "1h-mid-demand",
            "timeframe": "1h",
            "type": "DEMAND",
            "low": 49.0,
            "high": 51.0,
            "touches": 0,
            "score": 0.9,
        }
        candidates = _candidate_zones(
            [zone],
            current_price=50.0,
            atr=2.0,
            regime="CONSOLIDATING",
            movement_state="CONSOLIDATION",
            range_low=0.0,
            range_high=100.0,
            config=StrategyConfig(),
        )
        self.assertEqual(candidates, [])

    def test_seventy_percent_mitigated_zone_is_ineligible(self) -> None:
        zone = {
            "id": "1h-stale-demand",
            "timeframe": "1h",
            "type": "DEMAND",
            "formation": "DBR",
            "low": 5.0,
            "high": 10.0,
            "touches": 1,
            "mitigation_pct": 70.0,
            "score": 0.9,
        }
        candidates = _candidate_zones(
            [zone],
            current_price=8.0,
            atr=2.0,
            regime="CONSOLIDATING",
            movement_state="CONSOLIDATION",
            range_low=0.0,
            range_high=100.0,
            config=StrategyConfig(),
        )
        self.assertEqual(candidates, [])

    def test_fractal_pivot_waits_for_right_side_confirmation(self) -> None:
        records = candles_from_closes([100, 99, 98, 99, 100, 101], minutes=15)
        frame = add_structure_columns(
            records,
            atr_period=2,
            pivot_left=2,
            pivot_right=2,
        )
        self.assertTrue(bool(frame.loc[2, "pivot_low"]))
        self.assertFalse(bool(frame.loc[len(frame) - 1, "pivot_high"]))

    def test_zone_tracks_partial_mitigation(self) -> None:
        closes = [100 + index * 0.08 for index in range(35)]
        records = candles_from_closes(closes, half_range=0.45)
        last = records[-1]["close"]
        records.extend(
            [
                {
                    "ts": "2026-01-03T00:00:00+00:00",
                    "open": last - 0.7,
                    "high": last - 0.5,
                    "low": last - 2.0,
                    "close": last - 1.0,
                    "volume": 90,
                },
                {
                    "ts": "2026-01-03T01:00:00+00:00",
                    "open": last - 1.0,
                    "high": last + 2.0,
                    "low": last - 0.8,
                    "close": last + 1.8,
                    "volume": 180,
                },
                {
                    "ts": "2026-01-03T02:00:00+00:00",
                    "open": last + 1.8,
                    "high": last + 3.6,
                    "low": last + 1.7,
                    "close": last + 3.4,
                    "volume": 190,
                },
                {
                    "ts": "2026-01-03T03:00:00+00:00",
                    "open": last + 3.4,
                    "high": last + 4.8,
                    "low": last + 3.3,
                    "close": last + 4.6,
                    "volume": 200,
                },
                {
                    "ts": "2026-01-03T04:00:00+00:00",
                    "open": last + 4.6,
                    "high": last + 5.0,
                    "low": last + 4.3,
                    "close": last + 4.8,
                    "volume": 120,
                },
                {
                    "ts": "2026-01-03T05:00:00+00:00",
                    "open": last + 4.8,
                    "high": last + 4.9,
                    "low": last - 1.35,
                    "close": last + 0.2,
                    "volume": 160,
                },
                {
                    "ts": "2026-01-03T06:00:00+00:00",
                    "open": last + 0.2,
                    "high": last + 1.1,
                    "low": last,
                    "close": last + 0.9,
                    "volume": 130,
                },
            ]
        )
        zones = dynamic_zones(
            records,
            "1h",
            atr_period=14,
            pivot_left=2,
            pivot_right=2,
            impulse_atr=1.5,
        )
        zone = next(
            item
            for item in zones
            if item["type"] == "DEMAND" and item["anchor_pivot_index"] == 35
        )
        self.assertGreater(zone["mitigation_pct"], 0)
        self.assertLess(zone["mitigation_pct"], 70)
        self.assertEqual(zone["freshness_pct"], 100 - zone["mitigation_pct"])

    def test_confirmed_pivot_beyond_zone_invalidates_it(self) -> None:
        closes = [100 + index * 0.08 for index in range(35)]
        closes.extend([100.0, 103.0, 105.0, 107.0, 106.0])
        before_break = candles_from_closes(closes, half_range=0.45)
        initial_zones = dynamic_zones(
            before_break,
            "1h",
            atr_period=14,
            pivot_left=2,
            pivot_right=2,
            impulse_atr=1.5,
        )
        self.assertTrue(
            any(
                zone["type"] == "DEMAND" and zone["anchor_pivot_index"] == 35
                for zone in initial_zones
            )
        )
        closes.extend([98.0, 101.0, 102.0])
        records = candles_from_closes(closes, half_range=0.45)
        zones = dynamic_zones(
            records,
            "1h",
            atr_period=14,
            pivot_left=2,
            pivot_right=2,
            impulse_atr=1.5,
        )
        invalidated_anchor_is_active = any(
            zone["type"] == "DEMAND" and zone["anchor_pivot_index"] == 35
            for zone in zones
        )
        self.assertFalse(invalidated_anchor_is_active)


class RegimeTests(unittest.TestCase):
    def test_uptrend_regime(self) -> None:
        one_hour = candles_from_closes([100 + index * 0.45 for index in range(90)])
        four_hour = candles_from_closes(
            [90 + index * 0.8 for index in range(50)], minutes=240
        )
        regime = classify_regime(one_hour, four_hour, StrategyConfig())
        self.assertEqual(regime["regime"], "UPTREND")
        self.assertEqual(regime["four_hour_bias"], "UP")

    def test_consolidating_regime(self) -> None:
        closes = [100 + (0.1 if index % 2 else -0.1) for index in range(90)]
        one_hour = candles_from_closes(closes)
        four_hour = candles_from_closes([100] * 50, minutes=240)
        regime = classify_regime(one_hour, four_hour, StrategyConfig())
        self.assertEqual(regime["regime"], "CONSOLIDATING")

    def test_grind_is_distinct_from_impulse(self) -> None:
        records = candles_from_closes(
            [100 + index * 0.15 for index in range(90)],
            half_range=0.5,
        )
        state = classify_movement_state(records)
        self.assertEqual(state["movement_state"], "GRIND_UP")
        self.assertIsNone(state["recent_impulse"])


class ConfirmationTests(unittest.TestCase):
    def test_bullish_engulfing(self) -> None:
        candles = [
            {"open": 101, "high": 101.2, "low": 99.8, "close": 100},
            {"open": 99.8, "high": 101.5, "low": 99.6, "close": 101.3},
        ]
        self.assertEqual(candlestick_pattern(candles, 1), "BULLISH_ENGULFING")

    def test_recent_bullish_crossover(self) -> None:
        closes = [100.0] * 25 + [99, 98, 97, 98, 100, 103]
        candles = candles_from_closes(closes, minutes=5)
        crossover = _recent_crossover(
            candles,
            "LONG",
            StrategyConfig(crossover_lookback=5),
        )
        self.assertIsNotNone(crossover)
        self.assertEqual(crossover["direction"], "LONG")


class MacroSourcesTests(unittest.TestCase):
    def test_official_source_outranks_same_media_headline(self) -> None:
        official = {"channel": "central_bank", "tier": 1}
        media = {"channel": "central_bank", "tier": 2}
        title = "Monetary policy rate and inflation outlook"
        self.assertGreater(
            _headline_score(title, "", official),
            _headline_score(title, "", media),
        )

    def test_fred_parser_keeps_each_series_independent(self) -> None:
        observations = _series_observations(
            (
                "observation_date,DFII10,DGS10\n"
                "2026-09-17,2.10,4.90\n"
                "2026-09-18,2.05,\n"
            ).encode()
        )
        self.assertEqual(observations["DFII10"][-1], ("2026-09-18", 2.05))
        self.assertEqual(observations["DGS10"][-1], ("2026-09-17", 4.9))


class PipelineSafetyTests(unittest.TestCase):
    def test_derives_four_hour_candle_from_one_hour_data(self) -> None:
        records = candles_from_closes(
            [100, 101, 102, 103],
            start=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            minutes=60,
        )
        aggregated = _aggregate_one_hour_to_four_hour(records)
        self.assertEqual(len(aggregated), 1)
        self.assertEqual(aggregated[0]["open"], records[0]["open"])
        self.assertEqual(aggregated[0]["close"], records[-1]["close"])
        self.assertEqual(aggregated[0]["high"], max(row["high"] for row in records))

    def test_proxy_only_spot_blocks_actionable_trade(self) -> None:
        raw = {
            "status": "ACTIONABLE",
            "signal": "LONG",
            "current_chart_price": 2500.0,
            "entry": 2501.0,
            "stop_loss": 2490.0,
            "target_1": 2523.0,
            "reward_risk": 2.0,
            "order_type": "BUY_STOP",
            "zones": [],
        }
        converted = _apply_spot_basis(
            raw,
            {
                "price": 2500.0,
                "source": "GC=F chart proxy",
                "fallback": True,
            },
        )
        self.assertEqual(converted["status"], "RISK_BLOCKED")
        self.assertEqual(converted["signal"], "NO_TRADE")

    def test_stale_candles_block_actionable_trade(self) -> None:
        old = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        payload = {
            "data_source": "cache",
            "intervals": {
                "15m": {
                    "candles": [
                        {
                            "ts": old.isoformat(),
                            "open": 2500,
                            "high": 2501,
                            "low": 2499,
                            "close": 2500,
                        }
                    ]
                }
            },
        }
        gated = _apply_freshness_gate(
            {"status": "ACTIONABLE", "signal": "LONG"},
            payload,
            now=old + timedelta(hours=2),
        )
        self.assertEqual(gated["status"], "DATA_STALE")
        self.assertEqual(gated["signal"], "NO_TRADE")

    def test_macro_payload_excludes_all_technical_numbers(self) -> None:
        analysis = {
            "status": "ACTIONABLE",
            "signal": "LONG",
            "entry": 2501.25,
            "stop_loss": 2489.5,
            "target_1": 2525.0,
            "atr_15m": 12.4,
            "zones": [{"low": 2490, "high": 2500}],
            "selected_zone": {
                "type": "DEMAND",
                "formation": "RBR",
                "mitigation_pct": 25.0,
                "structure_event": "BOS_UP",
            },
            "regime": {
                "regime": "UPTREND",
                "movement_state": "GRIND_UP",
                "four_hour_bias": "UP",
                "adx14": 30.0,
            },
            "confirmation": {"execution_timeframe": "5m"},
            "execution": {"xau_ounces": 10.0},
        }
        payload = _macro_request_payload(
            analysis,
            {"dxy": {"value": 100.2}, "headlines": []},
        )
        serialized = str(payload)
        for forbidden in (
            "2501.25",
            "2489.5",
            "2525.0",
            "12.4",
            "2490",
            "2500",
            "30.0",
            "xau_ounces",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            payload["categorical_technical_context"]["movement_state"],
            "GRIND_UP",
        )

    def test_macro_can_veto_but_not_create_trade(self) -> None:
        blocked = _apply_macro_gate(
            {
                "status": "ACTIONABLE",
                "signal": "LONG",
                "execution": {"entry": 2500},
            },
            {
                "macro_bias": "BEARISH_GOLD",
                "risk_gate": "ALLOW",
                "confidence": "HIGH",
                "evidence_basis": "TIER1_OFFICIAL",
            },
        )
        self.assertEqual(blocked["status"], "MACRO_BLOCKED")
        self.assertEqual(blocked["signal"], "NO_TRADE")
        self.assertNotIn("execution", blocked)

        still_waiting = _apply_macro_gate(
            {"status": "WAIT", "signal": "NO_TRADE"},
            {
                "macro_bias": "BULLISH_GOLD",
                "risk_gate": "ALLOW",
                "confidence": "HIGH",
                "evidence_basis": "TIER1_OFFICIAL",
            },
        )
        self.assertEqual(still_waiting["status"], "WAIT")
        self.assertEqual(still_waiting["signal"], "NO_TRADE")

    def test_headlines_alone_cannot_block_trade(self) -> None:
        result = _apply_macro_gate(
            {"status": "ACTIONABLE", "signal": "LONG", "execution": {"entry": 1}},
            {
                "macro_bias": "BEARISH_GOLD",
                "risk_gate": "BLOCK",
                "confidence": "HIGH",
                "evidence_basis": "HEADLINES_ONLY",
            },
        )
        self.assertEqual(result["status"], "ACTIONABLE")
        self.assertEqual(result["macro_evaluation"]["risk_gate"], "CAUTION")

    def test_macro_service_failure_remains_fail_closed(self) -> None:
        result = _apply_macro_gate(
            {"status": "ACTIONABLE", "signal": "LONG", "execution": {"entry": 1}},
            {
                "macro_bias": "UNKNOWN",
                "risk_gate": "BLOCK",
                "confidence": "LOW",
                "evidence_basis": "INSUFFICIENT",
                "system_error": True,
            },
        )
        self.assertEqual(result["status"], "MACRO_BLOCKED")
        self.assertEqual(result["signal"], "NO_TRADE")

    def test_gemini_visible_text_skips_thought_parts(self) -> None:
        text = _gemini_visible_text(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "hidden reasoning", "thought": True},
                                {"text": '{"risk_gate":"ALLOW"}'},
                            ]
                        }
                    }
                ]
            }
        )
        self.assertEqual(text, '{"risk_gate":"ALLOW"}')

    def test_missing_gemini_key_blocks_without_network(self) -> None:
        from unittest.mock import patch

        with patch("supply_demand_strat.pipeline.load_dotenv"), patch(
            "supply_demand_strat.pipeline.os.getenv",
            return_value="",
        ):
            result = _macro_evaluation({"categorical_technical_context": {}})
        self.assertEqual(result["risk_gate"], "BLOCK")
        self.assertEqual(result["model"], "gemini-3.8-flash")
        self.assertIn("GEMINI_API_KEY", result["reason"])


def _flat_setup_inputs(last_ts: datetime) -> tuple[dict, list[dict]]:
    closes = [100.0 + (0.3 if index % 2 else -0.3) for index in range(80)]

    def series(minutes: int) -> list[dict]:
        start = last_ts - timedelta(minutes=minutes * (len(closes) - 1))
        return candles_from_closes(closes, start=start, minutes=minutes)

    candles = {"5m": series(5), "15m": series(15), "1h": series(60), "4h": series(240)}
    zone = {
        "id": "1h-demand-test",
        "timeframe": "1h",
        "type": "DEMAND",
        "formation": "DBR",
        "low": 99.5,
        "high": 100.5,
        "touches": 0,
        "mitigation_pct": 0.0,
        "score": 0.9,
    }
    return candles, [zone]


class StrategyGateTests(unittest.TestCase):
    def test_zone_against_four_hour_bias_is_filtered(self) -> None:
        candles, zones = _flat_setup_inputs(datetime(2026, 1, 5, 12, tzinfo=timezone.utc))
        regime = {"regime": "UPTREND", "movement_state": "TREND", "four_hour_bias": "DOWN"}
        result = evaluate_setup(candles, regime, zones, StrategyConfig())
        self.assertEqual(result["htf_filtered"], 1)
        self.assertEqual(result["candidate_zones"], [])
        self.assertIn("4H bias", result["reason"])

    def test_rollover_hour_blocks_new_orders(self) -> None:
        candles, zones = _flat_setup_inputs(datetime(2026, 1, 5, 21, 10, tzinfo=timezone.utc))
        regime = {"regime": "UPTREND", "movement_state": "TREND", "four_hour_bias": "UP"}
        result = evaluate_setup(candles, regime, zones, StrategyConfig())
        self.assertTrue(result["reason"].startswith("Session blocked"))

    def test_trigger_state_detects_stale_stop_orders(self) -> None:
        bars = [
            {"high": 101.0, "low": 99.5},
            {"high": 101.6, "low": 100.2},
        ]
        self.assertEqual(_trigger_state(bars, 0, "LONG", 101.5, 99.0), "ALREADY_TRIGGERED")
        bars[1]["low"] = 98.9
        self.assertEqual(
            _trigger_state(bars, 0, "LONG", 101.5, 99.0), "INVALIDATED_BEFORE_TRIGGER"
        )
        self.assertIsNone(_trigger_state(bars[:1], 0, "LONG", 101.5, 99.0))

    def test_noise_sized_hammer_is_not_a_confirmation(self) -> None:
        candles = candles_from_closes(
            [100.0 + (2.0 if index % 2 else -2.0) for index in range(30)],
            minutes=5,
            half_range=1.0,
        )
        candles.append(
            {"ts": "2026-01-01T02:30:00+00:00", "open": 100.0, "high": 100.12,
             "low": 99.5, "close": 100.1, "volume": 100}
        )
        zone = {"low": 99.0, "high": 101.0}
        loose = StrategyConfig(pattern_lookback=1, min_pattern_range_atr=0.0)
        strict = StrategyConfig(pattern_lookback=1)
        self.assertEqual(
            _pattern_in_zone(candles, zone, "LONG", loose)["name"], "BULLISH_HAMMER"
        )
        self.assertIsNone(_pattern_in_zone(candles, zone, "LONG", strict))


if __name__ == "__main__":
    unittest.main()
