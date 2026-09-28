# Gold edges I would actually consider

This is a separate study from `brooks_h2_l2`. That folder tested Al Brooks and
the other Gemini-report frameworks. This folder is the set of rules I would
defend: they have a published reason to exist, they are fully mechanical, and
they do not require reading candles.

Same data and costs as the Brooks study: HistData XAUUSD minutes 2009-03-15 to
2026-09-18, 1.6 bp spread, $0.10 slippage each side, 5% a year of notional for
overnight financing on daily holds. Parameters are chosen on 2009-2017.
2018-2026 is the check.

Run from the repo root:

```bash
python3 -m gold_edges
python3 -m unittest gold_edges.test_strategies -v
```

## Why these, not more price action

I do not like High 2 / Low 2, ICT kill-zones, Holy Grail, or anchored VWAP as
*systems* on gold. We already ran them. After costs they lose on 15-minute and
1-hour bars, and on daily bars they only look good because gold trended.

The rules below are ones I am willing to stand behind as *ideas*, even when
the gold-only result is small. Each has a reason it could exist that is not
"this bar looks like a textbook picture."

| Rule | Why it could exist | Source |
|---|---|---|
| Time-series momentum (12-1, 6-1) | Slow underreaction to changing risk premia | Moskowitz, Ooi, Pedersen 2012; AQR |
| Dual SMA 50/200 | Same trend, dumber trigger | Brock, Lakonishok, LeBaron 1992 |
| Always-long clips | Control: gold's structural bid, no timing | — |
| Connors RSI(2) washout | 2-day overreaction, but only in an uptrend | Connors, *Short-Term Trading Strategies That Work* |
| N-day down streak | 1-week reversal | Jegadeesh 1990 |
| ATR dip in a rising 200-day | Mechanical "buy the dip" | — |
| NR7 / quiet Donchian | Realized vol is mean-reverting | Crabel, *Day Trading with Short Term Price Patterns* |
| Asia 18:00–03:00 ET long | Dealer inventory into the London handover | already in `brooks_h2_l2` |
| Weekend Friday 16:00 → Sunday 18:00 | Weekend gap premia / positioning | — |
| London 03:00–08:00, NY 08:20–12:00 | Same inventory idea, other clocks | — |

Gold has no 17:00 ET hour in this data (daily break). "Overnight" here is the
last 16:00 ET print through 08:00 the next morning. "Weekend" is Friday 16:00
to the Sunday 18:00 reopen, with the wider reopen spread.

## 2018–2026, train-chosen rules

Dollars are 0.25% of a $25,000 account per 1R ($62.50). Return % is over the
whole 8.7-year test, not per year.

### Things that held up

**Asia-session long** is still the only session with a confidence interval that
excludes zero: 2,180 nights, 51.4%, +0.042R, t=2.98. This script picked "no
stop" because that had the best 2009-2017 expectancy. That path made +23% at
0.25% risk and a 6.4% drawdown, which breaches FundedNext's 6% max loss. Use
the 1.5× volatility stop from `brooks_h2_l2` instead (+13% / 4.5% drawdown,
no breach). See that folder.

**Weekend long, no stop** is the new session result. Friday 16:00 ET to Sunday
18:00 ET, including the reopen spread: +0.058R in 2009-2017 and +0.065R in
2018-2026, 52% wins, about 43 trades a year. t=1.56 and the 95% interval
still includes zero, so this is weaker than Asia. At 0.25% risk it made +6.2%
with a 2.3% drawdown and no limit breaches. About $70 a year on $25K.

**Connors RSI(2) < 5, above the 200-day, exit when RSI(2) > 65**, 2 ATR stop:
the only high-win-rate daily rule that was positive in both halves. 20 trades
at +0.094R (60% wins) in training, 31 trades at +0.169R (74% wins, t=1.94,
CI95 [+0.002, +0.337]) in the test. That is 3–4 trades a year. Two of the
test losses were the full −1R stop (12 Mar 2020 and 18 Mar 2026). At 0.25%
risk: +1.3% over 8.7 years, 0.3% drawdown. It will not pass a challenge. It
is the one "classic" pullback I would not throw away.

**Overnight 16:00–08:00 long** looks like +0.040R (t=2.43) but it contains the
Asia session. It is not a second edge.

### Things I like that did not earn their keep on gold

**Time-series momentum, long only.** I still like this family more than any
candlestick system. On a futures portfolio it is the most replicated academic
result I know. On *gold alone*, 12-1 was flat in 2009-2017 (+0.013R) and
looked brilliant in 2018-2026 (+1.45R, 36 trades) because gold went from
$1,300 to $5,000. Shorts lost. This is the bull, not a timing skill. Same
story as Turtle S2 in the Gemini study.

**Always-long 21-day clips** (the control): +0.20R per clip in training and
+0.19R in the test, t=1.19 both times, interval includes zero. If a trend
rule cannot beat this, it is not adding anything. Most of the SMA/TSMOM
long-only test results are this control plus a few 2024–2026 winners.

**ATR dip in an uptrend** was +0.04R / +0.10R (train/test) at the 1.5 ATR
stretch. t=1.03. Directionally what I wanted; not large enough to trade.

### Things that failed

- London 03:00–08:00 and New York 08:20–12:00, long or short.
- Every short session. Gold does not like being short overnight.
- The 16:00–18:00 weekday gap, once you only keep rules that made money in
  training.
- NR7 breakout: +0.087R in training, −0.002R in the test.
- Bollinger dip, 5-day streak (almost no trades), Connors with the looser
  RSI(2)<10 threshold (edge shrinks).
- Close-to-open drift as a daily long: −0.015R, t=−8. The 17:00 break and
  the spread eat it. Open-to-close is a small +0.021R (t=1.53) and the
  drawdown at 0.25% risk still breaches the 6% max loss.

## What I would actually do with this

On a FundedNext Stellar 1-Step $25K, at 0.25% risk:

1. **Asia long with the 1.5× stop** from `brooks_h2_l2` — the only result
   that is both significant and inside the loss limits. Expect about 1.5% a
   year, not a 10% challenge in a month.
2. **Weekend long** only if you want a second, weaker, non-overlapping
   session. Do not treat it as proven. Combined with Asia it is a
   Friday-to-Monday-03:00 hold; I have not tested that stack as one rule.
3. **Connors RSI(2)<5** as a rare daily add-on if you want a 70%+ win-rate
   trade a few times a year. The sample is 31 test trades. Size it the same
   0.25% and do not expect it to move the account.

I would not automate TSMOM, SMA 50/200, ICT, Brooks H2/L2, Holy Grail, or
London/NY session fades on this account.

## Files

```
gold_edges/
  core.py              daily bars, costs, position walker, setup walker
  strategies.py        the rules above
  sessions.py          clock-time holds and weekend/gap collects
  experiment.py        train/test run
  test_strategies.py   RSI bounds, TSMOM no look-ahead, next-open fill
  results/
    selected.csv       one test row per rule (session stops chosen on train)
    daily.csv          every daily rule, both periods
    sessions.csv       every session × side × stop, both periods
    sessions_picked.csv
    trades_connors_rsi2_test.csv
    trades_weekend_long_test.csv
```
