# Brooks High 2 / Low 2 on XAUUSD

A fully rule-based version of Al Brooks' High 2 / Low 2 pullback entry, backtested
on 5.7 years of XAUUSD 1-minute data with pandas/NumPy only (no AI in the loop).

## Verdict

**Not tradeable as specified.** Across 72 variants (5m/15m/1h, with or without a
higher-timeframe trend filter, two stop placements, 1R/1.5R/2R targets, two
session windows):

- No variant made money in the training period (Jan 2021 - Oct 2023). The best
  one broke even at exactly 0.000R.
- Only 7% of variants were positive in the untouched test period (Nov 2023 -
  Sep 2026), and training rank did not predict test rank (Spearman 0.04).
- A 45-50% win rate is easy to reach with a 1R target (46-49%), but those
  variants lose 0.04-0.10R per trade after costs.
- Before costs the setup is slightly better than random (+0.04 to +0.09R vs
  about -0.01R for random entries with the same stops), but that gross edge is
  smaller than the ~0.13R per trade that spread and slippage cost on 5m/15m,
  and not statistically significant.

At 0.5% risk per trade, every headline variant breached a 10% max-loss limit in
the test period.

| Variant | Train trades / win / expectancy | Test trades / win / expectancy |
|---|---|---|
| 5m + 1h trend filter, pullback stop, 1.5R (the plan) | 394 / 37.6% / -0.142R | 729 / 39.6% / -0.053R |
| 15m, London/NY, pullback stop, 1R (best with >=45% win) | 525 / 49.1% / -0.063R | 680 / 46.8% / -0.074R |
| 5m + 1h filter, signal-bar stop, 2R (best in training) | 143 / 37.1% / +0.000R | 351 / 29.9% / -0.145R |
| 1h, no filter, pullback stop, 1.5R (baseline) | 286 / 38.1% / -0.132R | 334 / 41.9% / +0.017R |
| 1h, no filter, pullback stop, 2R | 283 / 35.0% / -0.082R | 330 / 37.9% / +0.068R |

The 1h variants turn slightly positive only in 2025, gold's strongest trend
year, and mostly on the long side, so treat them as regime luck rather than
edge. Full numbers are in `results/report.txt`, `results/grid.csv`, and
`results/diagnostics.txt`.

## Would news filtering or practitioner rules fix it?

No. `research/filters_and_news.py` tests each idea as a separate hypothesis
(output in `results/filters_and_news.txt`).

- **Macro/news filtering hurts rather than helps.** Trades that were open
  across a US release slot (08:30/10:00 ET weekdays, 14:00 ET Wednesdays) did
  better than trades clear of releases, in all three variants tested. Blocking
  entries near releases left expectancy unchanged. A hindsight "oracle" that
  drops every trade hit by a 5x volatility shock roughly doubled the losses
  (5m: -0.14R to -0.29R in training). The little edge the pattern has comes
  from volatile periods; quiet-market trades are the losers. The backtest does
  not model spreads widening at releases, so live news trades would do
  somewhat worse, but trades clear of releases are negative in every variant
  anyway.
- **Practitioner context rules don't hold up out of sample.** Tested rules:
  H2 at the EMA, ADX >= 25, and a strong signal bar (close in the top 25%,
  body at least half the range).
  - Singly, none is positive in both periods.
  - Stacked with a news filter and Brooks' scale-out, training looked
    excellent (55-70% wins, +0.13 to +0.38R), but on only 20-42 trades. The
    test period fell back to 35-45% wins and negative expectancy. That is
    overfitting.
- **Brooks-style management** (half off at 1R, stop to breakeven, runner to
  2R) raises the win rate to 46-48% but doesn't improve expectancy.

### What other traders report

- **No independent, quantified H2/L2 backtests exist.** Published figures are
  self-reported and discretionary.
- **Reddit r/Daytrading** ([thread](https://www.reddit.com/r/Daytrading/comments/1osewll/price_action_al_brooks_high_win_rate/)):
  - One trader reports about 60% wins at about 2:1, taking only H2/L2s that
    bounce off the 20 EMA or VWAP, on ES, regular hours only.
  - Another reports 68% on MES, after four years of building personal rules.
    They skip setups whose stop is too wide, and many of their trades end at
    breakeven.
- **Brooks Trading Course forum** ([1](https://www.brookstradingcourse.com/support-forum/general-trading-discussion/three-11-setups-with-different-probabilities/),
  [2](https://www.brookstradingcourse.com/support-forum/40-entering-late-in-trends/probability/)):
  - A good H2 at the EMA in a strong trend is about a 60% bet for a 1R scalp
    and about 40% for 2R.
  - Forum members stress that the same H2 can be with-trend or effectively
    counter-trend after a climax. Their view is that mechanical backtests
    [mix the two and "tell nothing"](https://www.brookstradingcourse.com/support-forum/general-trading-discussion/backtesting-software/).

The mechanical version reproduces the scalp numbers: 46-49% at 1R, 35-38% at
2R. Brooks' 60% refers to hand-picked setups, and that selection skill is
exactly what doesn't translate into rules. The rules tried here (EMA touch,
ADX, signal-bar strength) did not recover it.

## Rules tested

Signals come from completed mid-price bars; nothing uses a bar before it closes.

1. **Trend context.** Long only when EMA(20) is higher than 5 bars ago and the
   close is above it. Optional higher-timeframe filter: the last *completed*
   1h bar (4h for the 1h chart) must be above a rising EMA(20).
2. **Pullback and count.** After a leg high, the first lower high starts a
   pullback. Each bar that trades above the prior bar's high, after at least
   one lower high, is one attempt (H1, H2...). Consecutive up bars count once.
   A new leg high, or a pullback longer than 20 bars, resets the count.
3. **Signal bar.** After H1, a bar with a lower high that closes in the upper
   half of its range is the H2 signal bar.
4. **Entry.** Buy stop 10 cents above the signal bar, valid for one bar.
   Cancelled if the stop level trades first.
5. **Stop.** Below the pullback low (`pullback`) or the signal-bar low
   (`signal_bar`). Risk must be 0.3-3 x ATR(14) and at least 0.13% of price,
   so costs stay under ~20% of 1R.
6. **Exit.** Target at 1R/1.5R/2R, stop, or time exit after 4h (5m), 8h (15m),
   or 24h (1h). One position at a time.
7. **Session.** No new signals 21:00-22:59 UTC (daily break/rollover);
   optionally London/NY only (07:00-20:00 UTC).

Low 2 is the exact mirror: the High 2 routine is run on prices reflected
through zero, and a unit test checks the two sides are identical.

## Data and costs

- **HistData.com XAUUSD 1-minute bid**, Jan 2021 - Sep 2026 (1.98M minutes).
  Despite the site saying EST, the timestamps are New York time *with*
  daylight saving; this was verified against Dukascopy (a one-hour shift gives
  an exact match) and corrected. April 2023 is about a third short, and there
  are holiday gaps.
- **Spread** modelled at 1.6 bp of price ($0.31 at $1,940, ~$0.70 at $4,300),
  calibrated on Dukascopy's real bid/ask ($0.30 median in Sep 2023, ~$0.50 in
  Sep 2025). Longs fill on the ask and exit on the bid; shorts the reverse.
- **Slippage** $0.10 on every stop-order fill (entry and protective stop).
  The report also re-runs each headline variant with an extra $0.20/$0.40
  commission.
- **Fill rules** use 1-minute bars inside each signal bar. A minute touching
  both stop and target counts as a stop, and the target can't fill in the
  entry minute.
- **Dukascopy** real bid/ask data is also supported (`--source dukascopy`),
  but the feed allows only a few requests a minute after an initial burst, so
  years of history take many hours. Only Sep 2023 was downloaded, for
  calibration.

## Method

`experiment.py` splits the data at its midpoint (2023-11-01). It picks
variants using training results only (highest training expectancy with at
least 100 trades, plus the best variant with at least 45% training win rate),
then reports the test period without further changes. The planned 5m + 1h
variant and a 1h baseline are always reported too.

`research/diagnostics.py` checks whether the signal predicts anything before
costs, by comparing it with random entries that use the same stop sizes and
targets. The reversed-signal check filled too few trades (84-113) to be
informative.

## Volatile history (2009-2020)

`python3 -m brooks_h2_l2.data --source histdata --start 2009-01-01` adds the
years before the main study, including the 2011 spike, the 2013 crash and the
2020 selloff (~4.2M extra minutes, in `data/histdata/`). The 2021+ study stays
unchanged because its loaders pass `start="2021-01-01"`.

`research/volatile.py` marks a day as volatile from the trailing 20-day average
range, using only the prior close, and reruns the three headline variants.
Output: `results/volatile_days.csv`, `results/volatile_grid.csv`.

Median trailing range by year, as a percent of price: about 1.3% in 2009-2011
and 2020, 1.64% in 2013, 0.82-0.86% in 2017-2019, and 2.17% in 2026 (the
highest year in the sample, and already inside the losing 2021+ test).

Restricting entries to those fast regimes does not create an edge:

- 5-minute stays negative at every cutoff, including a trailing range above 1.6%.
- 15-minute, London/NY, 1R, trailing range >= 1.6% (the best 2009-2014 cutoff):
  51.8% wins at 0.00R in 2009-2014, 52.7% wins at -0.01R in 2015-2020
  (129 trades), 50.0% wins at -0.01R in 2021+. That is the 45-50% win rate,
  and it is break-even after costs.
- 1-hour looks profitable in 2009-2014 (up to +0.24R when the trailing range
  is at least 1.4%) and loses in 2015-2020 (about -0.14R). The early result
  does not carry forward.

## Asia-session long

`research/asia_session.py` buys the first 18:00 ET print (Sunday–Thursday) and
sells at 03:00 ET, unless a volatility stop is hit. The stop is a multiple of
the prior 20 Asia-session ranges. The multiple is chosen on 2009–2017; 2018–2026
is the check. The long-Asia direction itself was first seen on the full sample,
so that part of the result is not a clean discovery.

Entry pays the modeled 1.6bp spread, a 1.21x reopen premium (Dukascopy
Sep 2023, 18:00–18:30 ET versus New York cash), and $0.10 of slippage. A stress
case uses 1.56x, the reopen 90th percentile. FundedNext Stellar 1-Step limits
are 2.5%/3% daily and 5%/6% max, on initial balance.

| Rule | 2009–2017 | 2018–2026 |
|---|---|---|
| Time exit, no stop | 2,238 trades, 51.9%, +0.012R (t=0.83) | 2,180 trades, 51.4%, +0.042R (t=2.98) |
| Stop 1.50x (best stop on train) | 51.6%, +0.005R (t=0.52) | 51.1%, +0.024R (t=2.54), CI95 [+0.005, +0.042] |
| Same stop, 1.56x stress spread | 51.1%, −0.002R | 50.6%, +0.018R (t=1.90) |

Tighter stops lost money in training. On the test, 0.25% risk with the 1.50x
stop made +13.1% with a 4.5% max drawdown: no daily breach, and under both the
5% and 6% max-loss limits. At 0.5% risk the same path made +26.2% and a 9.1%
drawdown, which breaches both max-loss limits. One trade per day never reached
the 3% daily cap. Output: `results/asia_session.csv`.

## All six Gemini frameworks, 2009-2026

`research/gemini_frameworks.py` replaces the 730-day GC=F screen. It covers
Grimes (pullback, failure test), Brooks (prototype H2/L2), Raschke (Holy Grail,
Anti, 80-20), ICT (sweep + MSS + FVG in the 02-05 and 07-10 ET kill zones),
Shannon (anchored average from the 100-bar swing, Stage 2/4), and Turtle S1/S2.
It runs them on 15m, 1h, 4h and daily mid bars with a 1.6bp spread, $0.10
slippage each side, and a 13bp minimum stop. The target multiple is chosen on
2009-2017, and 2018-2026 is the check. Spot gold has no volume, so the anchored
VWAP is time-weighted.

- **15m and 1h:** every framework loses after costs in 2018-2026. Gross edges
  are 0 to +0.05R, and costs take 0.1-0.2R.
- **4h:** everything is between -0.23R and +0.05R. Nothing is significant.
- **Daily:** most frameworks are positive in 2018-2026, but none reaches t=2 and
  trade counts are 30-90. Turtle S2 (55/20) is the only one positive in both
  halves, and only on the long side.

The daily Donchian check (`results/gemini_daily_trend.csv`) charges 5% a year of
notional for swap and marks equity daily for FundedNext's daily-loss rule.
After swap, every length from 20/10 to 100/30 is flat in 2009-2017 (-0.14R to
+0.08R). In 2018-2026, 20/10 is flat and 40/15 through 100/30 are +0.24R to +1.26R
(both sides). For
55/20 in 2018-2026, three trades supply +28.9R of the +29.3R total; without
them it is flat. At 0.5% risk that is about 1.6% a year. At 1% risk the drawdown
reaches 10-17%, and single days of giving back open profit (for example
29 Jan 2026) exceed the 2.5% daily limit. It is a bet on gold trending, not an
intraday edge.

Output: `results/gemini_frameworks.csv` (all rows), `results/gemini_frameworks_selected.csv`
(the train-chosen target and its test result), `results/gemini_daily_trend.csv`.

## What would have to change

The signal's gross edge (~0.05R) is smaller than trading costs on intraday
bars (~0.13R). To be viable, one of these would need to hold:

- **Lower costs relative to risk.** Wider stops on 1h/4h charts, or a raw-spread
  account. The 1h variants are closest to break-even for this reason.
- **A stronger signal.** For example, requiring the pullback to reach the EMA,
  a strong trend by ADX or channel slope, or Brooks' "second entry" context
  (a failed first reversal). Test each as a single hypothesis on the training
  period only. The test period is no longer untouched for these 72 variants.
- **Different exits.** Brooks often scales out or trails. Fixed-R exits may
  leave out part of the edge.

## Layout

```
brooks_h2_l2/
  data.py            HistData / Dukascopy downloaders, loader, mid-price bars
  strategy.py        Config, indicators, H2/L2 state machine, HTF filter, signals
  backtest.py        1-minute bid/ask order simulator, stats, prop-account check
  experiment.py      72-variant train/test grid and report
  __main__.py        CLI
  test_strategy.py   unit tests (H2 counting, mirror symmetry, no look-ahead, fills)
  results/           report.txt, summary.json, grid.csv, trades_*.csv, diagnostics.txt
  research/
    gemini_strategy_report.md   the Gemini Deep Research report that started this
    prototype_lab.py            first screen of all six frameworks on GC=F futures (superseded)
    gemini_frameworks.py        all six frameworks, 2009-2026, 15m/1h/4h/daily, plus daily trend check
    asia_session.py             Asia-session long with reopen spread, vol stop, FundedNext check
    prototype_results.csv       its output
    gcf_1h_730d.parquet         GC=F 1h bars used by the prototype
    diagnostics.py              zero-cost / random-entry checks
    filters_and_news.py         news windows, volatility oracle, practitioner rules, scale-out
    volatile.py                 2009-2020 high-volatility regime test
  data/              downloaded minute data (git-ignored)
```

## Run

From the repository root:

```bash
python3 -m brooks_h2_l2 download                 # HistData 2021 -> today (~3 min)
python3 -m brooks_h2_l2 experiment               # grid + report -> results/ (~30 s)
python3 -m brooks_h2_l2 backtest --set timeframe=1h --set htf=none --set rr=2.0 --set max_hold_bars=24
python3 -m brooks_h2_l2.research.diagnostics
python3 -m brooks_h2_l2.research.filters_and_news
python3 -m brooks_h2_l2.research.volatile
python3 -m brooks_h2_l2.research.asia_session
python3 -m brooks_h2_l2.research.gemini_frameworks   # ~40 s
python3 -m unittest brooks_h2_l2.test_strategy -v
```

`--set FIELD=VALUE` overrides any `Config` field in `strategy.py`.
