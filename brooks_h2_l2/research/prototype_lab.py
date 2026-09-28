"""First-pass screen of the Gemini report's strategies on GC=F futures bars (fixed $0.40 cost).

Superseded for Brooks H2/L2 by the package modules, which use XAUUSD bid/ask minutes.
Run from the repo root: python3 brooks_h2_l2/research/prototype_lab.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

COST = 0.40  # $/oz round trip (spread + slippage), same allowance as supply_demand_strat
TICK = 0.10


# ---------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def atr(df, n=14):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def adx(df, n=14):
    up = df.high.diff()
    dn = -df.low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), df.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), df.index)
    a = atr(df, n)
    pdi = 100 * rma(pdm, n) / a
    ndi = 100 * rma(ndm, n) / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return rma(dx.fillna(0), n), pdi, ndi


# ---------------------------------------------------------------- simulator
def simulate(df, signals, rr=None, valid=3, max_hold=48, trail=None):
    """signals: list of (i, dir, entry, stop, kind) decided at close of bar i.
    kind 'stop' = stop-entry order, 'limit' = limit order.  Exit at rr*R target, stop,
    or time.  If trail is a pd.Series pair (long_exit, short_exit) exit on close through it."""
    H, L, C, O = df.high.values, df.low.values, df.close.values, df.open.values
    n = len(df)
    trades = []
    busy_until = -1
    for (i, d, entry, stop, kind) in signals:
        if i <= busy_until or i + 1 >= n:
            continue
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + d * rr * risk if rr else None
        fill = None
        for j in range(i + 1, min(i + 1 + valid, n)):
            # cancel if protective stop trades before entry
            if kind == "stop":
                if (d == 1 and L[j] <= stop and H[j] < entry) or (d == -1 and H[j] >= stop and L[j] > entry):
                    break
                if (d == 1 and H[j] >= entry) or (d == -1 and L[j] <= entry):
                    px = max(entry, O[j]) if d == 1 else min(entry, O[j])  # gap fill
                    fill = (j, px)
                    break
            else:
                if (d == 1 and L[j] <= entry) or (d == -1 and H[j] >= entry):
                    px = min(entry, O[j]) if d == 1 else max(entry, O[j])
                    fill = (j, px)
                    break
        if fill is None:
            continue
        j0, px = fill
        exit_px, exit_j, reason = None, None, None
        for k in range(j0, min(j0 + max_hold, n)):
            hit_stop = L[k] <= stop if d == 1 else H[k] >= stop
            hit_tgt = target is not None and k > j0 and (H[k] >= target if d == 1 else L[k] <= target)
            if hit_stop:
                exit_px, exit_j, reason = stop, k, "STOP"
                break
            if hit_tgt:
                exit_px, exit_j, reason = target, k, "TARGET"
                break
            if trail is not None and k > j0:
                lvl = trail[0].iloc[k - 1] if d == 1 else trail[1].iloc[k - 1]
                if (d == 1 and C[k] < lvl) or (d == -1 and C[k] > lvl):
                    exit_px, exit_j, reason = C[k], k, "TRAIL"
                    break
        if exit_px is None:
            exit_j = min(j0 + max_hold, n) - 1
            exit_px, reason = C[exit_j], "TIME"
        r = (d * (exit_px - px) - COST) / risk
        trades.append((df.index[j0], d, r, reason))
        busy_until = exit_j
    return pd.DataFrame(trades, columns=["time", "dir", "r", "exit"])


def stats(t):
    if t.empty:
        return dict(n=0)
    r = t.r.values
    w = r[r > 0].sum()
    lo = -r[r <= 0].sum()
    eq = np.cumsum(r)
    dd = (np.maximum.accumulate(eq) - eq).max()
    return dict(n=len(r), win=round(100 * (r > 0).mean(), 1), exp=round(r.mean(), 3),
                pf=round(w / lo, 2) if lo else np.inf, tot=round(r.sum(), 1), maxdd=round(dd, 1))


# ---------------------------------------------------------------- strategies
def holy_grail(df, adx_min=30):
    """Raschke: ADX(14)>30 & rising, first pullback touch of EMA20, stop-entry past touch bar."""
    e = ema(df.close, 20)
    a, pdi, ndi = adx(df)
    rising = a > a.shift(3)
    sig = []
    for i in range(60, len(df)):
        if not (a.iloc[i] > adx_min and rising.iloc[i]):
            continue
        if pdi.iloc[i] > ndi.iloc[i] and df.low.iloc[i] <= e.iloc[i] and df.low.iloc[i - 1] > e.iloc[i - 1]:
            sig.append((i, 1, df.high.iloc[i] + TICK, df.low.iloc[i] - TICK, "stop"))
        elif ndi.iloc[i] > pdi.iloc[i] and df.high.iloc[i] >= e.iloc[i] and df.high.iloc[i - 1] < e.iloc[i - 1]:
            sig.append((i, -1, df.low.iloc[i] - TICK, df.high.iloc[i] + TICK, "stop"))
    return sig


def grimes_pullback(df, look=20, recent=10):
    """Grimes: impulse (new 20-bar extreme within last 10 bars, EMA20>EMA50) then pullback to EMA20;
    stop-entry above signal bar, stop under pullback low."""
    e20, e50 = ema(df.close, 20), ema(df.close, 50)
    hh = df.high.rolling(look).max()
    ll = df.low.rolling(look).min()
    new_high = (df.high >= hh).rolling(recent).max().astype(bool)
    new_low = (df.low <= ll).rolling(recent).max().astype(bool)
    sig = []
    for i in range(60, len(df)):
        if e20.iloc[i] > e50.iloc[i] and new_high.iloc[i - 1] and df.low.iloc[i] <= e20.iloc[i] < df.close.iloc[i]:
            sw = df.low.iloc[i - 5:i + 1].min()
            sig.append((i, 1, df.high.iloc[i] + TICK, sw - TICK, "stop"))
        elif e20.iloc[i] < e50.iloc[i] and new_low.iloc[i - 1] and df.high.iloc[i] >= e20.iloc[i] > df.close.iloc[i]:
            sw = df.high.iloc[i - 5:i + 1].max()
            sig.append((i, -1, df.low.iloc[i] - TICK, sw + TICK, "stop"))
    return sig


def brooks_h2(df):
    """Brooks High 2 / Low 2 in a trend (EMA20 slope + close side), buy stop above the signal bar."""
    e = ema(df.close, 20)
    slope = e - e.shift(5)
    H, L, C, O = df.high.values, df.low.values, df.close.values, df.open.values
    sig = []
    # long state
    leg_hi, pb, hcount, pb_low = -np.inf, False, 0, np.inf
    leg_lo, pbs, lcount, pb_high = np.inf, False, 0, -np.inf
    for i in range(30, len(df)):
        # ---- bull side
        if H[i] > leg_hi:
            leg_hi, pb, hcount, pb_low = H[i], False, 0, np.inf
        else:
            if H[i] < H[i - 1]:
                pb = True
            if pb:
                pb_low = min(pb_low, L[i])
                if H[i] > H[i - 1]:
                    hcount += 1
        trend_up = slope.iloc[i] > 0 and C[i] > e.iloc[i] * 0.999
        if pb and hcount == 1 and H[i] <= H[i - 1] and trend_up and C[i] > (H[i] + L[i]) / 2:
            sig.append((i, 1, H[i] + TICK, pb_low - TICK, "stop"))
        # ---- bear side
        if L[i] < leg_lo:
            leg_lo, pbs, lcount, pb_high = L[i], False, 0, -np.inf
        else:
            if L[i] > L[i - 1]:
                pbs = True
            if pbs:
                pb_high = max(pb_high, H[i])
                if L[i] < L[i - 1]:
                    lcount += 1
        trend_dn = slope.iloc[i] < 0 and C[i] < e.iloc[i] * 1.001
        if pbs and lcount == 1 and L[i] >= L[i - 1] and trend_dn and C[i] < (H[i] + L[i]) / 2:
            sig.append((i, -1, L[i] - TICK, pb_high + TICK, "stop"))
        # leg extremes decay: reset after 40 bars so old highs don't freeze state
        if i % 40 == 0:
            leg_hi, leg_lo = H[i], L[i]
    return sig


def failure_test(df, look=20, min_age=3, trend_filter=False):
    """Grimes failure test / Raschke Turtle Soup: pierce prior 20-bar extreme (set >=3 bars ago)
    and close back inside; stop-entry through signal bar, stop beyond the sweep wick."""
    e50 = ema(df.close, 50)
    H, L, C = df.high.values, df.low.values, df.close.values
    sig = []
    for i in range(look + 2, len(df)):
        win_l = L[i - look:i]
        win_h = H[i - look:i]
        pl, ph = win_l.min(), win_h.max()
        age_l = look - 1 - np.argmin(win_l)
        age_h = look - 1 - np.argmax(win_h)
        if L[i] < pl and C[i] > pl and age_l >= min_age and (not trend_filter or C[i] > e50.iloc[i]):
            sig.append((i, 1, H[i] + TICK, L[i] - TICK, "stop"))
        elif H[i] > ph and C[i] < ph and age_h >= min_age and (not trend_filter or C[i] < e50.iloc[i]):
            sig.append((i, -1, L[i] - TICK, H[i] + TICK, "stop"))
    return sig


def anti(df):
    """Raschke Anti: 3/10 osc (SMA3-SMA10) with 16-SMA slow line. Slow line rising, fast line
    pulled back and hooks up -> buy stop above bar, stop at 5-bar swing low."""
    fast = df.close.rolling(3).mean() - df.close.rolling(10).mean()
    slow = fast.rolling(16).mean()
    H, L = df.high.values, df.low.values
    sig = []
    for i in range(40, len(df)):
        s_up = slow.iloc[i] > slow.iloc[i - 1]
        s_dn = slow.iloc[i] < slow.iloc[i - 1]
        hook_up = fast.iloc[i] > fast.iloc[i - 1] and fast.iloc[i - 1] < fast.iloc[i - 2] and fast.iloc[i - 2] < fast.iloc[i - 3]
        hook_dn = fast.iloc[i] < fast.iloc[i - 1] and fast.iloc[i - 1] > fast.iloc[i - 2] and fast.iloc[i - 2] > fast.iloc[i - 3]
        if s_up and hook_up and fast.iloc[i] < slow.iloc[i] + abs(slow.iloc[i]):
            sig.append((i, 1, H[i] + TICK, L[i - 4:i + 1].min() - TICK, "stop"))
        elif s_dn and hook_dn:
            sig.append((i, -1, L[i] - TICK, H[i - 4:i + 1].max() + TICK, "stop"))
    return sig


def ict_sweep_mss_fvg(df, look=20, window=12, killzones=True):
    """ICT: sweep of 20-bar extreme -> within `window` bars a close through the last opposing
    swing (MSS) whose leg left a displacement FVG -> limit at FVG proximal edge, stop past sweep."""
    H, L, C, O = df.high.values, df.low.values, df.close.values, df.open.values
    body = np.abs(C - O)
    avgb = pd.Series(body).rolling(14).mean().values
    hrs = df.index.hour
    kz = (hrs >= 7) & (hrs < 10) | (hrs >= 12) & (hrs < 16)
    sig = []
    n = len(df)
    for i in range(look + 2, n - 1):
        pl, ph = L[i - look:i].min(), H[i - look:i].max()
        if L[i] < pl:  # sell-side sweep -> look for bullish MSS
            ref_hi = H[max(0, i - 5):i + 1].max()
            for j in range(i + 1, min(i + window, n)):
                if L[j] < L[i]:
                    break
                if C[j] > ref_hi:
                    # bullish FVG within the displacement leg (i..j)
                    for k in range(j, i + 1, -1):
                        if L[k] > H[k - 2] and body[k - 1] > 1.2 * avgb[k - 1] and C[k - 1] > O[k - 1]:
                            if not killzones or kz[j]:
                                sig.append((j, 1, L[k], L[i] - TICK, "limit"))
                            break
                    break
        if H[i] > ph:
            ref_lo = L[max(0, i - 5):i + 1].min()
            for j in range(i + 1, min(i + window, n)):
                if H[j] > H[i]:
                    break
                if C[j] < ref_lo:
                    for k in range(j, i + 1, -1):
                        if H[k] < L[k - 2] and body[k - 1] > 1.2 * avgb[k - 1] and C[k - 1] < O[k - 1]:
                            if not killzones or kz[j]:
                                sig.append((j, -1, H[k], H[i] + TICK, "limit"))
                            break
                    break
    sig.sort(key=lambda s: s[0])
    return sig


def turtle(df, entry=20, exit_=10):
    """Turtle S1 on bars: Donchian 20 breakout, 2N stop, exit on 10-bar opposite channel."""
    N = atr(df, 20)
    hh = df.high.rolling(entry).max().shift()
    ll = df.low.rolling(entry).min().shift()
    sig = []
    for i in range(entry + 21, len(df)):
        if df.close.iloc[i] > hh.iloc[i]:
            sig.append((i, 1, df.high.iloc[i] + TICK, df.high.iloc[i] - 2 * N.iloc[i], "stop"))
        elif df.close.iloc[i] < ll.iloc[i]:
            sig.append((i, -1, df.low.iloc[i] - TICK, df.low.iloc[i] + 2 * N.iloc[i], "stop"))
    trail = (df.low.rolling(exit_).min(), df.high.rolling(exit_).max())
    return sig, trail


# ---------------------------------------------------------------- run
def load(path):
    d = pd.read_parquet(path)
    d.columns = [c.lower() for c in d.columns]
    if "timestamp" in d.columns:
        d = d.set_index(pd.to_datetime(d.pop("timestamp"), utc=True))
    return d[["open", "high", "low", "close"]].astype(float).sort_index()


def resample(d, rule):
    return d.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def run(df, label, hold):
    rows = []
    for name, fn in [("Raschke Holy Grail", holy_grail), ("Grimes pullback", grimes_pullback),
                     ("Brooks H2/L2", brooks_h2), ("Failure test / Turtle Soup", failure_test),
                     ("Failure test + EMA50 filter", lambda d: failure_test(d, trend_filter=True)),
                     ("Raschke Anti (3/10)", anti), ("ICT sweep+MSS+FVG (killzones)", ict_sweep_mss_fvg)]:
        s = fn(df)
        for rr in (1.0, 1.5, 2.0, 3.0):
            valid = 10 if name.startswith("ICT") else 3
            st = stats(simulate(df, s, rr=rr, valid=valid, max_hold=hold))
            rows.append(dict(tf=label, strategy=name, rr=rr, signals=len(s), **st))
    s, tr = turtle(df)
    st = stats(simulate(df, s, rr=None, valid=1, max_hold=10_000, trail=tr))
    rows.append(dict(tf=label, strategy="Turtle S1 (20/10, 2N)", rr="trail", signals=len(s), **st))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    out = []
    here = Path(__file__).resolve().parent
    h1 = load(here / "gcf_1h_730d.parquet")
    out.append(run(h1, "1h (730d)", hold=48))
    h1a = h1[h1.index < h1.index[len(h1) // 2]]
    h1b = h1[h1.index >= h1.index[len(h1) // 2]]
    out.append(run(h1a, "1h 1st half", hold=48))
    out.append(run(h1b, "1h 2nd half", hold=48))
    m5 = load(here.parents[1] / "supply_demand_strat" / "backtest_data" / "gcf_5m_60d.parquet")
    out.append(run(resample(m5, "15min"), "15m (60d)", hold=64))
    res = pd.concat(out)
    res.to_csv(here / "prototype_results.csv", index=False)
    print(res.to_string(index=False))
