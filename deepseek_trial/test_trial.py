"""Hand-checkable paths: look-ahead, cash below 200-day, quiet gate, no shorts."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from deepseek_trial.config import XAUUSD
from deepseek_trial.indicators import wilder_atr
from deepseek_trial.paper import replay
from deepseek_trial.t1_gold_trend.strategy import t1_signal, t1_view
from deepseek_trial.t2_gold_compression.strategy import t2_signal, t2_view
from deepseek_trial.t3_btc_ma.strategy import t3_signal, t3_view
from deepseek_trial.ticket import Ticket, tickets_match, validate_ticket


def _ohlc(close: np.ndarray, start="2015-01-01", freq="D", pad: float = 0.4) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(close), freq=freq, tz="UTC")
    return pd.DataFrame({
        "open": close,
        "high": close + pad,
        "low": close - pad,
        "close": close,
    }, index=idx)


class TestWilderAtr(unittest.TestCase):
    def test_seed_is_mean_of_first_n_true_ranges(self):
        high = np.array([10.0, 12.0, 11.0, 13.0])
        low = np.array([9.0, 10.0, 10.0, 11.0])
        close = np.array([9.5, 11.0, 10.5, 12.0])
        atr = wilder_atr(high, low, close, 3)
        self.assertTrue(np.isnan(atr[1]))
        self.assertAlmostEqual(atr[2], float(np.mean([
            10.0 - 9.0,
            max(12.0 - 10.0, abs(12.0 - 9.5), abs(10.0 - 9.5)),
            max(11.0 - 10.0, abs(11.0 - 11.0), abs(10.0 - 11.0)),
        ])))


class TestT1(unittest.TestCase):
    def test_stays_cash_below_200day(self):
        n = 280
        close = np.concatenate([np.linspace(2000, 2400, 200), np.linspace(2390, 1800, 80)])
        high, low = close + 5, close - 5
        view = t1_view(close, high, low, 270)
        self.assertFalse(view.allow_long)
        self.assertLess(view.close, view.sma200)
        action, reason = t1_signal(view, in_long=False)
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(reason, "NO_SIGNAL")

    def test_12_1_does_not_use_current_close_as_lookback(self):
        n = 280
        close = np.linspace(1000, 2000, n)
        close[-1] = 1.0
        view = t1_view(close, close + 1, close - 1, n - 1)
        # ret uses t-21 over t-252, both still in the uptrend, not the last crash print.
        self.assertGreater(view.ret_12_1, 0)

    def test_replay_exits_when_trend_filter_fails(self):
        up = np.linspace(2000, 2600, 300)
        down = np.linspace(2590, 1700, 80)
        close = np.concatenate([up, down])
        idx = pd.date_range("2015-01-01", periods=len(close), freq="D", tz="UTC")
        df = pd.DataFrame({
            "open": close, "high": close + 12.0, "low": close - 12.0, "close": close,
        }, index=idx)
        book = replay(df, strategy_id="T1", spec=XAUUSD, instrument="XAUUSD")
        self.assertGreaterEqual(len(book.trades), 1)


class TestT2(unittest.TestCase):
    def test_break_without_quiet_is_blocked(self):
        n = 150
        close = np.full(n, 100.0)
        high = close + 8.0
        low = close - 8.0
        close[-1] = 130.0
        high[-1] = 131.0
        view = t2_view(close, high, low, n - 1)
        self.assertFalse(view.break_up)
        action, side, reason = t2_signal(view, in_position=False)
        self.assertEqual(action, "NO_TRADE")
        self.assertIn(reason, ("NOT_QUIET", "NO_SIGNAL"))

    def test_channel_excludes_signal_bar(self):
        n = 150
        close = np.full(n, 100.0)
        high = np.full(n, 100.2)
        low = np.full(n, 99.8)
        high[-1] = 150.0
        close[-1] = 100.05
        view = t2_view(close, high, low, n - 1)
        self.assertLess(view.upper20, 150.0)
        self.assertAlmostEqual(view.upper20, 100.2)

    def test_quiet_then_small_break_can_open(self):
        n = 160
        close = np.full(n, 100.0)
        high = np.full(n, 100.15)
        low = np.full(n, 99.85)
        close[-1] = 100.16
        high[-1] = 100.16
        low[-1] = 99.86
        view = t2_view(close, high, low, n - 1)
        self.assertTrue(view.quiet)
        self.assertTrue(view.break_up)
        action, side, reason = t2_signal(view, False)
        self.assertEqual((action, side, reason), ("OPEN", "BUY", "VALID_SIGNAL"))


class TestT3(unittest.TestCase):
    def test_never_emits_short(self):
        close = np.concatenate([np.full(110, 80.0), np.linspace(80, 40, 30)])
        view = t3_view(close, close + 1, close - 1, 130)
        self.assertEqual(view.state, "CASH")
        action, reason = t3_signal(view, "LONG", True, False, 0)
        self.assertEqual(action, "CLOSE")
        action, reason = t3_signal(view, "CASH", False, False, 0)
        self.assertEqual(action, "NO_TRADE")

    def test_requires_cash_to_long_transition(self):
        close = np.linspace(100, 200, 130)
        view = t3_view(close, close + 1, close - 1, 129)
        action, _ = t3_signal(view, prev_state="LONG", in_long=False, stop_blocked=False, entries_this_week=0)
        self.assertEqual(action, "NO_TRADE")


class TestTickets(unittest.TestCase):
    def test_t1_cannot_open_sell(self):
        bad = Ticket(
            spec_version="2.0", strategy_id="T1", event_id="x", snapshot_hash="h",
            decision_time_utc="2026-09-28T00:00:00Z", action="OPEN", order_type="MARKET",
            side="SELL", entry_reference=1.0, stop_loss=2.0, take_profit=None,
            volume_lots=0.01, expires_at_utc="2026-09-28T00:01:00Z", reason_code="VALID_SIGNAL",
        )
        self.assertEqual(validate_ticket(bad), "INVALID_OUTPUT")

    def test_mismatch_rejects_entry(self):
        engine = Ticket(
            spec_version="2.0", strategy_id="T1", event_id="x", snapshot_hash="h",
            decision_time_utc="2026-09-28T00:00:00Z", action="OPEN", order_type="MARKET",
            side="BUY", entry_reference=2000.0, stop_loss=1980.0, take_profit=None,
            volume_lots=0.01, expires_at_utc="2026-09-28T00:01:00Z", reason_code="VALID_SIGNAL",
        )
        model = Ticket(**{**engine.__dict__, "stop_loss": 1900.0})
        self.assertFalse(tickets_match(engine, model, 0.01))


if __name__ == "__main__":
    unittest.main()
