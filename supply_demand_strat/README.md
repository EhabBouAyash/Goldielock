# XAUUSD Supply/Demand Pipeline

This is a separate, deterministic gold strategy. It detects supply/demand
zones from completed GC=F chart candles, converts chart levels to XAUUSD spot,
and applies confirmation and prop-account gates. Pandas/NumPy own all technical
analysis. Optional Gemini 3.8 Flash use is restricted to a sanitized
macro-only veto.

## Strategy

### Zone formation

- Demand: Rally-Base-Rally (RBR) or Drop-Base-Rally (DBR).
- Supply: Drop-Base-Drop (DBD) or Rally-Base-Drop (RBD).
- Every zone is recomputed after each completed bar and anchored to the latest
  confirmed fractal pivot before its departure leg. A pivot requires two bars
  on both sides by default, so an unconfirmed swing cannot create a zone.
- Impulse: a one-to-three-bar net move of at least `1.5 × ATR(14)`.
- Mitigation is measured as the deepest percentage penetration into the zone.
  Zones at 70% mitigation or more are automatically ineligible.
- Invalidation requires a subsequently confirmed pivot beyond the distal edge,
  producing a deterministic BOS/CHoCH-based rule instead of line crossing.
- The anchor pivot must sit within 6 bars of the departure leg, the base only
  includes the leg-in candle when its body is small (`≤ 0.6 × ATR`), and zones
  wider than `2 × ATR` are discarded. Zone ids are keyed to the anchor
  timestamp, so the same zone keeps its id as new bars arrive.
- Quality considers departure strength, pivot-base tightness, structure break,
  and remaining freshness.

These rules infer zones from price behavior. They do not prove that unfilled
institutional orders exist.

### Regime playbook

- `UPTREND`: only buy a pullback into qualified demand.
- `DOWNTREND`: only sell a rally into qualified supply.
- `CONSOLIDATING`: buy demand or sell supply at an outer range edge; no
  mid-range trade.

The 1H EMA20/EMA50 relationship, slope, ATR-normalized spread, and ADX determine
the broad regime. NumPy then separates `IMPULSE_UP/DOWN`, `GRIND_UP/DOWN`,
`TREND_UP/DOWN`, and `CONSOLIDATION`. Grind requires directional slope and
efficiency with small bodies and overlapping ranges. During a grind, only
same-direction continuation formations are eligible and zone proximity is
widened; counter-trend extremity reversals are blocked. The 4H chart supplies
context.

### Entry confirmation

Every trade requires:

1. Price at a qualified zone whose direction does not oppose the 4H bias.
2. A completed engulfing or rejection candle intersecting the zone, with a
   range of at least `0.5 × ATR` (noise-sized candles do not count).
3. A same-direction 9/21 EMA crossover on 5M or 15M.
4. A buy-stop above bullish confirmation or sell-stop below bearish
   confirmation. If price already traded through the entry or the stop after
   the confirmation candle, the setup is skipped rather than chased.
5. Stop beyond both the zone and rejection/sweep extreme.
6. At least 1.5R after a `$0.40/oz` round-trip cost allowance, measured to the
   next opposing zone or 1H swing.
7. The order is cancelled if not triggered within 6 execution bars.
8. No new orders during 21:00–22:59 UTC (CME break / rollover spreads).
9. A passing FundedNext account-risk gate.

If any requirement is missing, the result is `WAIT`, `NO_TRADE`,
`DATA_ERROR`, `DATA_STALE`, `RISK_BLOCKED`, or `MACRO_BLOCKED`.

### Gemini 3.8 Flash boundary

With `--macro` (aliases: `--gemini`, `--opus`), the model receives categorical
technical context only: status, direction, movement-state tag,
higher-timeframe bias, zone type/formation, freshness bucket, structure-event
label, and confirmation state. It does not receive candles, pivots, indicator
values, zone prices, entries, stops, targets, position size, or account values.

Gemini receives a bounded, source-tiered macro packet:

- DXY plus FRED 10Y real yield, 2Y/10Y nominal yields, 10Y breakeven
  inflation, VIX, and WTI;
- Treasury General Account changes, latest coupon-auction demand, and New York
  Fed repo/reverse-repo usage;
- official weekly CFTC COMEX gold managed-money positioning;
- scheduled high/medium-impact USD events;
- first-party Fed monetary releases and speeches, BLS CPI/PPI/jobs/JOLTS,
  BEA PCE/GDP, ECB releases, and EIA energy reporting;
- CNBC economic reporting, a direct-gold cross-check restricted to established
  publishers (such as Reuters, Bloomberg, CNBC, Kitco, and the World Gold
  Council), plus BBC World and Al Jazeera geopolitical reporting.

No outlet can dominate the digest, duplicate headlines are removed, and
official releases outrank commentary. The local collector and its caches live
entirely in `supply_demand_strat/`; it does not use the root
`additional_news.py`. Gemini can return `ALLOW`, `CAUTION`, or `BLOCK`. It can
veto an existing actionable setup but cannot create one, reverse it, or modify
any technical number. A headline-only assessment is automatically downgraded
to `CAUTION`; `BLOCK` requires imminent event risk, fresh official evidence, or
measured cross-asset agreement. Macro-gate API failures remain fail-closed.

Setup:

1. Create a key at [Google AI Studio](https://aistudio.google.com/apikey).
2. Put `GEMINI_API_KEY=...` in the repo `.env`.
3. The pipeline calls `gemini-3.8-flash` via
   `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent`.

## Run

From the repository root:

```bash
python3 -m supply_demand_strat
```

Add the Gemini 3.8 Flash macro gate:

```bash
python3 -m supply_demand_strat --macro
```

Use saved intraday data:

```bash
python3 -m supply_demand_strat --input intraday_data.json
```

Output files:

- `latest_analysis.json`: complete machine-readable result.
- `latest_signal.txt`: compact trader-facing result.
- `analysis_archive.jsonl`: append-only run history.

If live chart retrieval fails, the pipeline can inspect
`day_trading_intraday_snapshot.json`, but candles older than 45 minutes are
forced to `DATA_STALE / NO_TRADE`. A GC=F fallback cannot substitute for an
independent XAUUSD spot quote when authorizing an order.

## Backtest

Requires `pandas`, `numpy`, and `pyarrow` (`python3 -m pip install pyarrow`).
Input is one or more parquet files, directories, or globs of 1-minute or
5-minute OHLC bars. Column names are matched case-insensitively (`open`/`o`,
`close`/`c`/`last`, `timestamp`/`time`/`datetime`, or a DatetimeIndex); epoch
seconds/ms/us/ns are detected automatically. Naive timestamps are treated as
UTC unless `--assume-tz` is given.

```bash
python3 -m supply_demand_strat.backtest --data path/to/xauusd_1m/ \
  --start 2023-01-01 --spread 0.30 --slippage 0.10
```

Tune rules without editing code via repeatable `--set FIELD=VALUE` overrides
of `StrategyConfig` (for example `--set crossover_lookback=6`). Results go to
`supply_demand_strat/backtest_results/`: `orders.csv`, `trades.csv`,
`summary.json`, and `report.txt`.

How it tests:

- Walk-forward: at every 5M close, only bars that have fully closed are
  visible, and the same `evaluate_setup` function used live makes the decision.
- Orders are simulated on the base bars as stop entries with bid/ask spread,
  stop slippage, gap fills, expiry, and cancellation if the stop trades first.
  When one bar touches both stop and target, the stop is assumed; the target
  is ignored on the fill bar.
- One position at a time; re-signals of the same zone and candle are dropped.
- The report includes expectancy with a bootstrap 95% CI and t-stat, a
  first/last time split, breakdowns by direction/regime/zone/pattern/year,
  FundedNext-style daily and max-loss breaches, and a rejection funnel showing
  which gate stopped each decision.

The Gemini macro veto and live account state cannot be replayed, so the
backtest covers the technical rules only. Treat fewer than ~100 trades as
inconclusive, and do not tune parameters on the same period you evaluate.

## Tests

```bash
python3 -m unittest supply_demand_strat.test_engine supply_demand_strat.test_backtest -v
```

The defaults are hypotheses, not validated edge. Backtest and forward-test the
strategy across trend, range, high-volatility news, and spread regimes before
using it for live decisions.
