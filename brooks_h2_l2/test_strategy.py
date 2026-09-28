import unittest

import numpy as np
import pandas as pd

from brooks_h2_l2.backtest import Costs, Minutes, simulate
from brooks_h2_l2.strategy import Config, generate_signals, h2_long_setups, htf_trend


def _setup(highs):
    h = np.array(highs, dtype=float)
    low = h - 1.0
    close = h - 0.2
    return h2_long_setups(h, low, close, np.ones(len(h), dtype=bool), 20, 0.5)


def _walk(n=3000, seed=3):
    rng = np.random.default_rng(seed)
    close = 2000 + np.cumsum(rng.normal(0, 1.0, n))
    open_ = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0, 0.6, n))
    idx = pd.date_range("2025-01-06", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + spread,
                         "low": np.minimum(open_, close) - spread, "close": close}, index=idx)


class H2Detection(unittest.TestCase):
    def test_signal_bar_after_failed_high_1(self):
        sig, _, pb_low = _setup([10, 11, 12, 11.5, 11.0, 11.3, 11.1, 11.4])
        self.assertEqual(np.flatnonzero(sig).tolist(), [6])
        self.assertEqual(pb_low[6], 10.0)

    def test_consecutive_up_bars_are_one_attempt(self):
        sig, _, _ = _setup([10, 11, 12, 11.5, 11.6, 11.7, 11.2])
        self.assertEqual(np.flatnonzero(sig).tolist(), [6])

    def test_new_leg_high_resets_count(self):
        sig, _, _ = _setup([10, 11, 12, 11.5, 11.8, 12.5, 12.2, 12.0])
        self.assertFalse(sig.any())

    def test_no_signal_after_high_2(self):
        sig, _, _ = _setup([10, 11, 12, 11.5, 11.0, 11.3, 11.1, 11.4, 11.0, 10.8])
        self.assertEqual(np.flatnonzero(sig).tolist(), [6])

    def test_low_2_is_mirror_of_high_2(self):
        bars = _walk()
        k = 5000.0
        mirror = pd.DataFrame({"open": k - bars["open"], "high": k - bars["low"],
                               "low": k - bars["high"], "close": k - bars["close"]}, index=bars.index)
        cfg = Config(htf=None, min_risk_bps=0.0)  # reflection changes the price level
        a = generate_signals(bars, cfg)
        b = generate_signals(mirror, cfg)
        longs, shorts = a[a.direction == 1].reset_index(drop=True), b[b.direction == -1].reset_index(drop=True)
        self.assertGreater(len(longs), 0)
        self.assertEqual(longs["decision_time"].tolist(), shorts["decision_time"].tolist())
        np.testing.assert_allclose(longs["entry"], k - shorts["entry"])
        np.testing.assert_allclose(longs["stop"], k - shorts["stop"])


class HigherTimeframe(unittest.TestCase):
    def test_htf_trend_uses_only_completed_bars(self):
        bars = _walk()
        cfg = Config(htf="1h")
        full = htf_trend(bars, cfg)
        for cut in (500, 1234, 2001, 2999):
            self.assertEqual(htf_trend(bars.iloc[: cut + 1], cfg)[-1], full[cut])


def _minutes(rows):
    idx = pd.date_range("2025-01-06 10:00", periods=len(rows), freq="1min", tz="UTC")
    frame = pd.DataFrame(rows, columns=["bid_open", "bid_high", "bid_low", "bid_close"], index=idx)
    for col in ("open", "high", "low", "close"):
        frame[f"ask_{col}"] = frame[f"bid_{col}"] + 0.5
    return Minutes(frame)


def _signal(entry, stop, target, direction=1):
    return pd.DataFrame([{"decision_time": pd.Timestamp("2025-01-06 10:00", tz="UTC"), "direction": direction,
                          "entry": entry, "stop": stop, "target": target, "risk": abs(entry - stop)}])


class Simulator(unittest.TestCase):
    cfg = Config(timeframe="5min", valid_bars=1, max_hold_bars=2)
    costs = Costs(slippage=0.0)

    def test_long_fills_on_ask_and_hits_target(self):
        m = _minutes([[99, 99.2, 98.8, 99], [99.4, 100.4, 99.3, 100.2], [100.2, 102.5, 100.1, 102.3]])
        t = simulate(m, _signal(100, 98, 102), self.cfg, self.costs)
        self.assertEqual(t.iloc[0]["fill"], 100.0)
        self.assertEqual(t.iloc[0]["exit_reason"], "TARGET")
        self.assertAlmostEqual(t.iloc[0]["r"], 1.0)

    def test_minute_touching_stop_and_target_is_a_stop(self):
        m = _minutes([[99, 99.3, 98.9, 99.2], [99, 103, 97, 100]])
        t = simulate(m, _signal(100, 98, 102), self.cfg, self.costs)
        self.assertEqual(t.iloc[0]["exit_reason"], "STOP")
        self.assertAlmostEqual(t.iloc[0]["r"], -1.0)

    def test_order_cancelled_when_stop_trades_first(self):
        m = _minutes([[99, 99.2, 97.5, 98], [98, 101, 97.9, 100.5]])
        self.assertTrue(simulate(m, _signal(100, 98, 102), self.cfg, self.costs).empty)

    def test_order_expires_after_valid_window(self):
        m = _minutes([[99, 99.1, 98.9, 99]] * 5 + [[99, 101, 98.9, 100.8]])
        self.assertTrue(simulate(m, _signal(100, 98, 102), self.cfg, self.costs).empty)

    def test_scale_out_then_breakeven(self):
        m = _minutes([[99, 99.2, 98.9, 99], [99.4, 99.8, 99.3, 99.6], [100.5, 102.5, 100.4, 102],
                      [101, 101.2, 99.8, 100]])
        cfg = Config(timeframe="5min", valid_bars=1, max_hold_bars=2, scale_out_r=1.0, rr=2.0)
        t = simulate(m, _signal(100, 98, 104), cfg, self.costs)
        self.assertEqual(t.iloc[0]["exit_reason"], "BREAKEVEN")
        self.assertAlmostEqual(t.iloc[0]["r"], 0.5)

    def test_short_fills_on_bid_and_stops_on_ask(self):
        m = _minutes([[100.2, 100.3, 99.9, 100], [100, 101.8, 99.9, 101.6]])
        t = simulate(m, _signal(100, 102, 96, direction=-1), self.cfg, self.costs)
        self.assertEqual(t.iloc[0]["fill"], 100.0)
        self.assertEqual(t.iloc[0]["exit_reason"], "STOP")
        self.assertAlmostEqual(t.iloc[0]["exit"], 102.0)


if __name__ == "__main__":
    unittest.main()
