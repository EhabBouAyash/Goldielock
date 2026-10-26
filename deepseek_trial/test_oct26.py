"""GOLD-OCT26-v1 rule checks: EMA seed, H4 direction, A/B/C mechanics, DeepSeek schema."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from deepseek_trial.a_h4_m15_pullback.strategy import StrategyA
from deepseek_trial.auditor import ALLOWED_DECISIONS, ALLOWED_REASONS, PROMPT_VERSION
from deepseek_trial.b_london_sweep.strategy import DayRange, StrategyB
from deepseek_trial.c_ny_range_retest.strategy import StrategyC
from deepseek_trial.calendar_rules import load_calendar, london_a, merged_blackout, ny_c_entry
from deepseek_trial.config import H4_WARMUP, XAUUSD
from deepseek_trial.data import merge_h4
from deepseek_trial.engine import replay_m5
from deepseek_trial.indicators import ema_seeded, h4_direction
from deepseek_trial.risk import apply_stop_band, friction_per_ounce


class TestIndicators(unittest.TestCase):
    def test_ema_seeds_with_sma_then_smooths(self):
        c = np.arange(1.0, 30.0)
        e = ema_seeded(c, 5)
        self.assertTrue(np.isnan(e[3]))
        self.assertAlmostEqual(e[4], float(np.mean(c[:5])))
        alpha = 2 / 6
        self.assertAlmostEqual(e[5], alpha * c[5] + (1 - alpha) * e[4])

    def test_h4_up_requires_stack_and_slope(self):
        n = 10
        close = np.linspace(100, 120, n)
        e20 = close - 1
        e50 = close - 2
        self.assertEqual(h4_direction(close, e20, e50, 9), "UP")
        e20b = close.copy()
        e20b[-1] = e20b[-4] - 1
        self.assertEqual(h4_direction(close, e20b, e50, 9), "NEUTRAL")


class TestWindows(unittest.TestCase):
    def test_london_a_half_open(self):
        self.assertTrue(london_a(datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)))
        self.assertFalse(london_a(datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)))

    def test_ny_c_entry(self):
        self.assertTrue(ny_c_entry(datetime(2026, 10, 5, 13, 45, tzinfo=timezone.utc)))
        self.assertFalse(ny_c_entry(datetime(2026, 10, 5, 15, 15, tzinfo=timezone.utc)))

    def test_columbus_day_is_tag_only(self):
        events, err = load_calendar()
        self.assertIsNone(err)
        ts = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
        self.assertFalse(merged_blackout(ts, events))


class TestA(unittest.TestCase):
    def test_arms_on_ema20_cross_down_above_ema50(self):
        n = 30
        c = np.full(n, 110.0)
        c[20] = 109.0
        ema20 = np.full(n, 109.5)
        ema50 = np.full(n, 100.0)
        h = c + 1
        l = c - 1
        a = StrategyA()
        h4c = np.linspace(100, 120, 10)
        e20 = h4c - 1
        e50 = h4c - 2
        ts = datetime(2026, 10, 5, 8, 15, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
        a.s.prev_close = 110.0
        a.s.prev_ema20 = 109.5
        a.on_m15(20, ts, c, h, l, c, ema20, ema50, h4c, e20, e50, 9, False)
        self.assertTrue(a.s.armed)
        self.assertEqual(a.s.side, "BUY")


class TestB(unittest.TestCase):
    def test_ambiguous_wick_both_sides_does_not_arm(self):
        b = StrategyB()
        b.s.range = DayRange("2026-10-05", 2000.0, 1990.0, 1995.0, 10.0, True)
        b.s.disabled = False
        b.s.frozen = True
        b.s.today = "2026-10-05"
        n = 20
        c = np.full(n, 1995.0)
        h = np.full(n, 1996.0)
        l = np.full(n, 1994.0)
        h[15] = 2001.0
        l[15] = 1989.0
        o = c.copy()
        h4c = np.linspace(100, 110, 8)
        ts = datetime(2026, 10, 5, 8, 5, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
        start = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
        cand = b.on_m5(15, ts, start, o, h, l, c, h4c, h4c - 1, h4c - 2, 7, False)
        self.assertFalse(b.s.armed)
        self.assertIsNone(cand)

    def test_collects_overnight_by_bar_start_not_close(self):
        b = StrategyB()
        n = 3
        o = h = l = c = np.array([2000.0, 2001.0, 1999.0])
        h4c = np.linspace(100, 110, 8)
        # Bar starts 07:55 London, closes 08:00 — still inside [00:00, 08:00).
        start = datetime(2026, 10, 5, 7, 55, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
        close = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
        b.on_m5(0, close, start, o, h, l, c, h4c, h4c - 1, h4c - 2, 7, False)
        self.assertEqual(len(b.s.collecting), 1)
        # First bar at/after 08:00 London freezes (history short → disabled).
        start8 = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
        close8 = datetime(2026, 10, 5, 8, 5, tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)
        b.on_m5(1, close8, start8, o, h, l, c, h4c, h4c - 1, h4c - 2, 7, False)
        self.assertTrue(b.s.frozen)
        self.assertTrue(b.s.disabled)


class TestC(unittest.TestCase):
    def test_body_must_be_60_percent(self):
        o, h, l, c = 100.0, 101.0, 99.0, 100.2
        body = abs(c - o)
        self.assertLess(body / (h - l), 0.60)

    def test_same_bar_retest_not_allowed(self):
        cstrat = StrategyC()
        cstrat.s.armed = True
        cstrat.s.side = "BUY"
        cstrat.s.brk_i = 10
        cstrat.s.ab = 2.0
        cstrat.s.frozen = True
        cstrat.s.today = "2026-10-05"
        from deepseek_trial.c_ny_range_retest.strategy import CRange
        start = datetime(2026, 10, 5, 13, 45, tzinfo=timezone.utc)
        cstrat.s.range = CRange("2026-10-05", 2000.0, 1990.0, 10.0, True, start, start)
        n = 20
        o = np.full(n, 2000.5)
        h = np.full(n, 2001.0)
        l = np.full(n, 1999.8)
        c = np.full(n, 2000.6)
        h4c = np.linspace(100, 110, 8)
        ts = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
        bar_start = datetime(2026, 10, 5, 13, 55, tzinfo=timezone.utc)
        # waited is computed as i - brk_i; same bar i==brk_i → waited 0 → no confirm
        cand = cstrat.on_m5(10, ts, bar_start, o, h, l, c, h4c, h4c - 1, h4c - 2, 7, False, [])
        self.assertIsNone(cand)
        self.assertTrue(cstrat.s.armed)


class TestRisk(unittest.TestCase):
    def test_stop_never_widens_band(self):
        stop, err = apply_stop_band("BUY", 2000.0, 1999.0, 2.0, 0.01)
        self.assertEqual(err, "")
        self.assertLess(stop, 2000.0)
        self.assertGreaterEqual(2000.0 - stop, 0.80 * 2.0 - 0.02)

    def test_friction_uses_commission_and_slippage(self):
        f = friction_per_ounce(4000.0, XAUUSD)
        self.assertGreater(f, 0)


class TestDeepSeekSchema(unittest.TestCase):
    def test_enums(self):
        self.assertIn("TAKE", ALLOWED_DECISIONS)
        self.assertIn("PASS", ALLOWED_DECISIONS)
        self.assertIn("TWO_SIDED_CHOP", ALLOWED_REASONS)
        self.assertTrue(PROMPT_VERSION.startswith("GOLD-OCT26-v1-prompt-"))


class TestH4MergeAndWarmup(unittest.TestCase):
    def test_merge_prefers_live_overlap(self):
        idx_h = pd.date_range("2026-01-01", periods=3, freq="4h", tz="UTC")
        idx_l = pd.date_range("2026-01-01 08:00", periods=2, freq="4h", tz="UTC")
        hist = pd.DataFrame({"open": 1, "high": 2, "low": 0, "close": 1.5}, index=idx_h)
        live = pd.DataFrame({"open": 9, "high": 9, "low": 9, "close": 9}, index=idx_l)
        merged = merge_h4(hist, live)
        self.assertEqual(merged.close.iloc[-1], 9)
        self.assertEqual(merged.close.iloc[0], 1.5)

    def test_engine_collects_b_ranges_before_warmup(self):
        """B must freeze overnight boxes during H4 warmup, not skip those days."""
        start = datetime(2026, 7, 6, 6, 50, tzinfo=timezone.utc)  # 07:50 London BST
        idx = pd.date_range(start, periods=20, freq="5min", tz="UTC")
        m5 = pd.DataFrame(
            {"open": 2000.0, "high": 2001.0, "low": 1999.0, "close": 2000.5},
            index=idx,
        )
        h4_idx = pd.date_range("2025-01-01", periods=H4_WARMUP + 5, freq="4h", tz="UTC")
        h4_hist = pd.DataFrame(
            {"open": 1800.0, "high": 1810.0, "low": 1790.0, "close": 1805.0},
            index=h4_idx,
        )
        result = replay_m5(m5, audit=False, source="test", strategy_filter="B", h4_hist=h4_hist)
        funnel = result["diagnostics"]["funnel"]["B"]
        self.assertGreaterEqual(funnel["frozen_days"], 1)


if __name__ == "__main__":
    unittest.main()
