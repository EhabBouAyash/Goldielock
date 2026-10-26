"""Gold rises while New York is closed: session long with time exits.

Entries buy the ask (recorded bid + spread), exits sell the recorded bid. The
18:00 reopen entry pays the Dukascopy first-print spread (2.8bp) instead of
the normal 1.6bp. Entries at 18:30 or 19:00 skip the reopen spread entirely.
None of these trades is open at the 17:00 ET rollover, so no swap is charged.

Run from the repo root: python3 -m gold_edges.session_drift
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from gold_edges import common as C

VARIANTS = {
    # name: (entry "HH:MM" on day D, exit "HH:MM" on day D+1, entry spread)
    "Asia long 18:00->03:00 (reopen spread)": ("18:00", "03:00", C.FIRST_PRINT_SPREAD),
    "Asia long 18:30->03:00": ("18:30", "03:00", C.SPREAD),
    "Asia long 19:00->03:00": ("19:00", "03:00", C.SPREAD),
    "Asia long 19:00->00:00": ("19:00", "00:00", C.SPREAD),
}


def _clock(hhmm: str) -> pd.Timedelta:
    h, m = hhmm.split(":")
    return pd.Timedelta(hours=int(h), minutes=int(m))


def session_trades(entry: str, exit_: str, entry_spread: float, m: pd.DataFrame | None = None) -> pd.DataFrame:
    m = C.minutes() if m is None else m
    idx = m.index
    bid_o, bid_c = m.bid_open.to_numpy(), m.bid_close.to_numpy()
    days = pd.DatetimeIndex(np.unique(idx.normalize()))
    days = days[days.dayofweek.isin([6, 0, 1, 2, 3])]  # Sun-Thu evenings
    t_in = days + _clock(entry)
    t_out = days + pd.Timedelta(days=1) + _clock(exit_) if _clock(exit_) < _clock(entry) else days + _clock(exit_)
    i = idx.searchsorted(t_in)
    j = idx.searchsorted(t_out) - 1
    ok = (i < len(idx)) & (j > 0)
    i, j, t_in, t_out = i[ok], j[ok], t_in[ok], t_out[ok]
    ok = (idx[i] < t_in + pd.Timedelta(minutes=30)) & (idx[j] > t_out - pd.Timedelta(minutes=30)) & (j > i)
    i, j = i[ok], j[ok]
    px = bid_o[i]
    ask = px * (1 + entry_spread) + C.SLIPPAGE
    ret = (bid_c[j] - C.SLIPPAGE - ask) / ask
    return pd.DataFrame({"entry": idx[i], "exit": idx[j], "ret": ret}).set_index("exit")


def main() -> None:
    rows, streams = [], {}
    for name, (a, b, spr) in VARIANTS.items():
        t = session_trades(a, b, spr)
        daily = C.to_daily(t.ret)
        streams[name] = daily
        rows.append(C.evaluate(name, daily))
    df = C.print_table(rows)
    df.to_csv(C.RESULTS / "session_drift.csv", index=False)
    pd.DataFrame(streams).to_parquet(C.RESULTS / "streams_session.parquet")


if __name__ == "__main__":
    main()
