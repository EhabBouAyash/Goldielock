"""Walk-forward backtest of the supply/demand strategy on parquet OHLC bars.

Every decision point sees only candles that had fully closed at that moment,
and calls the same ``evaluate_setup`` used by the live pipeline. Orders are
simulated as stop entries on the finest available bars with bid/ask spread,
stop slippage, order expiry, gap fills, and a stop-first rule whenever one bar
could have hit both the stop and the target.

Usage (from the repository root):

    python3 -m supply_demand_strat.backtest --data data/xauusd_1m.parquet
    python3 -m supply_demand_strat.backtest --data data/ --start 2024-01-01 \\
        --spread 0.30 --slippage 0.10 --set min_reward_risk=2.0
"""

from __future__ import annotations

import argparse
import glob
import json
import math
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .engine import (
    TIMEFRAME_MINUTES,
    StrategyConfig,
    classify_regime,
    detect_zones,
    evaluate_setup,
)


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PACKAGE_DIR / "backtest_results"
TIMEFRAMES = ("5m", "15m", "1h", "4h")
# Roughly the history the live pipeline receives from Yahoo, so zones and EMAs
# are computed over comparable windows.
DEFAULT_WINDOWS = {"5m": 400, "15m": 440, "1h": 480, "4h": 130}
MIN_BARS = {"5m": 30, "15m": 40, "1h": 60, "4h": 30}
NS_PER_MINUTE = 60 * 1_000_000_000

TIMESTAMP_ALIASES = (
    "ts",
    "timestamp",
    "time",
    "datetime",
    "date",
    "date_time",
    "gmt time",
    "time_utc",
    "open_time",
)
COLUMN_ALIASES = {
    "open": ("open", "o", "open_price"),
    "high": ("high", "h", "high_price"),
    "low": ("low", "l", "low_price"),
    "close": ("close", "c", "close_price", "last"),
    "volume": ("volume", "v", "vol", "tick_volume", "tickvol"),
}


@dataclass(frozen=True)
class BacktestSettings:
    starting_equity: float = 10_000.0
    # Full bid/ask spread in USD per ounce; data is treated as mid prices.
    spread: float = 0.30
    # Extra adverse fill on stop-type orders (entry and stop-loss).
    slippage: float = 0.10
    max_hold_hours: float | None = None
    daily_loss_limit_pct: float = 5.0
    max_loss_limit_pct: float = 10.0
    step: str = "5m"
    oos_fraction: float = 0.3
    windows: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_WINDOWS))


def _expand_paths(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        path = Path(item).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.rglob("*.parquet")))
        elif any(character in item for character in "*?["):
            paths.extend(sorted(Path(match) for match in glob.glob(item, recursive=True)))
        else:
            paths.append(path)
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Parquet input not found: {', '.join(missing)}")
    if not paths:
        raise FileNotFoundError(f"No parquet files matched: {', '.join(inputs)}")
    return paths


def _to_utc_index(values: Any, assume_tz: str) -> pd.DatetimeIndex:
    series = pd.Series(values)
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        magnitude = float(series.abs().median())
        unit = (
            "s" if magnitude < 1e11 else "ms" if magnitude < 1e14 else "us" if magnitude < 1e17 else "ns"
        )
        return pd.DatetimeIndex(pd.to_datetime(series, unit=unit, utc=True))
    try:
        parsed = pd.DatetimeIndex(pd.to_datetime(series))
    except (TypeError, ValueError):
        return pd.DatetimeIndex(pd.to_datetime(series, utc=True))
    if parsed.tz is None:
        return parsed.tz_localize(assume_tz).tz_convert("UTC")
    return parsed.tz_convert("UTC")


def normalize_bars(
    raw: pd.DataFrame,
    *,
    timestamp_column: str | None = None,
    assume_tz: str = "UTC",
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Map arbitrary OHLC column names onto a clean UTC-indexed frame."""
    lookup = {str(column).strip().lower(): column for column in raw.columns}
    if timestamp_column:
        if timestamp_column not in raw.columns:
            raise ValueError(f"Timestamp column {timestamp_column!r} not found.")
        timestamps = _to_utc_index(raw[timestamp_column], assume_tz)
    elif isinstance(raw.index, pd.DatetimeIndex):
        timestamps = _to_utc_index(raw.index, assume_tz)
    else:
        column = next((lookup[name] for name in TIMESTAMP_ALIASES if name in lookup), None)
        if column is None:
            raise ValueError(
                "No timestamp column found. Use a DatetimeIndex, one of "
                f"{TIMESTAMP_ALIASES}, or pass --timestamp-column."
            )
        timestamps = _to_utc_index(raw[column], assume_tz)

    data: dict[str, np.ndarray] = {}
    for target, aliases in COLUMN_ALIASES.items():
        column = next((lookup[name] for name in aliases if name in lookup), None)
        if column is None:
            if target == "volume":
                data[target] = np.full(len(raw), np.nan)
                continue
            raise ValueError(f"No {target!r} column found; tried {aliases}.")
        data[target] = pd.to_numeric(raw[column], errors="coerce").to_numpy(dtype=float)

    bars = pd.DataFrame(data, index=timestamps)
    bars.index.name = "ts"
    rows = len(bars)
    bars = bars.dropna(subset=["open", "high", "low", "close"])
    missing_rows = rows - len(bars)
    valid = (
        (bars[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (bars["high"] >= bars[["open", "close"]].max(axis=1))
        & (bars["low"] <= bars[["open", "close"]].min(axis=1))
    )
    invalid_rows = int((~valid).sum())
    bars = bars[valid].sort_index()
    duplicates = int(bars.index.duplicated(keep="last").sum())
    bars = bars[~bars.index.duplicated(keep="last")]
    return bars, {
        "rows_read": rows,
        "dropped_missing_ohlc": missing_rows,
        "dropped_inconsistent_ohlc": invalid_rows,
        "dropped_duplicate_timestamps": duplicates,
    }


def load_bars(
    inputs: list[str],
    *,
    timestamp_column: str | None = None,
    assume_tz: str = "UTC",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    frames: list[pd.DataFrame] = []
    quality = {
        "files": 0,
        "rows_read": 0,
        "dropped_missing_ohlc": 0,
        "dropped_inconsistent_ohlc": 0,
        "dropped_duplicate_timestamps": 0,
    }
    for path in _expand_paths(inputs):
        try:
            raw = pd.read_parquet(path)
        except ImportError as exc:
            raise SystemExit(
                "Reading parquet requires pyarrow: python3 -m pip install pyarrow"
            ) from exc
        bars, stats = normalize_bars(
            raw, timestamp_column=timestamp_column, assume_tz=assume_tz
        )
        frames.append(bars)
        quality["files"] += 1
        for key, value in stats.items():
            quality[key] += value
    combined = pd.concat(frames).sort_index()
    duplicates = int(combined.index.duplicated(keep="last").sum())
    combined = combined[~combined.index.duplicated(keep="last")]
    quality["dropped_duplicate_timestamps"] += duplicates
    # Live-download exports often end with a still-forming bar stamped
    # mid-interval (e.g. 07:45:14); it is not a completed bar.
    step_ns = int(round(infer_base_minutes(combined.index) * NS_PER_MINUTE))
    on_grid = combined.index.asi8 % step_ns == 0
    quality["dropped_off_grid_bars"] = int((~on_grid).sum())
    combined = combined[on_grid]
    quality["bars"] = len(combined)
    return combined, quality


def infer_base_minutes(index: pd.DatetimeIndex) -> float:
    if len(index) < 3:
        raise ValueError("Need at least three bars to infer the bar interval.")
    deltas = np.diff(index.asi8) / NS_PER_MINUTE
    deltas = deltas[deltas > 0]
    values, counts = np.unique(np.round(deltas, 6), return_counts=True)
    return float(values[np.argmax(counts)])


def resample_bars(bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate to UTC-aligned bars labelled by their open time."""
    grouped = bars.resample(
        f"{minutes}min", label="left", closed="left", origin="epoch"
    )
    frame = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(min_count=1),
        }
    )
    return frame.dropna(subset=["open", "high", "low", "close"])


def build_timeframes(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    base = infer_base_minutes(bars.index)
    if base > 5 or 5 % base:
        raise ValueError(
            f"Input bars are {base:g}-minute; the strategy executes on 5M, so the "
            "parquet data must be 1-minute or 5-minute bars."
        )
    return {
        timeframe: resample_bars(bars, TIMEFRAME_MINUTES[timeframe])
        for timeframe in TIMEFRAMES
    }


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    stamps = frame.index.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return [
        {"ts": ts, "open": o, "high": h, "low": low, "close": c, "volume": v}
        for ts, o, h, low, c, v in zip(
            stamps,
            frame["open"].to_numpy(dtype=float),
            frame["high"].to_numpy(dtype=float),
            frame["low"].to_numpy(dtype=float),
            frame["close"].to_numpy(dtype=float),
            frame["volume"].to_numpy(dtype=float),
        )
    ]


@dataclass
class _FillBars:
    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    interval_ns: int

    @classmethod
    def from_frame(cls, bars: pd.DataFrame) -> "_FillBars":
        return cls(
            ts=bars.index.asi8,
            open=bars["open"].to_numpy(dtype=float),
            high=bars["high"].to_numpy(dtype=float),
            low=bars["low"].to_numpy(dtype=float),
            close=bars["close"].to_numpy(dtype=float),
            interval_ns=int(round(infer_base_minutes(bars.index) * NS_PER_MINUTE)),
        )


REJECTION_PRIORITY = (
    "REWARD_RISK",
    "NO_TARGET",
    "ALREADY_TRIGGERED",
    "INVALIDATED_BEFORE_TRIGGER",
    "CONFIRMATIONS_NOT_ALIGNED",
    "NO_CROSSOVER",
    "NO_PATTERN",
    "NO_PATTERN_OR_CROSSOVER",
)


def _funnel_stage(setup: dict[str, Any]) -> str:
    """Name the furthest gate a decision point reached before being rejected."""
    status = setup.get("status")
    if status == "ACTIONABLE":
        return "ACTIONABLE"
    if status == "DATA_ERROR":
        return "DATA_ERROR"
    if str(setup.get("reason", "")).startswith("Session blocked"):
        return "SESSION_BLOCKED"
    if not setup.get("candidate_zones"):
        return "HTF_MISALIGNED" if setup.get("htf_filtered") else "NO_ZONE_AT_PRICE"
    rejections = {item.get("rejection") for item in setup.get("diagnostics") or []}
    return next((name for name in REJECTION_PRIORITY if name in rejections), "UNCONFIRMED")


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def simulate_order(
    setup: dict[str, Any],
    decided_ns: int,
    fills: _FillBars,
    settings: BacktestSettings,
) -> dict[str, Any]:
    """Simulate one stop-entry order against mid-price bars.

    Buy orders fill at ask (mid + half spread) and sell orders at bid. When a
    bar's range covers both levels, the adverse outcome is assumed.
    """
    long = setup["signal"] == "LONG"
    entry = float(setup["entry"])
    stop = float(setup["stop_loss"])
    target = float(setup["target_1"])
    half = settings.spread / 2.0
    slip = settings.slippage
    expiry_ns = decided_ns + int(setup["order_expiry_minutes"]) * NS_PER_MINUTE
    count = len(fills.ts)
    index = int(np.searchsorted(fills.ts, decided_ns, side="left"))
    order = {
        "decided_at": _iso(decided_ns),
        "direction": setup["signal"],
        "entry": entry,
        "stop_loss": stop,
        "target_1": target,
        "planned_risk": abs(entry - stop),
    }

    fill_index = None
    fill_price = None
    while index < count and fills.ts[index] < expiry_ns:
        o, h, low = fills.open[index], fills.high[index], fills.low[index]
        if long:
            triggered = h + half >= entry
            invalidated = low - half <= stop
            gap_fill = o + half >= entry
            price = (o + half if gap_fill else entry) + slip
        else:
            triggered = low - half <= entry
            invalidated = h + half >= stop
            gap_fill = o - half <= entry
            price = (o - half if gap_fill else entry) - slip
        if triggered:
            fill_index, fill_price = index, price
            break
        if invalidated:
            order.update(
                status="INVALIDATED",
                resolved_ns=int(fills.ts[index] + fills.interval_ns),
            )
            return order
        index += 1

    if fill_index is None:
        order.update(status="EXPIRED", resolved_ns=int(expiry_ns))
        return order

    max_hold_ns = (
        int(settings.max_hold_hours * 60 * NS_PER_MINUTE)
        if settings.max_hold_hours
        else None
    )
    exit_index, exit_price, reason = None, None, None
    # The fill bar may also reach the stop after entry; its target is ignored
    # because the intrabar order cannot be known.
    if long and fills.low[fill_index] - half <= stop:
        exit_index, exit_price, reason = fill_index, stop - slip, "STOP"
    elif not long and fills.high[fill_index] + half >= stop:
        exit_index, exit_price, reason = fill_index, stop + slip, "STOP"

    index = fill_index + 1
    while exit_index is None and index < count:
        o, h, low = fills.open[index], fills.high[index], fills.low[index]
        if max_hold_ns and fills.ts[index] - fills.ts[fill_index] >= max_hold_ns:
            exit_index, exit_price, reason = index, (o - half if long else o + half), "TIME"
        elif long:
            if o - half <= stop:
                exit_index, exit_price, reason = index, o - half - slip, "STOP"
            elif o - half >= target:
                exit_index, exit_price, reason = index, o - half, "TARGET"
            elif low - half <= stop:
                exit_index, exit_price, reason = index, stop - slip, "STOP"
            elif h - half >= target:
                exit_index, exit_price, reason = index, target, "TARGET"
        else:
            if o + half >= stop:
                exit_index, exit_price, reason = index, o + half + slip, "STOP"
            elif o + half <= target:
                exit_index, exit_price, reason = index, o + half, "TARGET"
            elif h + half >= stop:
                exit_index, exit_price, reason = index, stop + slip, "STOP"
            elif low + half <= target:
                exit_index, exit_price, reason = index, target, "TARGET"
        index += 1

    if exit_index is None:
        exit_index = count - 1
        close = fills.close[exit_index]
        exit_price, reason = (close - half if long else close + half), "END_OF_DATA"

    pnl = (exit_price - fill_price) if long else (fill_price - exit_price)
    order.update(
        status="FILLED",
        fill_time=_iso(int(fills.ts[fill_index])),
        fill_price=round(float(fill_price), 4),
        exit_time=_iso(int(fills.ts[exit_index] + fills.interval_ns)),
        exit_price=round(float(exit_price), 4),
        exit_reason=reason,
        pnl_per_oz=round(float(pnl), 4),
        r_multiple=round(float(pnl / order["planned_risk"]), 4),
        hold_hours=round(
            float(fills.ts[exit_index] + fills.interval_ns - fills.ts[fill_index])
            / (60 * NS_PER_MINUTE),
            3,
        ),
        resolved_ns=int(fills.ts[exit_index] + fills.interval_ns),
    )
    return order


def run_backtest(
    bars: pd.DataFrame,
    *,
    config: StrategyConfig | None = None,
    settings: BacktestSettings | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    config = config or StrategyConfig()
    settings = settings or BacktestSettings()
    frames = build_timeframes(bars)
    fills = _FillBars.from_frame(bars)
    records = {timeframe: _records(frames[timeframe]) for timeframe in TIMEFRAMES}
    close_ns = {
        timeframe: frames[timeframe].index.asi8
        + TIMEFRAME_MINUTES[timeframe] * NS_PER_MINUTE
        for timeframe in TIMEFRAMES
    }
    decisions = close_ns[settings.step]
    if start is not None:
        decisions = decisions[decisions >= pd.Timestamp(start).value]
    if end is not None:
        decisions = decisions[decisions <= pd.Timestamp(end).value]

    regime_cache: tuple[tuple[int, int], dict[str, Any]] | None = None
    zone_cache: dict[str, tuple[int, list[dict[str, Any]]]] = {}
    orders: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    funnel: dict[str, int] = {}
    blocked_until = -1
    total = len(decisions)

    for position, decided_ns in enumerate(decisions):
        if progress and position % 5000 == 0:
            print(f"  {position}/{total} decision points, {len(orders)} orders", flush=True)
        if decided_ns < blocked_until:
            continue
        counts = {
            timeframe: int(np.searchsorted(close_ns[timeframe], decided_ns, side="right"))
            for timeframe in TIMEFRAMES
        }
        if any(counts[timeframe] < MIN_BARS[timeframe] for timeframe in TIMEFRAMES):
            continue
        candles = {
            timeframe: records[timeframe][
                max(0, counts[timeframe] - settings.windows[timeframe]) : counts[timeframe]
            ]
            for timeframe in TIMEFRAMES
        }
        regime_key = (counts["1h"], counts["4h"])
        if regime_cache is None or regime_cache[0] != regime_key:
            regime_cache = (
                regime_key,
                classify_regime(candles["1h"], candles["4h"], config),
            )
        detected: list[dict[str, Any]] = []
        for timeframe in ("4h", "1h", "15m"):
            cached = zone_cache.get(timeframe)
            if cached is None or cached[0] != counts[timeframe]:
                cached = (
                    counts[timeframe],
                    detect_zones(candles[timeframe], timeframe, config),
                )
                zone_cache[timeframe] = cached
            detected.extend(cached[1])

        decided_at = datetime.fromtimestamp(decided_ns / 1e9, tz=timezone.utc)
        setup = evaluate_setup(
            candles, regime_cache[1], detected, config, now=decided_at
        )
        stage = _funnel_stage(setup)
        if stage == "ACTIONABLE":
            pattern = setup["confirmation"]["candlestick_pattern"]
            key = (setup["selected_zone"]["id"], str(pattern.get("ts")), setup["signal"])
            if key in seen:
                stage = "DUPLICATE_SETUP"
            seen.add(key)
        funnel[stage] = funnel.get(stage, 0) + 1
        if stage != "ACTIONABLE":
            continue

        order = simulate_order(setup, int(decided_ns), fills, settings)
        zone = setup["selected_zone"]
        regime = setup["regime"]
        order.update(
            zone_id=zone["id"],
            zone_timeframe=zone["timeframe"],
            zone_type=zone["type"],
            zone_formation=zone["formation"],
            zone_score=zone["score"],
            zone_mitigation_pct=zone["mitigation_pct"],
            structure_event=zone["structure_event"],
            regime=regime.get("regime"),
            movement_state=regime.get("movement_state"),
            four_hour_bias=regime.get("four_hour_bias"),
            execution_timeframe=setup["confirmation"]["execution_timeframe"],
            pattern=pattern.get("name"),
            planned_reward_risk=setup["reward_risk"],
            planned_reward_risk_net=setup.get("reward_risk_net"),
        )
        orders.append(order)
        blocked_until = order["resolved_ns"]

    trades = _size_trades(
        [order for order in orders if order["status"] == "FILLED"], config, settings
    )
    tested = {
        "start": _iso(int(decisions[0])) if len(decisions) else None,
        "end": _iso(int(decisions[-1])) if len(decisions) else None,
        "decision_points": total,
        "rejection_funnel": dict(sorted(funnel.items(), key=lambda item: -item[1])),
    }
    return {
        "orders": orders,
        "trades": trades,
        "summary": summarize(orders, trades, settings, tested),
    }


def _size_trades(
    filled: list[dict[str, Any]],
    config: StrategyConfig,
    settings: BacktestSettings,
) -> list[dict[str, Any]]:
    equity = settings.starting_equity
    sized: list[dict[str, Any]] = []
    for order in filled:
        trade = dict(order)
        risk_usd = equity * config.risk_fraction
        ounces = risk_usd / order["planned_risk"] if order["planned_risk"] > 0 else 0.0
        pnl_usd = ounces * order["pnl_per_oz"]
        equity += pnl_usd
        trade.update(
            risk_usd=round(risk_usd, 2),
            ounces=round(ounces, 4),
            pnl_usd=round(pnl_usd, 2),
            equity_after=round(equity, 2),
        )
        sized.append(trade)
    return sized


def _r_stats(r: np.ndarray, seed: int = 7) -> dict[str, Any]:
    if r.size == 0:
        return {"trades": 0}
    wins, losses = r[r > 0], r[r <= 0]
    gross_loss = float(-losses.sum())
    cumulative = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate(([0.0], cumulative)))[1:]
    streak = longest = 0
    for value in r:
        streak = streak + 1 if value <= 0 else 0
        longest = max(longest, streak)
    stats: dict[str, Any] = {
        "trades": int(r.size),
        "win_rate_pct": round(100.0 * wins.size / r.size, 1),
        "expectancy_r": round(float(r.mean()), 3),
        "median_r": round(float(np.median(r)), 3),
        "total_r": round(float(r.sum()), 2),
        "average_win_r": round(float(wins.mean()), 3) if wins.size else None,
        "average_loss_r": round(float(losses.mean()), 3) if losses.size else None,
        "profit_factor": round(float(wins.sum()) / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_r": round(float((peak - cumulative).max()), 2),
        "longest_losing_streak": longest,
    }
    if r.size >= 2:
        deviation = float(r.std(ddof=1))
        stats["t_stat"] = (
            round(float(r.mean()) / (deviation / math.sqrt(r.size)), 2)
            if deviation > 0
            else None
        )
        rng = np.random.default_rng(seed)
        means = rng.choice(r, size=(2000, r.size), replace=True).mean(axis=1)
        stats["expectancy_r_95ci"] = [
            round(float(np.percentile(means, 2.5)), 3),
            round(float(np.percentile(means, 97.5)), 3),
        ]
    return stats


def _breakdown(frame: pd.DataFrame, column: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value, group in frame.groupby(column, dropna=False):
        r = group["r_multiple"].to_numpy(dtype=float)
        result[str(value)] = {
            "trades": int(r.size),
            "win_rate_pct": round(100.0 * float((r > 0).mean()), 1),
            "expectancy_r": round(float(r.mean()), 3),
            "total_r": round(float(r.sum()), 2),
        }
    return result


def summarize(
    orders: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    settings: BacktestSettings,
    tested: dict[str, Any],
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for order in orders:
        status_counts[order["status"]] = status_counts.get(order["status"], 0) + 1
    summary: dict[str, Any] = {
        "period": tested,
        "costs": {"spread": settings.spread, "stop_slippage": settings.slippage},
        "orders": {"signals": len(orders), **status_counts},
        "fill_rate_pct": (
            round(100.0 * len(trades) / len(orders), 1) if orders else None
        ),
        "scope": (
            "Technical rules only. The Gemini macro veto and live prop-account "
            "state cannot be replayed historically."
        ),
    }
    if not trades:
        summary["overall"] = {"trades": 0}
        summary["verdict"] = ["No filled trades; there is nothing to evaluate."]
        return summary

    frame = pd.DataFrame(trades)
    frame["exit_ts"] = pd.to_datetime(frame["exit_time"], utc=True)
    r = frame["r_multiple"].to_numpy(dtype=float)
    summary["overall"] = _r_stats(r)
    summary["exit_reasons"] = frame["exit_reason"].value_counts().to_dict()
    summary["average_hold_hours"] = round(float(frame["hold_hours"].mean()), 2)

    start_ts = pd.Timestamp(tested["start"]) if tested.get("start") else frame["exit_ts"].min()
    end_ts = pd.Timestamp(tested["end"]) if tested.get("end") else frame["exit_ts"].max()
    months = max((end_ts - start_ts).days / 30.44, 1e-9)
    summary["trades_per_month"] = round(len(frame) / months, 2)

    equity = np.concatenate(([settings.starting_equity], frame["equity_after"].to_numpy(dtype=float)))
    peaks = np.maximum.accumulate(equity)
    daily = frame.groupby(frame["exit_ts"].dt.date)["pnl_usd"].sum()
    day_start = settings.starting_equity + daily.cumsum().shift(1, fill_value=0.0)
    daily_pct = 100.0 * daily / day_start
    floor = settings.starting_equity * (1 - settings.max_loss_limit_pct / 100.0)
    summary["account"] = {
        "starting_equity": settings.starting_equity,
        "ending_equity": round(float(equity[-1]), 2),
        "return_pct": round(100.0 * (equity[-1] / equity[0] - 1.0), 2),
        "max_drawdown_pct": round(float((100.0 * (peaks - equity) / peaks).max()), 2),
        "worst_day_pct": round(float(daily_pct.min()), 2),
        "days_breaching_daily_limit": int((daily_pct <= -settings.daily_loss_limit_pct).sum()),
        "max_loss_limit_breached": bool((equity <= floor).any()),
    }

    for column in (
        "direction",
        "regime",
        "movement_state",
        "zone_timeframe",
        "zone_formation",
        "execution_timeframe",
        "pattern",
    ):
        summary.setdefault("breakdown", {})[column] = _breakdown(frame, column)
    frame["year"] = frame["exit_ts"].dt.year
    summary["breakdown"]["year"] = _breakdown(frame, "year")

    split_ts = start_ts + (end_ts - start_ts) * (1.0 - settings.oos_fraction)
    in_sample = frame[frame["exit_ts"] < split_ts]["r_multiple"].to_numpy(dtype=float)
    out_sample = frame[frame["exit_ts"] >= split_ts]["r_multiple"].to_numpy(dtype=float)
    summary["time_split"] = {
        "split_at": split_ts.isoformat(),
        "first_segment": _r_stats(in_sample),
        "last_segment": _r_stats(out_sample),
    }
    summary["verdict"] = _verdict(summary)
    return summary


def _verdict(summary: dict[str, Any]) -> list[str]:
    overall = summary["overall"]
    notes: list[str] = []
    if overall["trades"] < 100:
        notes.append(
            f"Only {overall['trades']} trades: too few to separate edge from luck "
            "(aim for 100+ across several market regimes)."
        )
    ci = overall.get("expectancy_r_95ci")
    if ci and ci[0] > 0:
        notes.append("Bootstrap 95% CI of expectancy is above zero after costs.")
    elif ci and ci[1] < 0:
        notes.append("Bootstrap 95% CI of expectancy is below zero: negative edge after costs.")
    elif ci:
        notes.append("Expectancy CI straddles zero: no statistically reliable edge yet.")
    else:
        notes.append("Too few trades to estimate a confidence interval.")
    first = summary["time_split"]["first_segment"].get("expectancy_r")
    last = summary["time_split"]["last_segment"].get("expectancy_r")
    if first is not None and last is not None and (first > 0) != (last > 0):
        notes.append(
            "Expectancy changes sign between the first and last time segments: "
            "results are regime-dependent or unstable."
        )
    if summary["account"]["days_breaching_daily_limit"]:
        notes.append("At least one day breached the daily loss limit.")
    if summary["account"]["max_loss_limit_breached"]:
        notes.append("The max-loss limit was breached.")
    return notes


def format_report(summary: dict[str, Any]) -> str:
    lines = [
        "SUPPLY / DEMAND BACKTEST",
        f"Period: {summary['period'].get('start')} -> {summary['period'].get('end')}",
        f"Costs: spread ${summary['costs']['spread']:.2f}, stop slippage "
        f"${summary['costs']['stop_slippage']:.2f} per oz",
        f"Orders: {summary['orders']} | fill rate: {summary.get('fill_rate_pct')}%",
        f"Decision funnel (flat bars): {summary['period'].get('rejection_funnel')}",
        f"Scope: {summary['scope']}",
    ]
    overall = summary.get("overall", {})
    if overall.get("trades"):
        account = summary["account"]
        lines.extend(
            [
                "",
                f"Trades: {overall['trades']} ({summary['trades_per_month']}/month) | "
                f"win rate {overall['win_rate_pct']}% | expectancy {overall['expectancy_r']}R "
                f"(95% CI {overall.get('expectancy_r_95ci')}) | t={overall.get('t_stat')}",
                f"Total {overall['total_r']}R | profit factor {overall['profit_factor']} | "
                f"max DD {overall['max_drawdown_r']}R | longest losing streak "
                f"{overall['longest_losing_streak']}",
                f"Exits: {summary['exit_reasons']} | avg hold {summary['average_hold_hours']}h",
                f"Account: ${account['starting_equity']:,.0f} -> ${account['ending_equity']:,.2f} "
                f"({account['return_pct']}%), max DD {account['max_drawdown_pct']}%, "
                f"worst day {account['worst_day_pct']}%",
                "",
                f"Time split at {summary['time_split']['split_at']}:",
                f"  first: {summary['time_split']['first_segment']}",
                f"  last:  {summary['time_split']['last_segment']}",
                "",
                "Breakdown (trades / win% / expectancy R):",
            ]
        )
        for column, groups in summary["breakdown"].items():
            parts = [
                f"{name}: {stats['trades']} / {stats['win_rate_pct']}% / {stats['expectancy_r']}R"
                for name, stats in groups.items()
            ]
            lines.append(f"  {column}: " + "; ".join(parts))
    lines.append("")
    lines.append("Verdict:")
    lines.extend(f"  - {note}" for note in summary["verdict"])
    lines.append("")
    if overall.get("trades"):
        wins = round(overall["trades"] * overall["win_rate_pct"] / 100.0)
        lines.append(
            f"WIN RATE: {overall['win_rate_pct']}% "
            f"({wins} wins / {overall['trades'] - wins} losses of {overall['trades']} trades)"
        )
    else:
        lines.append("WIN RATE: n/a (no filled trades)")
    return "\n".join(lines) + "\n"


def _parse_overrides(pairs: list[str]) -> dict[str, Any]:
    defaults = {item.name: getattr(StrategyConfig(), item.name) for item in fields(StrategyConfig)}
    overrides: dict[str, Any] = {}
    for pair in pairs:
        name, _, raw = pair.partition("=")
        default = defaults.get(name)
        if name not in defaults or (not raw and not isinstance(default, tuple)):
            raise SystemExit(f"Unknown or empty strategy override: {pair!r}")
        if isinstance(default, bool):
            value: Any = raw.strip().lower() in {"1", "true", "yes", "on"}
        elif isinstance(default, int):
            value = int(raw)
        elif isinstance(default, float):
            value = float(raw)
        elif isinstance(default, tuple):
            value = tuple(int(part) for part in raw.split(",") if part.strip())
        else:
            value = raw
        overrides[name] = value
    return overrides


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = pd.Timestamp(value)
    return (parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed).to_pydatetime()


def write_outputs(result: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    orders = [
        {key: value for key, value in order.items() if key != "resolved_ns"}
        for order in result["orders"]
    ]
    trades = [
        {key: value for key, value in trade.items() if key != "resolved_ns"}
        for trade in result["trades"]
    ]
    pd.DataFrame(orders).to_csv(output_dir / "orders.csv", index=False)
    pd.DataFrame(trades).to_csv(output_dir / "trades.csv", index=False)
    (output_dir / "summary.json").write_text(
        json.dumps(result["summary"], indent=2, default=str) + "\n", encoding="utf-8"
    )
    (output_dir / "report.txt").write_text(format_report(result["summary"]), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Walk-forward backtest of the XAUUSD supply/demand strategy on parquet bars."
    )
    parser.add_argument("--data", nargs="+", required=True, help="Parquet files, directories, or globs of 1M/5M OHLC bars.")
    parser.add_argument("--timestamp-column", help="Timestamp column name, if not auto-detected.")
    parser.add_argument("--assume-tz", default="UTC", help="Timezone for naive timestamps (default UTC).")
    parser.add_argument("--start", help="First decision time, e.g. 2024-01-01.")
    parser.add_argument("--end", help="Last decision time.")
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--spread", type=float, default=0.30, help="Full spread in USD/oz.")
    parser.add_argument("--slippage", type=float, default=0.10, help="Stop-order slippage in USD/oz.")
    parser.add_argument("--max-hold-hours", type=float, help="Optional time exit.")
    parser.add_argument("--daily-loss-pct", type=float, default=5.0)
    parser.add_argument("--max-loss-pct", type=float, default=10.0)
    parser.add_argument("--step", choices=("5m", "15m"), default="5m", help="Decision frequency.")
    parser.add_argument("--oos-fraction", type=float, default=0.3, help="Share of the period reported as the last segment.")
    parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="FIELD=VALUE", help="Override a StrategyConfig field; repeatable.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    bars, quality = load_bars(
        args.data, timestamp_column=args.timestamp_column, assume_tz=args.assume_tz
    )
    if not args.quiet:
        print(f"Loaded {quality['bars']} bars from {quality['files']} file(s): {quality}")
    config = replace(StrategyConfig(), **_parse_overrides(args.overrides))
    settings = BacktestSettings(
        starting_equity=args.equity,
        spread=args.spread,
        slippage=args.slippage,
        max_hold_hours=args.max_hold_hours,
        daily_loss_limit_pct=args.daily_loss_pct,
        max_loss_limit_pct=args.max_loss_pct,
        step=args.step,
        oos_fraction=args.oos_fraction,
    )
    result = run_backtest(
        bars,
        config=config,
        settings=settings,
        start=_parse_time(args.start),
        end=_parse_time(args.end),
        progress=not args.quiet,
    )
    result["summary"]["data_quality"] = quality
    result["summary"]["strategy_overrides"] = _parse_overrides(args.overrides)
    write_outputs(result, args.out)
    print(format_report(result["summary"]), end="")
    print(f"Wrote {args.out}/summary.json, trades.csv, orders.csv, report.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
