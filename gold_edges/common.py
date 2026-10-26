"""Shared data, costs and scoring for the gold_edges studies.

Every strategy is reduced to a daily series of returns on 1x notional, after
costs, indexed by New York trading day. Leverage is then fixed on 2009-2017 so
that the training drawdown is 4% (under FundedNext's 5% soft max loss), and
the same leverage is applied to 2018-2026. That makes strategies with very
different trade lengths comparable in account terms.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from brooks_h2_l2.data import load_minutes

RESULTS = Path(__file__).resolve().parent / "results"
NY = "America/New_York"
TRAIN_END = pd.Timestamp("2018-01-01", tz=NY)

# Costs as fractions of price.
SPREAD = 1.6e-4          # HistData model: normal spread ~1.6bp of price
FIRST_PRINT_SPREAD = 2.8e-4  # Dukascopy Sep 2023: $0.53 first-print spread at ~$1,920
SLIPPAGE = 0.10          # $ per side on market / stop orders
SWAP_YEAR = 0.05         # CFD overnight financing, charged per night held through 17:00 ET

# FundedNext Stellar 1-Step, % of initial balance.
DAILY_SOFT, DAILY_HARD, MAX_SOFT, MAX_HARD = 2.5, 3.0, 5.0, 6.0
TRAIN_DD_TARGET = 4.0
MAX_LEVERAGE = 30.0      # metals leverage on most prop accounts is 1:20-1:30


_MINUTES: pd.DataFrame | None = None


def minutes() -> pd.DataFrame:
    """Bid/ask minutes in New York time (cached)."""
    global _MINUTES
    if _MINUTES is None:
        _MINUTES = load_minutes().tz_convert(NY)
    return _MINUTES


def mid(m: pd.DataFrame | None = None) -> pd.DataFrame:
    m = minutes() if m is None else m
    return pd.DataFrame({c: (m[f"bid_{c}"] + m[f"ask_{c}"]) / 2 for c in ("open", "high", "low", "close")})


def trading_day(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The broker day: 17:00 ET to 17:00 ET, labelled by the date it closes on."""
    return (index + pd.Timedelta(hours=7)).normalize()


def daily_bars(m: pd.DataFrame | None = None) -> pd.DataFrame:
    x = mid(m)
    g = x.groupby(trading_day(x.index))
    d = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last()})
    return d[d.index.dayofweek < 5]


def round_trip_cost(price: float | np.ndarray, spread: float = SPREAD, stop_exit: bool = False) -> np.ndarray:
    """Fraction of price paid per round trip: one spread, entry slippage, and exit slippage on stops."""
    price = np.asarray(price, dtype=float)
    return spread + SLIPPAGE * (1 + stop_exit) / price


# ---------------------------------------------------------------- scoring
def _dd(equity: np.ndarray) -> np.ndarray:
    return np.maximum.accumulate(np.maximum(equity, 0)) - equity


def leverage_for(train: pd.Series, target_dd: float = TRAIN_DD_TARGET, cap: float = MAX_LEVERAGE) -> float:
    """Size whose non-compounded training drawdown equals target_dd (% of balance)."""
    dd = _dd(np.cumsum(train.to_numpy() * 100)).max()
    if dd <= 0:
        return cap
    return float(min(cap, target_dd / dd))


def t_stat(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    x = x[x != 0]
    if len(x) < 20 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def period_stats(daily: pd.Series, lev: float) -> dict:
    """daily: returns on 1x notional per trading day (0 on flat days)."""
    pct = daily.to_numpy() * lev * 100
    active = daily.to_numpy() != 0
    years = len(daily) / 252
    equity = np.cumsum(pct)
    dd = _dd(equity)
    sd = pct.std(ddof=1)
    trades = daily[active]
    return {
        "days_traded": int(active.sum()),
        "win_pct": round(100 * float((trades > 0).mean()), 1) if active.any() else None,
        "avg_bp": round(float(trades.mean() * 1e4), 2) if active.any() else None,
        "t": round(t_stat(daily.to_numpy()), 2),
        "ret_pct_yr": round(float(equity[-1] / years), 2),
        "sharpe": round(float(pct.mean() / sd * np.sqrt(252)), 2) if sd > 0 else None,
        "max_dd_pct": round(float(dd.max()), 2),
        "worst_day_pct": round(float(pct.min()), 2),
        "daily_soft_breaches": int((pct <= -DAILY_SOFT).sum()),
        "max_hard_breached": bool(dd.max() >= MAX_HARD),
        "years_to_10pct": round(10 / (equity[-1] / years), 1) if equity[-1] > 0 else None,
    }


R_UNIT = 0.01  # R-sized strategies book 1R as 1% of balance, so their size is % risk per trade
MAX_RISK_PCT = 2.0


def evaluate(name: str, daily: pd.Series, lev: float | None = None, cap: float = MAX_LEVERAGE) -> dict:
    """Size from the training half unless given; stats for both halves.

    For notional strategies the size is leverage. For R-sized strategies (daily
    = R x R_UNIT) it is % of balance risked per trade; pass cap=MAX_RISK_PCT.
    """
    daily = daily.sort_index().fillna(0.0)
    idx = daily.index if daily.index.tz is not None else daily.index.tz_localize(NY)
    daily = pd.Series(daily.to_numpy(), idx)
    train, test = daily[daily.index < TRAIN_END], daily[daily.index >= TRAIN_END]
    lev = leverage_for(train, cap=cap) if lev is None else lev
    row = {"strategy": name, "size": round(lev, 2)}
    for tag, part in (("train", train), ("test", test)):
        for k, v in period_stats(part, lev).items():
            row[f"{tag}_{k}"] = v
    return row


def calendar(daily_index: pd.DatetimeIndex | None = None) -> pd.DatetimeIndex:
    """All trading days in the data, so flat days count as zero."""
    return daily_bars().index if daily_index is None else daily_index


def to_daily(values: pd.Series) -> pd.Series:
    """Sum per-trade returns (indexed by exit time or day) onto the trading-day calendar."""
    cal = calendar()
    if len(values) == 0:
        return pd.Series(0.0, cal)
    day = trading_day(values.index) if values.index.tz is not None else values.index
    s = pd.Series(values.to_numpy(), day).groupby(level=0).sum()
    return s.reindex(cal, fill_value=0.0)


def print_table(rows: list[dict], cols: list[str] | None = None) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    cols = cols or ["strategy", "size", "train_days_traded", "train_avg_bp", "train_t", "train_ret_pct_yr",
                    "train_sharpe", "test_days_traded", "test_win_pct", "test_avg_bp", "test_t", "test_ret_pct_yr",
                    "test_sharpe", "test_max_dd_pct", "test_worst_day_pct", "test_daily_soft_breaches",
                    "test_max_hard_breached"]
    pd.set_option("display.width", 300)
    pd.set_option("display.max_columns", 50)
    print(df[cols].to_string(index=False), flush=True)
    return df
