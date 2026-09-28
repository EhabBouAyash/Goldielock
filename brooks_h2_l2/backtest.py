"""Order simulation on 1-minute bid/ask candles and performance statistics."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategy import Config


@dataclass(frozen=True)
class Costs:
    slippage: float = 0.10     # $/oz on each stop-order fill (entry and protective stop)
    commission: float = 0.0    # $/oz round trip, on top of the bid/ask spread


class Minutes:
    """Column arrays of the 1-minute bid/ask history for fast walking."""

    def __init__(self, frame: pd.DataFrame):
        self.ns = frame.index.asi8
        for side in ("bid", "ask"):
            for col in ("open", "high", "low", "close"):
                setattr(self, f"{side}_{col}", frame[f"{side}_{col}"].to_numpy())
        self.index = frame.index


def simulate(minutes: Minutes, signals: pd.DataFrame, cfg: Config, costs: Costs = Costs()) -> pd.DataFrame:
    """Stop-entry orders filled on the ask (longs) or bid (shorts); exits on the opposite side.

    Conservative rules: a minute that touches both stop and target is a stop; the
    target cannot fill in the entry minute; the order is cancelled if the stop
    trades before the entry. One position at a time.
    """
    bar = pd.Timedelta(cfg.timeframe).value
    valid_ns = cfg.valid_bars * bar
    hold_ns = cfg.max_hold_bars * bar
    m = minutes
    n = len(m.ns)
    rows = []
    busy_until = np.iinfo(np.int64).min
    starts = np.searchsorted(m.ns, pd.DatetimeIndex(signals["decision_time"]).as_unit("ns").asi8)
    for sig, start in zip(signals.itertuples(index=False), starts):
        t0 = sig.decision_time.value
        if t0 < busy_until or start >= n:
            continue
        d, entry, stop, target = sig.direction, sig.entry, sig.stop, sig.target
        # --- entry
        fill_k = fill_px = None
        k = start
        while k < n and m.ns[k] < t0 + valid_ns:
            if d == 1:
                stop_hit, trig = m.bid_low[k] <= stop, m.ask_high[k] >= entry
                if stop_hit and not trig:
                    break
                if trig:
                    fill_k, fill_px = k, max(entry, m.ask_open[k]) + costs.slippage
                    break
            else:
                stop_hit, trig = m.ask_high[k] >= stop, m.bid_low[k] <= entry
                if stop_hit and not trig:
                    break
                if trig:
                    fill_k, fill_px = k, min(entry, m.bid_open[k]) - costs.slippage
                    break
            k += 1
        if fill_k is None:
            continue
        # --- exit (longs exit on the bid, shorts on the ask)
        if d == 1:
            ex_open, adverse, favorable, ex_close = m.bid_open, m.bid_low, m.bid_high, m.bid_close
        else:
            ex_open, adverse, favorable, ex_close = m.ask_open, m.ask_high, m.ask_low, m.ask_close
        partial = entry + d * cfg.scale_out_r * sig.risk if cfg.scale_out_r else None
        half_px = None
        stop_now = stop
        exit_px = exit_k = reason = None
        deadline = m.ns[fill_k] + hold_ns
        k = fill_k
        while k < n and m.ns[k] < deadline:
            if d * (adverse[k] - stop_now) <= 0:
                worse = min(d * stop_now, d * ex_open[k]) * d
                exit_px, reason = worse - d * costs.slippage, "STOP" if half_px is None else "BREAKEVEN"
            elif k > fill_k and d * (favorable[k] - target) >= 0:
                exit_px, reason = d * max(d * target, d * ex_open[k]), "TARGET"
            elif k > fill_k and partial is not None and half_px is None and d * (favorable[k] - partial) >= 0:
                half_px, stop_now = partial, fill_px
                # Unknown order inside the minute: assume the breakeven stop was hit too if it traded.
                if d * (adverse[k] - stop_now) <= 0:
                    exit_px, reason = stop_now - d * costs.slippage, "BREAKEVEN"
            if reason:
                exit_k = k
                break
            k += 1
        if exit_k is None:
            exit_k = max(k - 1, fill_k)
            exit_px = ex_close[exit_k]
            reason = "TIME"
        move = d * (exit_px - fill_px)
        if half_px is not None:
            move = 0.5 * d * (half_px - fill_px) + 0.5 * move
        pnl = move - costs.commission
        rows.append({
            "signal_time": sig.decision_time,
            "fill_time": m.index[fill_k],
            "exit_time": m.index[exit_k],
            "direction": "LONG" if d == 1 else "SHORT",
            "entry": round(entry, 3),
            "fill": round(fill_px, 3),
            "stop": round(stop, 3),
            "target": round(target, 3),
            "exit": round(exit_px, 3),
            "exit_reason": reason,
            "risk": round(sig.risk, 3),
            "spread_at_fill": round(m.ask_close[fill_k] - m.bid_close[fill_k], 3),
            "r": pnl / sig.risk,
        })
        busy_until = m.ns[exit_k] + 60_000_000_000
    return pd.DataFrame(rows)


def r_stats(r: np.ndarray, months: float | None = None, seed: int = 7) -> dict:
    r = np.asarray(r, dtype=float)
    if r.size == 0:
        return {"trades": 0}
    wins, losses = r[r > 0], r[r <= 0]
    equity = np.cumsum(r)
    drawdown = np.maximum.accumulate(np.maximum(equity, 0)) - equity
    streak = run = 0
    for x in r:
        run = run + 1 if x <= 0 else 0
        streak = max(streak, run)
    out = {
        "trades": int(r.size),
        "win_rate_pct": round(100 * wins.size / r.size, 1),
        "expectancy_r": round(float(r.mean()), 3),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else None,
        "total_r": round(float(r.sum()), 1),
        "max_drawdown_r": round(float(drawdown.max()), 1),
        "longest_losing_streak": int(streak),
    }
    if months:
        out["trades_per_month"] = round(r.size / months, 1)
    if r.size >= 20:
        rng = np.random.default_rng(seed)
        boots = rng.choice(r, size=(5000, r.size)).mean(axis=1)
        out["expectancy_ci95"] = [round(float(np.percentile(boots, 2.5)), 3), round(float(np.percentile(boots, 97.5)), 3)]
        out["t_stat"] = round(float(r.mean() / (r.std(ddof=1) / np.sqrt(r.size))), 2)
    return out


def prop_check(trades: pd.DataFrame, risk_pct: float = 0.5, daily_limit: float = 5.0, total_limit: float = 10.0) -> dict:
    """Fixed-fractional (non-compounding) account path against FundedNext-style loss limits."""
    if trades.empty:
        return {}
    pct = trades["r"].to_numpy() * risk_pct
    equity = np.cumsum(pct)
    dd = np.maximum.accumulate(np.maximum(equity, 0)) - equity
    daily = pd.Series(pct, index=pd.to_datetime(trades["exit_time"]).dt.floor("D")).groupby(level=0).sum()
    return {
        "risk_per_trade_pct": risk_pct,
        "return_pct": round(float(equity[-1]), 2),
        "max_drawdown_pct": round(float(dd.max()), 2),
        "worst_day_pct": round(float(daily.min()), 2),
        "daily_limit_breaches": int((daily <= -daily_limit).sum()),
        "total_limit_breached": bool(dd.max() >= total_limit),
    }


def breakdown(trades: pd.DataFrame, key: pd.Series) -> dict:
    return {str(k): r_stats(g["r"].to_numpy()) for k, g in trades.groupby(key)}
