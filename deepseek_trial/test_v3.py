"""v3 knowledge agents: cost math, schema, daily fill cap."""
from __future__ import annotations

import unittest

from deepseek_trial.v3_knowledge.auditor import _extract_json, _parse
from deepseek_trial.v3_knowledge.cost import CALLS_IF_LOOK_CAP, CALLS_IF_MAX_TRADES, budget, usd
from deepseek_trial.v3_knowledge.engine import MAX_FILLS_PER_DAY, MAX_LOOKS_PER_DAY


class TestV3Cost(unittest.TestCase):
    def test_five_trades_times_thirty_is_150_calls(self):
        self.assertEqual(CALLS_IF_MAX_TRADES, 150)

    def test_look_cap_is_240_calls(self):
        self.assertEqual(CALLS_IF_LOOK_CAP, 240)

    def test_pro_thinking_150_calls_is_a_few_dollars(self):
        b = budget()
        per = b["per_agent_30d"][0]["pro_thinking_usd"]
        self.assertGreater(per, 1.0)
        self.assertLess(per, 10.0)

    def test_usd_formula(self):
        # 1M in @ 1.32 + 1M out @ 3.96 = 5.28
        self.assertAlmostEqual(usd(1, 1_000_000, 1_000_000, 1.32e-6, 3.96e-6), 5.28)


class TestV3Schema(unittest.TestCase):
    def test_extract_json_fence(self):
        raw = _extract_json('```json\n{"decision":"SKIP"}\n```')
        self.assertEqual(raw["decision"], "SKIP")
        prop, meta = _parse({"decision": "SKIP"}, {})
        self.assertIsNone(prop)
        self.assertEqual(meta["decision"], "SKIP")

    def test_trade_clamps_stop(self):
        prop, meta = _parse({"decision": "TRADE", "side": "BUY", "stop_atr": 0.1, "target_r": 9}, {})
        self.assertEqual(prop["side"], "BUY")
        self.assertEqual(prop["stop_atr"], 0.80)
        self.assertEqual(prop["target_r"], 2.50)
        self.assertEqual(meta["decision"], "TRADE")

    def test_trade_rejects_bad_side(self):
        prop, _ = _parse({"decision": "TRADE", "side": "HOLD"}, {})
        self.assertIsNone(prop)


class TestV3Caps(unittest.TestCase):
    def test_five_fills_max(self):
        self.assertEqual(MAX_FILLS_PER_DAY, 5)

    def test_eight_looks_max(self):
        self.assertEqual(MAX_LOOKS_PER_DAY, 8)


if __name__ == "__main__":
    unittest.main()
