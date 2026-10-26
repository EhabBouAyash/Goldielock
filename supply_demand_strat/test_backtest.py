from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from supply_demand_strat import backtest
from supply_demand_strat.backtest import (
    BacktestSettings,
    NS_PER_MINUTE,
    _FillBars,
    _r_stats,
    build_timeframes,
    load_bars,
    normalize_bars,
    resample_bars,
    run_backtest,
    simulate_order,
)

START = pd.Timestamp("2026-01-05T00:00:00Z")


def synthetic_bars(count: int, minutes: int = 5, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 2000 + np.cumsum(rng.normal(0, 1.5, count))
    opens = np.concatenate(([closes[0]], closes[:-1]))
    spread = np.abs(rng.normal(0, 1.0, count)) + 0.2
    index = pd.date_range(START, periods=count, freq=f"{minutes}min")
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + spread,
            "low": np.minimum(opens, closes) - spread,
            "close": closes,
            "volume": np.full(count, 100.0),
        },
        index=pd.DatetimeIndex(index, name="ts"),
    )


def fill_bars(rows: list[tuple[float, float, float, float]]) -> _FillBars:
    values = np.asarray(rows, dtype=float)
    return _FillBars(
        ts=pd.date_range(START, periods=len(rows), freq="5min").asi8,
        open=values[:, 0],
        high=values[:, 1],
        low=values[:, 2],
        close=values[:, 3],
        interval_ns=5 * NS_PER_MINUTE,
    )


def long_setup(expiry_minutes: int = 30) -> dict:
    return {
        "signal": "LONG",
        "entry": 101.0,
        "stop_loss": 99.0,
        "target_1": 104.0,
        "order_expiry_minutes": expiry_minutes,
    }


FRICTIONLESS = BacktestSettings(spread=0.0, slippage=0.0)


class NormalizeBarsTests(unittest.TestCase):
    def test_epoch_milliseconds_and_aliases(self) -> None:
        raw = pd.DataFrame(
            {
                "Time": [1_767_225_600_000, 1_767_225_900_000],
                "O": [1.0, 2.0],
                "H": [2.0, 3.0],
                "L": [0.5, 1.5],
                "Close": [1.5, 2.5],
            }
        )
        bars, stats = normalize_bars(raw)
        self.assertEqual(str(bars.index[0]), "2026-01-01 00:00:00+00:00")
        self.assertEqual(stats["rows_read"], 2)
        self.assertTrue(bars["volume"].isna().all())

    def test_invalid_and_duplicate_rows_are_dropped(self) -> None:
        raw = pd.DataFrame(
            {
                "timestamp": [
                    "2026-01-01 00:00",
                    "2026-01-01 00:05",
                    "2026-01-01 00:10",
                    "2026-01-01 00:10",
                ],
                "open": [1.0, 1.0, None, 1.0],
                "high": [2.0, 0.9, 2.0, 2.0],
                "low": [0.5, 0.5, 0.5, 0.5],
                "close": [1.5, 0.8, 1.5, 1.6],
            }
        )
        bars, stats = normalize_bars(raw, assume_tz="America/New_York")
        self.assertEqual(stats["dropped_missing_ohlc"], 1)
        self.assertEqual(stats["dropped_inconsistent_ohlc"], 1)
        self.assertEqual(len(bars), 2)
        self.assertEqual(str(bars.index[0]), "2026-01-01 05:00:00+00:00")

    def test_parquet_round_trip(self) -> None:
        frame = synthetic_bars(20)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.parquet"
            frame.to_parquet(path)
            loaded, quality = load_bars([directory])
        self.assertEqual(quality["files"], 1)
        self.assertEqual(quality["bars"], 20)
        np.testing.assert_allclose(loaded["close"].to_numpy(), frame["close"].to_numpy())

    def test_forming_bar_stamped_off_grid_is_dropped(self) -> None:
        frame = synthetic_bars(20)
        partial = frame.iloc[[-1]].copy()
        partial.index = partial.index + pd.Timedelta(seconds=14)
        frame = pd.concat([frame, partial])
        with tempfile.TemporaryDirectory() as directory:
            frame.to_parquet(Path(directory) / "bars.parquet")
            loaded, quality = load_bars([directory])
        self.assertEqual(quality["dropped_off_grid_bars"], 1)
        self.assertEqual(len(loaded), 20)

    def test_four_hour_bars_align_to_utc_midnight(self) -> None:
        frame = synthetic_bars(200, minutes=5)
        frame.index = frame.index + pd.Timedelta(hours=1)
        four_hour = resample_bars(frame, 240)
        self.assertEqual(str(four_hour.index[0]), "2026-01-05 00:00:00+00:00")
        self.assertTrue(all(stamp.hour % 4 == 0 for stamp in four_hour.index))

    def test_rejects_bars_coarser_than_five_minutes(self) -> None:
        with self.assertRaises(ValueError):
            build_timeframes(synthetic_bars(50, minutes=15))


class SimulateOrderTests(unittest.TestCase):
    def test_fill_then_target(self) -> None:
        fills = fill_bars(
            [(100, 100.5, 99.8, 100.2), (100.2, 101.5, 100.1, 101.2), (101.2, 104.5, 101, 104)]
        )
        order = simulate_order(long_setup(), int(fills.ts[0]), fills, FRICTIONLESS)
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["exit_reason"], "TARGET")
        self.assertAlmostEqual(order["r_multiple"], 1.5)

    def test_bar_touching_stop_and_target_is_a_loss(self) -> None:
        fills = fill_bars([(100, 101.2, 100, 101), (101, 104.5, 98.5, 102)])
        order = simulate_order(long_setup(), int(fills.ts[0]), fills, FRICTIONLESS)
        self.assertEqual(order["exit_reason"], "STOP")
        self.assertAlmostEqual(order["r_multiple"], -1.0)

    def test_fill_bar_ignores_target_but_honours_stop(self) -> None:
        fills = fill_bars([(100, 105, 98.5, 102)])
        order = simulate_order(long_setup(), int(fills.ts[0]), fills, FRICTIONLESS)
        self.assertEqual(order["exit_reason"], "STOP")

    def test_gap_through_stop_fills_at_open(self) -> None:
        fills = fill_bars([(100, 101.5, 100, 101.2), (97.0, 97.5, 96.5, 97.2)])
        order = simulate_order(long_setup(), int(fills.ts[0]), fills, FRICTIONLESS)
        self.assertEqual(order["exit_reason"], "STOP")
        self.assertAlmostEqual(order["exit_price"], 97.0)
        self.assertAlmostEqual(order["r_multiple"], -2.0)

    def test_order_expires_when_never_triggered(self) -> None:
        fills = fill_bars([(100, 100.5, 99.5, 100)] * 10)
        order = simulate_order(long_setup(expiry_minutes=15), int(fills.ts[0]), fills, FRICTIONLESS)
        self.assertEqual(order["status"], "EXPIRED")
        self.assertEqual(order["resolved_ns"], int(fills.ts[0]) + 15 * NS_PER_MINUTE)

    def test_stop_hit_before_trigger_invalidates(self) -> None:
        fills = fill_bars([(100, 100.5, 98.8, 99), (99, 102, 99, 101.5)])
        order = simulate_order(long_setup(), int(fills.ts[0]), fills, FRICTIONLESS)
        self.assertEqual(order["status"], "INVALIDATED")

    def test_spread_and_slippage_worsen_the_fill(self) -> None:
        fills = fill_bars([(100, 101.5, 100, 101.2), (101.2, 105, 101, 104.5)])
        settings = BacktestSettings(spread=0.4, slippage=0.1)
        order = simulate_order(long_setup(), int(fills.ts[0]), fills, settings)
        self.assertAlmostEqual(order["fill_price"], 101.1)
        self.assertLess(order["r_multiple"], 1.5)


class RunBacktestTests(unittest.TestCase):
    def _capture(self, bars: pd.DataFrame) -> dict[int, dict[str, str]]:
        seen: dict[int, dict[str, str]] = {}
        real = backtest.evaluate_setup

        def spy(candles, regime, detected, config, *, now=None):
            seen[int(pd.Timestamp(now).value)] = {
                timeframe: rows[-1]["ts"] for timeframe, rows in candles.items()
            }
            return real(candles, regime, detected, config, now=now)

        with mock.patch.object(backtest, "evaluate_setup", side_effect=spy):
            run_backtest(bars)
        return seen

    def test_only_completed_bars_are_visible(self) -> None:
        seen = self._capture(synthetic_bars(1700))
        self.assertTrue(seen)
        for decided_ns, last in seen.items():
            for timeframe, stamp in last.items():
                closes_at = pd.Timestamp(stamp).value + backtest.TIMEFRAME_MINUTES[timeframe] * NS_PER_MINUTE
                self.assertLessEqual(closes_at, decided_ns, timeframe)

    def test_future_data_does_not_change_past_inputs(self) -> None:
        bars = synthetic_bars(1800)
        full = self._capture(bars)
        truncated = self._capture(bars.iloc[:1650])
        common = sorted(set(full) & set(truncated))
        self.assertGreater(len(common), 50)
        for decided_ns in common:
            self.assertEqual(full[decided_ns], truncated[decided_ns])

    def test_funnel_accounts_for_every_evaluated_decision(self) -> None:
        bars = synthetic_bars(1700)
        evaluated = len(self._capture(bars))
        funnel = run_backtest(bars)["summary"]["period"]["rejection_funnel"]
        self.assertGreater(evaluated, 0)
        self.assertEqual(sum(funnel.values()), evaluated)


class StatsTests(unittest.TestCase):
    def test_r_stats(self) -> None:
        stats = _r_stats(np.array([2.0, -1.0, -1.0, 2.0, -1.0]))
        self.assertEqual(stats["trades"], 5)
        self.assertAlmostEqual(stats["win_rate_pct"], 40.0)
        self.assertAlmostEqual(stats["expectancy_r"], 0.2)
        self.assertAlmostEqual(stats["profit_factor"], 4 / 3, places=2)
        self.assertEqual(stats["longest_losing_streak"], 2)
        self.assertEqual(stats["max_drawdown_r"], 2.0)
        low, high = stats["expectancy_r_95ci"]
        self.assertLess(low, 0.2)
        self.assertGreater(high, 0.2)

    def test_empty_stats(self) -> None:
        self.assertEqual(_r_stats(np.array([])), {"trades": 0})


if __name__ == "__main__":
    unittest.main()
