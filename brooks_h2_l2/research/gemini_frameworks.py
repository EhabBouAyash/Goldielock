"""All six Gemini-report frameworks on XAUUSD 2009-2026, at 15m / 1h / 4h / daily.

Replaces the 730-day GC=F screen in prototype_lab.py. Signals are computed on
mid-price bars built from HistData minutes. Bars are in New York time, and
4h/daily bars open at 17:00 ET, which is the broker day. Fills are at the bar
level: stop entries fill at max(level, open), stops and targets exit at the
level or at the open on a gap, and when a bar touches both, the stop counts
first. Each trade pays a 1.6bp spread plus $0.10 slippage on entry and exit.
Stops closer than 13bp are skipped, the same floor the H2/L2 study used.

The target multiple is chosen on 2009-2017 only; 2018-2026 is the check.

Run from the repo root: python3 -m brooks_h2_l2.research.gemini_frameworks
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from brooks_h2_l2.backtest import r_stats
from brooks_h2_l2.data import load_minutes

RESULTS = Path(__file__).resolve().parents[1] / "results"
TRAIN_END = pd.Timestamp("2018-01-01", tz="UTC")
SPREAD_BPS = 1.6
SLIPPAGE = 0.10
MIN_RISK_BPS = 13.0
TICK = 0.10
RRS = (1.0, 1.5, 2.0, 3.0)
TIMEFRAMES = {"15m": ("15min", None, 64), "1h": ("1h", None, 48), "4h": ("4h", "17h", 30), "1d": ("1D", "17h", 20)}


# ---------------------------------------------------------------- bars and indicators
def bars(minutes: pd.DataFrame, rule: str, offset: str | None) -> pd.DataFrame:
    mid = pd.DataFrame({c: (minutes[f"bid_{c}"] + minutes[f"ask_{c}"]) / 2 for c in ("open", "high", "low", "close")})
    mid = mid.tz_convert("America/New_York")
    kw = {"offset": offset} if offset else {}
    out = mid.resample(rule, label="left", closed="left", **kw).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"})
    return out.dropna()


def ema(s: pd.Series, n: int) -> np.ndarray:
    return s.ewm(span=n, adjust=False).mean().to_numpy()


def _rma(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1 / n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return _rma(tr, n).to_numpy()


def adx(df: pd.DataFrame, n: int = 14):
    up, dn = df.high.diff(), -df.low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), df.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), df.index)
    a = pd.Series(atr(df, n), df.index)
    pdi, ndi = 100 * _rma(pdm, n) / a, 100 * _rma(ndm, n) / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return _rma(dx.fillna(0), n).to_numpy(), pdi.to_numpy(), ndi.to_numpy()


# ---------------------------------------------------------------- strategies
# A signal is (bar, direction, entry, stop, kind, last_bar). kind "stop"/"limit"
# orders work from the next bar for up to `valid` bars; "now" fills on the signal
# bar itself (Turtle breakouts). last_bar caps the holding period (-1 = none).

def holy_grail(df, adx_min=30):
    e = ema(df.close, 20)
    a, pdi, ndi = adx(df)
    H, L = df.high.to_numpy(), df.low.to_numpy()
    sig = []
    for i in range(60, len(df)):
        if not (a[i] > adx_min and a[i] > a[i - 3]):
            continue
        if pdi[i] > ndi[i] and L[i] <= e[i] and L[i - 1] > e[i - 1]:
            sig.append((i, 1, H[i] + TICK, L[i] - TICK, "stop", -1))
        elif ndi[i] > pdi[i] and H[i] >= e[i] and H[i - 1] < e[i - 1]:
            sig.append((i, -1, L[i] - TICK, H[i] + TICK, "stop", -1))
    return sig


def grimes_pullback(df, look=20, recent=10):
    e20, e50 = ema(df.close, 20), ema(df.close, 50)
    H, L, C = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    new_high = (df.high >= df.high.rolling(look).max()).rolling(recent).max().fillna(0).to_numpy().astype(bool)
    new_low = (df.low <= df.low.rolling(look).min()).rolling(recent).max().fillna(0).to_numpy().astype(bool)
    sig = []
    for i in range(60, len(df)):
        if e20[i] > e50[i] and new_high[i - 1] and L[i] <= e20[i] < C[i]:
            sig.append((i, 1, H[i] + TICK, L[i - 5:i + 1].min() - TICK, "stop", -1))
        elif e20[i] < e50[i] and new_low[i - 1] and H[i] >= e20[i] > C[i]:
            sig.append((i, -1, L[i] - TICK, H[i - 5:i + 1].max() + TICK, "stop", -1))
    return sig


def brooks_h2(df):
    e = ema(df.close, 20)
    slope = e - np.roll(e, 5)
    H, L, C = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    sig = []
    leg_hi, pb, hcount, pb_low = -np.inf, False, 0, np.inf
    leg_lo, pbs, lcount, pb_high = np.inf, False, 0, -np.inf
    for i in range(30, len(df)):
        if H[i] > leg_hi:
            leg_hi, pb, hcount, pb_low = H[i], False, 0, np.inf
        else:
            pb = pb or H[i] < H[i - 1]
            if pb:
                pb_low = min(pb_low, L[i])
                hcount += H[i] > H[i - 1]
        if pb and hcount == 1 and H[i] <= H[i - 1] and slope[i] > 0 and C[i] > e[i] * 0.999 and C[i] > (H[i] + L[i]) / 2:
            sig.append((i, 1, H[i] + TICK, pb_low - TICK, "stop", -1))
        if L[i] < leg_lo:
            leg_lo, pbs, lcount, pb_high = L[i], False, 0, -np.inf
        else:
            pbs = pbs or L[i] > L[i - 1]
            if pbs:
                pb_high = max(pb_high, H[i])
                lcount += L[i] < L[i - 1]
        if pbs and lcount == 1 and L[i] >= L[i - 1] and slope[i] < 0 and C[i] < e[i] * 1.001 and C[i] < (H[i] + L[i]) / 2:
            sig.append((i, -1, L[i] - TICK, pb_high + TICK, "stop", -1))
        if i % 40 == 0:
            leg_hi, leg_lo = H[i], L[i]
    return sig


def failure_test(df, look=20, min_age=3, trend_filter=False):
    e50 = ema(df.close, 50)
    H, L, C = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    sig = []
    for i in range(look + 2, len(df)):
        wl, wh = L[i - look:i], H[i - look:i]
        pl, ph = wl.min(), wh.max()
        if L[i] < pl < C[i] and look - 1 - wl.argmin() >= min_age and (not trend_filter or C[i] > e50[i]):
            sig.append((i, 1, H[i] + TICK, L[i] - TICK, "stop", -1))
        elif H[i] > ph > C[i] and look - 1 - wh.argmax() >= min_age and (not trend_filter or C[i] < e50[i]):
            sig.append((i, -1, L[i] - TICK, H[i] + TICK, "stop", -1))
    return sig


def anti(df):
    fast = (df.close.rolling(3).mean() - df.close.rolling(10).mean()).to_numpy()
    slow = pd.Series(fast).rolling(16).mean().to_numpy()
    H, L = df.high.to_numpy(), df.low.to_numpy()
    sig = []
    for i in range(40, len(df)):
        hook_up = fast[i] > fast[i - 1] < fast[i - 2] < fast[i - 3]
        hook_dn = fast[i] < fast[i - 1] > fast[i - 2] > fast[i - 3]
        if slow[i] > slow[i - 1] and hook_up:
            sig.append((i, 1, H[i] + TICK, L[i - 4:i + 1].min() - TICK, "stop", -1))
        elif slow[i] < slow[i - 1] and hook_dn:
            sig.append((i, -1, L[i] - TICK, H[i - 4:i + 1].max() + TICK, "stop", -1))
    return sig


def ict_sweep_mss_fvg(df, look=20, window=12):
    """Sweep of a 20-bar extreme, then a close through the pre-sweep swing (MSS) inside the
    London (02-05 ET) or New York (07-10 ET) kill zone, then a limit at the displacement FVG."""
    H, L, C, O = (df[c].to_numpy() for c in ("high", "low", "close", "open"))
    body = np.abs(C - O)
    avgb = pd.Series(body).rolling(14).mean().to_numpy()
    hrs = df.index.hour
    kz = ((hrs >= 2) & (hrs < 5)) | ((hrs >= 7) & (hrs < 10))
    sig, n = [], len(df)
    for i in range(look + 2, n - 1):
        if L[i] < L[i - look:i].min():
            ref = H[max(0, i - 5):i + 1].max()
            for j in range(i + 1, min(i + window, n)):
                if L[j] < L[i]:
                    break
                if C[j] > ref:
                    for k in range(j, i + 1, -1):
                        if L[k] > H[k - 2] and body[k - 1] > 1.2 * avgb[k - 1] and C[k - 1] > O[k - 1]:
                            if kz[j]:
                                sig.append((j, 1, L[k], L[i] - TICK, "limit", -1))
                            break
                    break
        if H[i] > H[i - look:i].max():
            ref = L[max(0, i - 5):i + 1].min()
            for j in range(i + 1, min(i + window, n)):
                if H[j] > H[i]:
                    break
                if C[j] < ref:
                    for k in range(j, i + 1, -1):
                        if H[k] < L[k - 2] and body[k - 1] > 1.2 * avgb[k - 1] and C[k - 1] < O[k - 1]:
                            if kz[j]:
                                sig.append((j, -1, H[k], H[i] + TICK, "limit", -1))
                            break
                    break
    sig.sort(key=lambda s: s[0])
    return sig


def shannon_avwap(df, stage=150, anchor_look=100, min_age=5):
    """Stage 2 (close above a rising 150-bar SMA) pullback to the average price anchored at the
    lowest low of the last 100 bars; Stage 4 shorts mirror it. Spot gold has no volume, so the
    anchored average is time-weighted."""
    H, L, C = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    tp = (H + L + C) / 3
    cs = np.concatenate([[0.0], np.cumsum(tp)])
    sma = df.close.rolling(stage).mean().to_numpy()
    a = atr(df)
    sig = []
    for i in range(stage + 20, len(df)):
        up = C[i] > sma[i] > sma[i - 20]
        dn = C[i] < sma[i] < sma[i - 20]
        if not (up or dn):
            continue
        lo, hi = i - anchor_look + 1, i + 1
        k = lo + (L[lo:hi].argmin() if up else H[lo:hi].argmax())
        if i - k < min_age:
            continue
        vw = (cs[i + 1] - cs[k]) / (i - k + 1)
        prev = (cs[i] - cs[k]) / (i - k)
        ext = C[max(k, i - 20):i]
        if up and L[i] <= vw < C[i] and L[i - 1] > prev and (ext - vw).max() > a[i]:
            sig.append((i, 1, H[i] + TICK, min(L[i], vw) - 0.25 * a[i], "stop", -1))
        elif dn and H[i] >= vw > C[i] and H[i - 1] < prev and (vw - ext).max() > a[i]:
            sig.append((i, -1, L[i] - TICK, max(H[i], vw) + 0.25 * a[i], "stop", -1))
    return sig


def raschke_80_20(df):
    """Prior broker day opened in its top 20% and closed in its bottom 20% (or the mirror).
    Today trades through the prior extreme and a bar closes back inside: stop entry past that
    bar, stop at today's extreme, out by the end of the day."""
    day = (df.index - pd.Timedelta(hours=17)).normalize()
    g = df.groupby(day)
    d = pd.DataFrame({"o": g.open.first(), "h": g.high.max(), "l": g.low.min(), "c": g.close.last()})
    rng = (d.h - d.l).replace(0, np.nan)
    long_q = ((d.o - d.l) / rng >= 0.8) & ((d.c - d.l) / rng <= 0.2)
    short_q = ((d.o - d.l) / rng <= 0.2) & ((d.c - d.l) / rng >= 0.8)
    prev = pd.DataFrame({"pl": d.l.shift(), "ph": d.h.shift(), "lq": long_q.shift(fill_value=False),
                         "sq": short_q.shift(fill_value=False)})
    ctx = prev.reindex(day)
    pl, ph = ctx.pl.to_numpy(), ctx.ph.to_numpy()
    lq, sq = ctx.lq.to_numpy(dtype=bool), ctx.sq.to_numpy(dtype=bool)
    H, L, C = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    run_lo = df.low.groupby(day).cummin().to_numpy()
    run_hi = df.high.groupby(day).cummax().to_numpy()
    day_arr = day.to_numpy()
    last_bar = pd.Series(np.arange(len(df))).groupby(day_arr).transform("max").to_numpy()
    sig, done = [], set()
    for i in range(1, len(df)):
        if day_arr[i] in done:
            continue
        if lq[i] and run_lo[i] < pl[i] < C[i]:
            sig.append((i, 1, H[i] + TICK, run_lo[i] - TICK, "stop", int(last_bar[i])))
            done.add(day_arr[i])
        elif sq[i] and run_hi[i] > ph[i] > C[i]:
            sig.append((i, -1, L[i] - TICK, run_hi[i] + TICK, "stop", int(last_bar[i])))
            done.add(day_arr[i])
    return sig


def turtle(df, entry=20, exit_=10):
    """Donchian breakout filled intraday at the channel, 2N stop, exit on the exit-channel touch."""
    N = pd.Series(atr(df, 20)).shift().to_numpy()
    hh = df.high.rolling(entry).max().shift().to_numpy()
    ll = df.low.rolling(entry).min().shift().to_numpy()
    H, L = df.high.to_numpy(), df.low.to_numpy()
    sig = []
    for i in range(entry + 21, len(df)):
        if H[i] > hh[i] and H[i - 1] <= hh[i - 1]:
            sig.append((i, 1, hh[i] + TICK, hh[i] + TICK - 2 * N[i], "now", -1))
        elif L[i] < ll[i] and L[i - 1] >= ll[i - 1]:
            sig.append((i, -1, ll[i] - TICK, ll[i] - TICK + 2 * N[i], "now", -1))
    trail = (df.low.rolling(exit_).min().shift().to_numpy(), df.high.rolling(exit_).max().shift().to_numpy())
    return sig, trail


# ---------------------------------------------------------------- simulator
def _resolve(arr, s, rr, valid, max_hold, trail):
    O, H, L, C = arr
    n = len(C)
    i, d, entry, stop, kind, cap = s
    if kind == "now":
        j0, px = i, (max(entry, O[i]) if d == 1 else min(entry, O[i]))
    else:
        j0 = px = None
        for j in range(i + 1, min(i + 1 + valid, n)):
            if kind == "stop":
                if (d == 1 and L[j] <= stop and H[j] < entry) or (d == -1 and H[j] >= stop and L[j] > entry):
                    return None
                if (d == 1 and H[j] >= entry) or (d == -1 and L[j] <= entry):
                    j0, px = j, (max(entry, O[j]) if d == 1 else min(entry, O[j]))
                    break
            elif (d == 1 and L[j] <= entry) or (d == -1 and H[j] >= entry):
                j0, px = j, (min(entry, O[j]) if d == 1 else max(entry, O[j]))
                break
        if j0 is None:
            return None
    risk = d * (px - stop)
    if risk <= 0 or risk < px * MIN_RISK_BPS / 1e4:
        return None
    target = px + d * rr * risk if rr else None
    last = min(j0 + max_hold, n - 1)
    if cap >= 0:
        last = min(last, cap)
    exit_px, k = None, j0
    for k in range(j0, last + 1):
        if k > j0 and (O[k] - stop) * d <= 0:
            exit_px = O[k]
            break
        if (L[k] <= stop) if d == 1 else (H[k] >= stop):
            exit_px = stop
            break
        if target is not None and k > j0:
            if (O[k] - target) * d >= 0:
                exit_px = O[k]
                break
            if (H[k] >= target) if d == 1 else (L[k] <= target):
                exit_px = target
                break
        if trail is not None and k > j0:
            lvl = trail[0][k] if d == 1 else trail[1][k]
            if (L[k] <= lvl) if d == 1 else (H[k] >= lvl):
                exit_px = min(O[k], lvl) if d == 1 else max(O[k], lvl)
                break
    if exit_px is None:
        k, exit_px = last, C[last]
    cost = px * SPREAD_BPS / 1e4 + 2 * SLIPPAGE
    gross = d * (exit_px - px) / risk
    return {"entry_bar": j0, "exit_bar": k, "dir": d, "r": gross - cost / risk, "gross_r": gross,
            "px": px, "risk": risk}


def simulate(df, signals, rr, valid=3, max_hold=48, trail=None, skip_after_win=False):
    arr = tuple(df[c].to_numpy() for c in ("open", "high", "low", "close"))
    trades, busy, last_win = [], -1, False
    for s in signals:
        if s[0] <= busy and not skip_after_win:
            continue
        t = _resolve(arr, s, rr, valid, max_hold, trail)
        if t is None:
            continue
        take = s[0] > busy and not (skip_after_win and last_win)
        last_win = t["r"] > 0
        if take:
            trades.append(t)
            busy = t["exit_bar"]
    out = pd.DataFrame(trades, columns=["entry_bar", "exit_bar", "dir", "r", "gross_r", "px", "risk"])
    out["time"] = df.index[out.entry_bar.to_numpy()].tz_convert("UTC") if len(out) else pd.Series(dtype="datetime64[ns, UTC]")
    out["exit_time"] = df.index[out.exit_bar.to_numpy()].tz_convert("UTC") if len(out) else pd.Series(dtype="datetime64[ns, UTC]")
    return out


# ---------------------------------------------------------------- run
STRATEGIES = {
    "Grimes pullback": grimes_pullback,
    "Grimes failure test": failure_test,
    "Grimes failure test + EMA50": lambda d: failure_test(d, trend_filter=True),
    "Brooks H2/L2 (prototype)": brooks_h2,
    "Raschke Holy Grail": holy_grail,
    "Raschke Anti": anti,
    "Raschke 80-20": raschke_80_20,
    "ICT sweep+MSS+FVG": ict_sweep_mss_fvg,
    "Shannon AVWAP": shannon_avwap,
}


def _row(tf, name, rr, period, t, years):
    st = r_stats(t.r.to_numpy()) if len(t) else {"trades": 0}
    row = {"tf": tf, "strategy": name, "rr": rr, "period": period, "n": st["trades"]}
    if not st["trades"]:
        return row
    longs, shorts = t[t.dir == 1].r, t[t.dir == -1].r
    row.update(win=st["win_rate_pct"], exp=st["expectancy_r"], gross_exp=round(float(t.gross_r.mean()), 3),
               t=st.get("t_stat"), pf=st["profit_factor"], maxdd_r=st["max_drawdown_r"],
               per_year=round(st["trades"] / years, 1),
               long_n=len(longs), long_exp=round(float(longs.mean()), 3) if len(longs) else None,
               short_n=len(shorts), short_exp=round(float(shorts.mean()), 3) if len(shorts) else None)
    return row


SWAP_PCT_YEAR = 5.0  # overnight financing on CFD gold, % of notional per year, charged both sides
ACCOUNT = 25_000
DAILY_SOFT, MAX_HARD = 2.5, 6.0  # FundedNext Stellar 1-Step, % of initial balance


def _mark_to_market(df, trades, risk_pct):
    """Daily equity in % of the initial balance, with open trades marked at each close."""
    C = df.close.to_numpy()
    pnl = np.zeros(len(df))
    for t in trades.itertuples():
        path = C[t.entry_bar:t.exit_bar + 1].copy()
        path[0] = t.px
        steps = t.dir * np.diff(path) / t.risk
        pnl[t.entry_bar + 1:t.exit_bar + 1] += steps
        final = t.r - steps.sum()
        pnl[t.exit_bar] += final
    return np.cumsum(pnl * risk_pct), pnl * risk_pct


def daily_trend(minutes: pd.DataFrame) -> pd.DataFrame:
    df = bars(minutes, "1D", "17h")
    rows = []
    for entry, exit_ in ((20, 10), (40, 15), (55, 20), (80, 25), (100, 30)):
        sig, trail = turtle(df, entry, exit_)
        for side in ("both", "long only"):
            s = sig if side == "both" else [x for x in sig if x[1] == 1]
            t = simulate(df, s, None, valid=1, max_hold=10**9, trail=trail)
            days = (t.exit_time - t.time).dt.days.to_numpy()
            t["r_swap"] = t.r - t.px * SWAP_PCT_YEAR / 100 * days / 365 / t.risk
            t["days"] = days
            for period, g in (("train", t[t.time < TRAIN_END]), ("test", t[t.time >= TRAIN_END])):
                st = r_stats(g.r_swap.to_numpy())
                row = {"entry": entry, "exit": exit_, "side": side, "period": period, "n": st["trades"],
                       "win": st.get("win_rate_pct"), "exp_no_swap": round(float(g.r.mean()), 3),
                       "exp": st.get("expectancy_r"), "t": st.get("t_stat"), "ci95": st.get("expectancy_ci95"),
                       "avg_days": round(float(g.days.mean()), 1)}
                sub = df.index.tz_convert("UTC") < TRAIN_END if period == "train" else df.index.tz_convert("UTC") >= TRAIN_END
                years = sub.sum() / 252
                g2 = g.copy()
                g2["r"] = g2.r_swap
                for risk in (0.5, 1.0):
                    eq, day_pnl = _mark_to_market(df, g2, risk)
                    eq = eq[sub]
                    dd = np.maximum.accumulate(np.maximum(eq - eq[0], 0)) - (eq - eq[0])
                    row[f"ret%/yr@{risk}"] = round(float((eq[-1] - eq[0]) / years), 2)
                    row[f"maxdd%@{risk}"] = round(float(dd.max()), 2)
                    row[f"worst_day%@{risk}"] = round(float(day_pnl[sub].min()), 2)
                    row[f"daily_breach@{risk}"] = int((day_pnl[sub] <= -DAILY_SOFT).sum())
                rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    minutes = load_minutes()
    trend = daily_trend(minutes)
    trend.to_csv(RESULTS / "gemini_daily_trend.csv", index=False)
    pd.set_option("display.width", 250)
    print(f"Daily Donchian trend, swap {SWAP_PCT_YEAR}%/yr of notional")
    print(trend.to_string(index=False), flush=True)
    first, last = minutes.index[0], minutes.index[-1]
    yrs_train = (TRAIN_END - first).days / 365.25
    yrs_test = (last - TRAIN_END).days / 365.25
    print(f"data {first:%Y-%m-%d} -> {last:%Y-%m-%d}; train {yrs_train:.1f}y, test {yrs_test:.1f}y", flush=True)
    rows = []
    for tf, (rule, offset, hold) in TIMEFRAMES.items():
        df = bars(minutes, rule, offset)
        print(f"\n== {tf}: {len(df)} bars", flush=True)
        runs = []
        for name, fn in STRATEGIES.items():
            if name == "Raschke 80-20" and tf == "1d":
                continue
            sig = fn(df)
            valid = 10 if name.startswith("ICT") else 3
            for rr in RRS:
                runs.append((name, rr, simulate(df, sig, rr, valid=valid, max_hold=hold)))
        for entry, exit_, label in ((20, 10, "Turtle S1 20/10"), (55, 20, "Turtle S2 55/20")):
            sig, trail = turtle(df, entry, exit_)
            runs.append((label, "trail", simulate(df, sig, None, valid=1, max_hold=10**9, trail=trail,
                                                  skip_after_win=label.startswith("Turtle S1"))))
        for name, rr, t in runs:
            train, test = t[t.time < TRAIN_END], t[t.time >= TRAIN_END]
            rows.append(_row(tf, name, rr, "train", train, yrs_train))
            rows.append(_row(tf, name, rr, "test", test, yrs_test))
    res = pd.DataFrame(rows)
    res.to_csv(RESULTS / "gemini_frameworks.csv", index=False)

    tr = res[(res.period == "train") & (res.n >= 30)]
    best = tr.loc[tr.groupby(["tf", "strategy"]).exp.idxmax(), ["tf", "strategy", "rr", "exp"]]
    best = best.rename(columns={"exp": "train_exp"})
    te = res[res.period == "test"]
    summary = best.merge(te, on=["tf", "strategy", "rr"]).sort_values(["tf", "exp"], ascending=[True, False])
    cols = ["tf", "strategy", "rr", "train_exp", "n", "per_year", "win", "exp", "gross_exp", "t", "pf",
            "maxdd_r", "long_exp", "short_exp"]
    summary[cols].to_csv(RESULTS / "gemini_frameworks_selected.csv", index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)
    print("\nTarget chosen on 2009-2017 -> 2018-2026 result")
    print(summary[cols].to_string(index=False))


if __name__ == "__main__":
    main()
