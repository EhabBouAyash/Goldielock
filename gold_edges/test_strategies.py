"""Sanity checks: no look-ahead, RSI bounds, session wrap."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from gold_edges.core import rsi, walk_positions
from gold_edges.strategies import tsmom


class TestIndicators(unittest.TestCase):
    def test_rsi_uptrend_is_high(self):
        c = pd.Series(np.arange(1.0, 40.0))
        r = rsi(c, 2)
        self.assertGreater(r[-1], 90)

    def test_rsi_downtrend_is_low(self):
        c = pd.Series(np.arange(40.0, 1.0, -1.0))
        r = rsi(c, 2)
        self.assertLess(r[-1], 10)


class TestTsmom(unittest.TestCase):
    def test_uses_only_past_prices(self):
        n = 300
        close = np.linspace(100, 200, n)
        close[250:] = 50  # crash after bar 250
        df = pd.DataFrame({
            "open": close, "high": close + 1, "low": close - 1, "close": close,
        }, index=pd.date_range("2010-01-01", periods=n, tz="America/New_York"))
        pos = tsmom(df, lookback=100, skip=5)
        # Signal at 240 only sees prices up to 240-5 vs 240-100, still an uptrend.
        self.assertEqual(pos[240], 1.0)
        # Signal at 260 sees the crash (260-5=255 is after the drop).
        self.assertEqual(pos[260], -1.0)


class TestWalk(unittest.TestCase):
    def test_enter_next_open_not_same_close(self):
        close = np.array([10.0, 10.0, 12.0, 12.0, 8.0, 8.0])
        df = pd.DataFrame({
            "open": np.array([10.0, 10.0, 11.0, 12.0, 12.0, 8.0]),
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
        }, index=pd.date_range("2010-01-01", periods=6, tz="America/New_York"))
        pos = np.array([0, 1, 1, 1, 0, 0], dtype=float)
        trades = walk_positions(df, pos, stop_k=None, swap=False)
        self.assertGreaterEqual(len(trades), 1)
        # Entered on the open of the bar after the first +1 signal (index 1 -> open of 2).
        self.assertEqual(str(trades.iloc[0]["time"].date()), "2010-01-03")


if __name__ == "__main__":
    unittest.main()
