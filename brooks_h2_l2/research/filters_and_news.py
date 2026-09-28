"""Would news filtering or practitioner-style context rules rescue H2/L2?

Each rule is one pre-stated hypothesis taken from what discretionary Brooks
traders say they do (see README). Results are split train/test at the same date
as experiment.py; judge a rule on train first, then check it holds on test.

Run from the repo root: python3 -m brooks_h2_l2.research.filters_and_news
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from brooks_h2_l2.backtest import Minutes, r_stats, simulate
from brooks_h2_l2.data import load_minutes, mid_bars
from brooks_h2_l2.experiment import MAX_HOLD
from brooks_h2_l2.strategy import Config, atr, ema, generate_signals

SPLIT = pd.Timestamp("2023-11-01", tz="UTC")
CONFIGS = {
    "5m + 1h filter, 1.5R": Config(timeframe="5min", htf="1h", rr=1.5, max_hold_bars=MAX_HOLD["5min"]),
    "15m London/NY, 1R": Config(timeframe="15min", htf=None, rr=1.0, session="london_ny", max_hold_bars=MAX_HOLD["15min"]),
    "1h, 1.5R": Config(timeframe="1h", htf=None, rr=1.5, max_hold_bars=MAX_HOLD["1h"]),
}


# ------------------------------------------------------------------ scheduled US data windows
def event_times(start: pd.Timestamp, end: pd.Timestamp) -> dict[str, np.ndarray]:
    """UTC instants of US release slots. Over-inclusive: every weekday 08:30 and 10:00 ET
    (NFP, CPI, PPI, claims, retail sales, GDP, PCE, ISM, JOLTS...) and every Wednesday
    14:00 ET (FOMC slot)."""
    days = pd.date_range(start.tz_convert("America/New_York").normalize().tz_localize(None),
                         end.tz_convert("America/New_York").normalize().tz_localize(None), freq="D")
    days = days[days.dayofweek < 5]
    def at(ds, hh, mm):
        local = (ds + pd.Timedelta(hours=hh, minutes=mm)).tz_localize("America/New_York", nonexistent="NaT", ambiguous="NaT")
        return local.dropna().tz_convert("UTC").asi8
    return {"0830": at(days, 8, 30), "1000": at(days, 10, 0), "fomc": at(days[days.dayofweek == 2], 14, 0)}


def near_event(ns: np.ndarray, events: np.ndarray, before_min: int, after_min: int) -> np.ndarray:
    """True where an event falls within [t - after, t + before] i.e. t in [event - before, event + after]."""
    idx = np.searchsorted(events, ns)
    hit = np.zeros(len(ns), dtype=bool)
    for j in (idx - 1, idx):
        ok = (j >= 0) & (j < len(events))
        delta = np.full(len(ns), np.nan)
        delta[ok] = ns[ok] - events[j[ok]]
        hit |= (delta >= -before_min * 60e9) & (delta <= after_min * 60e9)
    return hit


def news_block(sig: pd.DataFrame, ev: dict[str, np.ndarray]) -> np.ndarray:
    ns = pd.DatetimeIndex(sig["decision_time"]).as_unit("ns").asi8
    return ~(near_event(ns, ev["0830"], 60, 30) | near_event(ns, ev["1000"], 30, 30) | near_event(ns, ev["fomc"], 60, 60))


def exposed(trades: pd.DataFrame, ev: dict[str, np.ndarray]) -> np.ndarray:
    """Trade was open across a release instant (or filled within 30 min after one)."""
    allev = np.sort(np.concatenate(list(ev.values())))
    a = pd.DatetimeIndex(trades["fill_time"]).as_unit("ns").asi8 - 30 * 60_000_000_000
    b = pd.DatetimeIndex(trades["exit_time"]).as_unit("ns").asi8
    return np.searchsorted(allev, b, side="right") > np.searchsorted(allev, a, side="left")


# ------------------------------------------------------------------ oracle volatility-shock tag (look-ahead)
def shock_tag(trades: pd.DataFrame, raw: pd.DataFrame, mult: float = 5.0) -> np.ndarray:
    rng = (raw["bid_high"] - raw["bid_low"]).to_numpy()
    base = pd.Series(rng).rolling(1440, min_periods=300).median().shift(1).to_numpy()
    ns = raw.index.asi8
    a = np.searchsorted(ns, pd.DatetimeIndex(trades["fill_time"]).as_unit("ns").asi8)
    b = np.searchsorted(ns, pd.DatetimeIndex(trades["exit_time"]).as_unit("ns").asi8, side="right")
    return np.array([np.nanmax(rng[i:j] / base[i:j]) > mult if j > i else False for i, j in zip(a, b)])


# ------------------------------------------------------------------ practitioner context rules
def features(bars: pd.DataFrame, sig: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    i = bars.index.get_indexer(sig["bar_time"])
    e20 = ema(bars["close"], cfg.ema).to_numpy()[i]
    a = atr(bars, cfg.atr_period).to_numpy()[i]
    o, h, low, c = (bars[k].to_numpy()[i] for k in ("open", "high", "low", "close"))
    d = sig["direction"].to_numpy()
    pb_extreme = sig["stop"].to_numpy() + d * cfg.tick  # pullback low (long) / high (short)
    up, dn = bars["high"].diff(), -bars["low"].diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), bars.index).ewm(alpha=1 / 14, adjust=False).mean()
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), bars.index).ewm(alpha=1 / 14, adjust=False).mean()
    full_atr = atr(bars, 14)
    pdi, ndi = 100 * pdm / full_atr, 100 * ndm / full_atr
    adx = (100 * (pdi - ndi).abs() / (pdi + ndi)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()[i]
    rng = np.where(h - low > 0, h - low, np.nan)
    close_pos = np.where(d == 1, (c - low) / rng, (h - c) / rng)
    body = np.abs(c - o) / rng
    return pd.DataFrame({
        "ema_touch": d * (pb_extreme - e20) / a <= 0.25,   # pullback reached (or crossed) the EMA
        "adx25": adx >= 25,                                # trending, not a trading range
        "strong_bar": (close_pos >= 0.75) & (body >= 0.5) & (d * (c - o) > 0),
    })


def row(name: str, trades: pd.DataFrame) -> dict:
    t = pd.to_datetime(trades["signal_time"]) if len(trades) else pd.Series(dtype="datetime64[ns, UTC]")
    tr, te = r_stats(trades.loc[t < SPLIT, "r"].to_numpy()), r_stats(trades.loc[t >= SPLIT, "r"].to_numpy())
    return {"variant": name,
            "train_n": tr.get("trades", 0), "train_win": tr.get("win_rate_pct"), "train_exp": tr.get("expectancy_r"),
            "test_n": te.get("trades", 0), "test_win": te.get("win_rate_pct"), "test_exp": te.get("expectancy_r")}


def main() -> None:
    pd.set_option("display.width", 200)
    raw = load_minutes(start="2021-01-01")
    minutes = Minutes(raw)
    ev = event_times(raw.index[0], raw.index[-1])
    for label, cfg in CONFIGS.items():
        bars = mid_bars(raw, cfg.timeframe)
        sig = generate_signals(bars, cfg)
        f = features(bars, sig, cfg)
        base = simulate(minutes, sig, cfg)
        rows = [row("baseline", base)]

        # News: how do trades that sat through a release compare with the rest?
        exp_mask = exposed(base, ev)
        rows.append(row("  trades open across a US release", base[exp_mask]))
        rows.append(row("  trades clear of releases", base[~exp_mask]))
        rows.append(row("news filter: no entries near releases", simulate(minutes, sig[news_block(sig, ev)], cfg)))
        shock = shock_tag(base, raw)
        rows.append(row("ORACLE: drop trades hit by a 5x vol shock", base[~shock]))

        # Practitioner context rules, one at a time
        for col, name in (("ema_touch", "H2 at the EMA only"), ("adx25", "ADX >= 25 only"),
                          ("strong_bar", "strong signal bar only")):
            rows.append(row(name, simulate(minutes, sig[f[col].to_numpy()], cfg)))
        so = replace(cfg, scale_out_r=1.0, rr=2.0)
        rows.append(row("Brooks management: half at 1R, BE, runner 2R", simulate(minutes, generate_signals(bars, so), so)))

        # Everything together
        stack = f["ema_touch"].to_numpy() & f["adx25"].to_numpy() & f["strong_bar"].to_numpy() & news_block(sig, ev)
        sig_so = generate_signals(bars, so)
        f_so = features(bars, sig_so, so)
        stack_so = (f_so["ema_touch"].to_numpy() & f_so["adx25"].to_numpy() & f_so["strong_bar"].to_numpy()
                    & news_block(sig_so, ev))
        rows.append(row("ALL context rules + news filter", simulate(minutes, sig[stack], cfg)))
        rows.append(row("ALL + news + Brooks management", simulate(minutes, sig_so[stack_so], so)))

        print(f"\n=== {label}  (share of baseline trades hit by a vol shock: {shock.mean():.0%}, "
              f"open across a release: {exp_mask.mean():.0%})")
        print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
