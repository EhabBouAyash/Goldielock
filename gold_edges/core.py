"""Daily bars, costs, and the two simulators (always-in positions and setups)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from brooks_h2_l2.backtest import r_stats

TRAIN_END = pd.Timestamp("2018-01-01", tz="UTC")
SPREAD_BPS = 1.6
SLIPPAGE = 0.10
SWAP_PCT_YEAR = 5.0
DAILY_SOFT, DAILY_HARD = 2.5, 3.0
MAX_SOFT, MAX_HARD = 5.0, 6.0


def daily_bars(minutes: pd.DataFrame) -> pd.DataFrame:
    """Broker-day OHLC in New York time, session open 17:00 ET."""
    mid = pd.DataFrame({c: (minutes[f"bid_{c}"] + minutes[f"ask_{c}"]) / 2
                        for c in ("open", "high", "low", "close")})
    mid = mid.tz_convert("America/New_York")
    out = mid.resample("1D", label="left", closed="left", offset="17h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"})
    return out.dropna()


def atr(df: pd.DataFrame, n: int = 20) -> np.ndarray:
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def rsi(close: pd.Series, n: int = 2) -> np.ndarray:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(ad != 0, 100.0)
    out = out.where(au != 0, 0.0)
    out = out.where(~((au == 0) & (ad == 0)), 50.0)
    return out.to_numpy()


def _cost(px: float) -> float:
    return px * SPREAD_BPS / 1e4 + 2 * SLIPPAGE


def _line(r: np.ndarray) -> str:
    st = r_stats(np.asarray(r, dtype=float))
    if not st.get("trades"):
        return "no trades"
    ci = st.get("expectancy_ci95")
    ci_s = f" CI95={ci}" if ci else ""
    t = st.get("t_stat")
    t_s = f" t={t}" if t is not None else ""
    return (f"n={st['trades']} win={st['win_rate_pct']}% exp={st['expectancy_r']:+.3f}R "
            f"tot={st['total_r']:+.1f} dd={st['max_drawdown_r']:.1f}{t_s}{ci_s}")


def _split(times: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    t = pd.to_datetime(times)
    if t.dt.tz is None:
        t = t.dt.tz_localize("UTC")
    else:
        t = t.dt.tz_convert("UTC")
    return (t < TRAIN_END).to_numpy(), (t >= TRAIN_END).to_numpy()


def prop_path(r: np.ndarray, risk_pct: float) -> dict:
    pct = np.asarray(r, dtype=float) * risk_pct
    equity = np.cumsum(pct)
    dd = np.maximum.accumulate(np.maximum(equity, 0)) - equity
    return {
        "risk_pct": risk_pct,
        "return_pct": round(float(equity[-1]), 2),
        "max_dd_pct": round(float(dd.max()), 2),
        "worst_trade_pct": round(float(pct.min()), 2),
        "daily_soft_breaches": int((pct <= -DAILY_SOFT).sum()),
        "daily_hard_breaches": int((pct <= -DAILY_HARD).sum()),
        "max_soft_breached": bool(dd.max() >= MAX_SOFT),
        "max_hard_breached": bool(dd.max() >= MAX_HARD),
    }


def summarize(trades: pd.DataFrame, years: float) -> dict:
    if trades.empty:
        return {"n": 0}
    st = r_stats(trades["r"].to_numpy())
    longs = trades.loc[trades["dir"] == 1, "r"]
    shorts = trades.loc[trades["dir"] == -1, "r"]
    out = {
        "n": st["trades"],
        "win": st.get("win_rate_pct"),
        "exp": st.get("expectancy_r"),
        "t": st.get("t_stat"),
        "ci95": st.get("expectancy_ci95"),
        "pf": st.get("profit_factor"),
        "tot_r": st.get("total_r"),
        "maxdd_r": st.get("max_drawdown_r"),
        "per_year": round(st["trades"] / years, 1) if years else None,
        "long_n": int(len(longs)),
        "long_exp": round(float(longs.mean()), 3) if len(longs) else None,
        "short_n": int(len(shorts)),
        "short_exp": round(float(shorts.mean()), 3) if len(shorts) else None,
        "line": _line(trades["r"].to_numpy()),
    }
    if len(trades) >= 20:
        for risk in (0.25, 0.50):
            out[f"prop_{risk}"] = prop_path(trades["r"].to_numpy(), risk)
    return out


def walk_positions(df: pd.DataFrame, pos: np.ndarray, stop_k: float | None = 2.0,
                   swap: bool = True) -> pd.DataFrame:
    """pos[i] is the desired direction at the close of bar i, entered on the next open.

    A hard stop of stop_k * ATR(signal) is optional. Overnight financing is
    charged on calendar days held, as a fraction of the risk unit so the
    result stays in R.
    """
    O, H, L, C = (df[c].to_numpy() for c in ("open", "high", "low", "close"))
    a = atr(df)
    idx = df.index
    n = len(df)
    trades = []
    i = 0
    while i < n - 1:
        d = int(pos[i]) if np.isfinite(pos[i]) else 0
        if d == 0 or not np.isfinite(a[i]) or a[i] <= 0:
            i += 1
            continue
        j0 = i + 1
        fill = O[j0] + d * (O[j0] * SPREAD_BPS / 2e4 + SLIPPAGE)
        risk = (stop_k if stop_k else 2.0) * a[i]
        if risk <= 0:
            i += 1
            continue
        stop = fill - d * risk if stop_k else None
        exit_j, exit_px, reason = None, None, None
        for j in range(j0, n):
            if stop is not None:
                hit = L[j] <= stop if d == 1 else H[j] >= stop
                if hit:
                    gap = (O[j] - stop) * d <= 0
                    raw = O[j] if gap else stop
                    exit_j, exit_px, reason = j, raw - d * SLIPPAGE, "STOP"
                    break
            if pos[j] != d:
                if j + 1 < n:
                    exit_j = j + 1
                    exit_px = O[exit_j] - d * (O[exit_j] * SPREAD_BPS / 2e4 + SLIPPAGE)
                else:
                    exit_j, exit_px = j, C[j] - d * (C[j] * SPREAD_BPS / 2e4 + SLIPPAGE)
                reason = "SIGNAL"
                break
        if exit_j is None:
            exit_j, exit_px, reason = n - 1, C[-1] - d * (C[-1] * SPREAD_BPS / 2e4 + SLIPPAGE), "TIME"
        days = max((idx[exit_j] - idx[j0]).days, 0)
        swap_px = fill * SWAP_PCT_YEAR / 100 * days / 365 if swap else 0.0
        pnl = d * (exit_px - fill) - _cost(fill) - swap_px
        trades.append({
            "time": pd.Timestamp(idx[j0]).tz_convert("UTC") if idx.tz else pd.Timestamp(idx[j0], tz="UTC"),
            "exit_time": pd.Timestamp(idx[exit_j]).tz_convert("UTC") if idx.tz else pd.Timestamp(idx[exit_j], tz="UTC"),
            "dir": d, "r": pnl / risk, "days": days, "exit_reason": reason,
        })
        i = (exit_j - 1) if reason == "SIGNAL" and exit_j > j0 else exit_j
        if i <= j0:
            i = j0 + 1
    return pd.DataFrame(trades)


def walk_setups(df: pd.DataFrame, signals: list, valid: int = 1, max_hold: int = 10,
                trail: np.ndarray | None = None) -> pd.DataFrame:
    """signals: (bar, dir, entry, stop, kind[, last_bar]) decided at the close of `bar`.

    kind 'stop' waits for a break of `entry` on later bars. kind 'next_open'
    fills the next open. kind 'now' fills the signal bar itself (intrabar
    breakout). Stop first if a bar hits both. last_bar, if given, exits at
    that bar's close (Connors RSI recovery).
    """
    O, H, L, C = (df[c].to_numpy() for c in ("open", "high", "low", "close"))
    idx = df.index
    n = len(df)
    trades, busy = [], -1
    for s in signals:
        i, d, entry, stop, kind = s[:5]
        last_bar = s[5] if len(s) > 5 else -1
        if i <= busy:
            continue
        if kind == "now":
            j0 = i
            px = (max(entry, O[i]) if d == 1 else min(entry, O[i])) + d * SLIPPAGE
        elif kind == "next_open":
            if i + 1 >= n:
                continue
            j0 = i + 1
            px = O[j0] + d * (O[j0] * SPREAD_BPS / 2e4 + SLIPPAGE)
        else:
            j0 = px = None
            for j in range(i + 1, min(i + 1 + valid, n)):
                if (d == 1 and L[j] <= stop and H[j] < entry) or (d == -1 and H[j] >= stop and L[j] > entry):
                    break
                if (d == 1 and H[j] >= entry) or (d == -1 and L[j] <= entry):
                    j0 = j
                    px = (max(entry, O[j]) if d == 1 else min(entry, O[j])) + d * SLIPPAGE
                    break
            if j0 is None:
                continue
        risk = d * (px - stop)
        if risk <= 0:
            continue
        exit_j, exit_px, reason = None, None, None
        last = min(j0 + max_hold, n - 1)
        if last_bar >= 0:
            last = min(last, last_bar)
        for j in range(j0, last + 1):
            if j > j0 and (O[j] - stop) * d <= 0:
                exit_j, exit_px, reason = j, O[j] - d * SLIPPAGE, "STOP"
                break
            if (L[j] <= stop) if d == 1 else (H[j] >= stop):
                exit_j, exit_px, reason = j, stop - d * SLIPPAGE, "STOP"
                break
            if trail is not None and j > j0:
                lvl = trail[j]
                hit = L[j] <= lvl if d == 1 else H[j] >= lvl
                if hit:
                    raw = min(O[j], lvl) if d == 1 else max(O[j], lvl)
                    exit_j, exit_px, reason = j, raw - d * SLIPPAGE, "TRAIL"
                    break
        if exit_j is None:
            exit_j, exit_px, reason = last, C[last] - d * (C[last] * SPREAD_BPS / 2e4 + SLIPPAGE), "TIME"
        days = max((idx[exit_j] - idx[j0]).days, 0)
        swap_px = px * SWAP_PCT_YEAR / 100 * days / 365
        pnl = d * (exit_px - px) - _cost(px) - swap_px
        trades.append({
            "time": pd.Timestamp(idx[j0]).tz_convert("UTC") if idx.tz else pd.Timestamp(idx[j0], tz="UTC"),
            "exit_time": pd.Timestamp(idx[exit_j]).tz_convert("UTC") if idx.tz else pd.Timestamp(idx[exit_j], tz="UTC"),
            "dir": d, "r": pnl / risk, "days": days, "exit_reason": reason,
        })
        busy = exit_j
    return pd.DataFrame(trades)
