# GOLD-OCT26-v1 — how to run the three gold agents

This is the instruction file for the paper agents that implement
`October_2026_Gold_Trading_System.txt` (specification **GOLD-OCT26-v1**).

These programs **paper-trade on your computer**. They do **not** connect to
FundedNext, Match-Trader, TradingView, or MetaTrader. Do not enable an EA on a
FundedNext **Free Trial** (EAs are banned there).

DeepSeek (`deepseek-v4-pro`) only sees **already-valid** candidates and answers
**TAKE** or **PASS**. It cannot invent a setup, change the stop, size, or target.
A late or bad reply is **PASS** on the AI book. The mechanical book still trades
at the fixed delay.

Parameters in the spec have **not** been proven profitable. October is the
forward test.

A separate research track, **GOLD-ORB-v2**, is the `*_v2.py` files in this
folder. It does not replace A, B, or C. It also does not place broker orders,
and it has not been shown to make money. The last section of this file explains
how to run it.

---

## Folders (what each agent is)

| Agent | Folder | What it does |
|---|---|---|
| **A** | `deepseek_trial/a_h4_m15_pullback/` | **4-hour trend**, then a **15-minute** pullback to EMA20 and a reclaim. First to implement. |
| **B** | `deepseek_trial/b_london_sweep/` | **London** overnight range `[00:00, 08:00)` London, a sweep of the high or low, then a reclaim back toward the midpoint. |
| **C** | `deepseek_trial/c_ny_range_retest/` | **New York** `[09:30, 09:45)` range, a break that **holds on a later retest**. No H4 filter (H4 is only tagged in the journal). |

Shared engine, calendar, risk, and DeepSeek client: `deepseek_trial/` (this directory).

Each agent writes ledgers under its `books/` folder. Running all three together
also writes `deepseek_trial/books/`.

**Six isolated books** (each starts at $25,000 — do **not** add them up as one account):

- `A_mech` / `A_ai`
- `B_mech` / `B_ai`
- `C_mech` / `C_ai`

Plus `combined_mech`: only **one gold position** at a time; earliest candidate
wins; ties **A, then B, then C**. A busy combined book rejects the new candidate
without queuing it. Independent books still record it.

---

## Timeframes and clocks (exact)

- Signal candles are **UTC** **M5**, **M15**, and **H4**. H4 bars start at 00:00,
  04:00, 08:00, 12:00, 16:00, 20:00 UTC.
- Session hours use **Europe/London** and **America/New_York** (DST on
  **25 October 2026** is handled by the timezone library — no hard-coded GMT/EST).
- Only **closed** candles count. The current unfinished H4 is never used for bias.
- EMAs: first n-bar SMA seed, then α = 2/(n+1). ATR14 is Wilder. A5/A15 is ATR
  through the bar **before** the trigger.
- At least **300 completed H4 bars** of warm-up before trial decisions.

**A windows:** 08:00–11:00 London **and** 08:00–11:00 New York, weekdays. The end
time does **not** take new entries. Hold at most **six hours**, or **15:45 New
York**, plus news exits. Max **one fill per window**, **two fills per day**.

**B window:** 08:00–10:30 London. Overnight box **[00:00, 08:00)** London. Hold
**two hours** or **12:00 London**, whichever first. Max **one B fill per day**.

**C:** Freeze the range at 09:45 New York from **[09:30, 09:45)** (three M5
candles). Entries **09:45–11:15** New York; the **breakout** must confirm before
**10:45**. Hold **three hours** or **15:45 New York**. Max **one C fill per day**.

---

## How each strategy works (plain language)

**A — trend pullback**  
H4 must be UP (close > EMA20 > EMA50, and EMA20 rising vs three H4 bars ago) or
the mirror for shorts. Arm when M15 **closes back through EMA20** (from above for
longs) but **stays above EMA50**. Then, within the **next four** M15 candles, the
first close **above EMA20 and above the previous candle’s high** confirms. Stop
under the pullback minus 0.20 A15. Target **2.5 times** the stop distance.

**B — London sweep**  
If last night’s range width is not between 0.5× and 1.5× the median of the prior
**20** nights, B is off. H4 may be UP or NEUTRAL for longs. Price must poke
**beyond** the overnight low (or high) by at least 0.05 ATR, then **close back**
through the boundary **and** a short-term structure level K (five bars before the
excursion). Target is the **nearer** of the range midpoint and 2R, but only if
there is at least **1.5R** of room. An excursion that reaches **both** H and L
cannot arm either side.

**C — NY break and retest**  
Range width uses the same 20-day median filter. Disable C if a news blackout
overlaps the 09:30–09:45 reference. A breakout is an M5 close beyond the box with
a **body at least 60%** of the candle range — that only **arms**. Entry waits for
a **later** candle that **retests** the broken level and closes back in the
breakout direction. Same-candle break-and-retest is **invalid**. Target **2.5R**.
If a long attempt fails, a short attempt is still allowed until something fills.

---

## Automation (what software vs DeepSeek owns)

Software owns quotes, bars, indicators, state machines, lot size, stops, targets,
news blackouts, shock pauses, and all fills.

DeepSeek is called **only when a candidate is already valid**, with a compact
snapshot (12 M5 bars, 8 M15 bars, H4 values, levels, costs). Prompt and schema
are frozen in `auditor.py`. Response deadline **8 seconds**; execution delay
**10 seconds** after the confirmation close (OHLC replay uses the **next M5
open** as that fill).

News: events in `calendars/events.json` (October 2026 official dates are
preloaded). Flatten **10 minutes before** an event; no new entries **15 minutes
before through 15 minutes after**. FOMC **decision** day **blocks the rest of
that New York afternoon**. Columbus Day is tagged for liquidity review only — it
is not a flatten event. If a completed M5 true range exceeds **3×** prior ATR14,
unfilled candidates are cancelled and new entries pause **15 minutes**.

Risk: **$125** per trade (0.50% of $25,000), **no compounding**. Daily stop
**$375**. Trial stop **$1,500** drawdown from the book’s high. Stops never widen
after fill. No partials, pyramids, or auto-breakeven. Friction must be ≤ **15%**
of stop distance. Stop distance floor **0.80 ATR**, cap **2.50 ATR**.

---

## Commands

From the Goldielock repo root (where `.env` lives). You need `DEEPSEEK_API_KEY`
in `.env` for `paper --audit`. Replay does not call the API.

```bash
python3 -m unittest deepseek_trial.test_oct26 -v

python3 -m deepseek_trial.a_h4_m15_pullback status
python3 -m deepseek_trial.a_h4_m15_pullback replay
python3 -m deepseek_trial.b_london_sweep replay
python3 -m deepseek_trial.c_ny_range_retest replay

# Mechanical + DeepSeek TAKE/PASS on the same candidates
python3 -m deepseek_trial.a_h4_m15_pullback paper --audit
python3 -m deepseek_trial.b_london_sweep paper --audit
python3 -m deepseek_trial.c_ny_range_retest paper --audit

# All six books + combined mechanical portfolio (no API)
python3 -m deepseek_trial

# Same, with DeepSeek TAKE/PASS on the AI books
python3 -m deepseek_trial --audit
```

`replay` does **not** call the API. `paper --audit` does.

History is **GC=F last-trade OHLC** from Yahoo — a chart proxy, **not** the
broker’s XAUUSD bid/ask midpoint. The spec wants the intended execution feed
before a real October live-paper month.

About **60 days** of 5-minute bars give only a little more than **300 H4
candles**. The engine therefore also loads **GC=F 1-hour** history, resamples it
to H4, and uses it only to **warm up EMA20/50** so trial decisions can run across
the whole 5-minute file. That mix is labelled in the run report (`source` and
`h4_source`). B and C still need **20 prior same-session ranges** from the
5-minute file before they can enable.

Each run prints a **rejection funnel** (`diagnostics.funnel`): armed → confirmed
→ width/history failures. Few trades is then a diagnosable condition, not a
mystery.

---

## Suggested order (from the spec)

1. Recorder / replay / sizing (this package).
2. Strategy **A**, then **B** and **C**.
3. Six-book comparison and DeepSeek journal.
4. Daily diagnostics.
5. Only keep a model upgrade if TAKE/PASS **beats** the matched mechanical book.

A known bug blocks a strategy until fixed. A correctly implemented **losing**
result is evidence against the rule — do not silently rewrite October.

---

## v3 — DeepSeek knowledge agents (not GOLD-OCT26-v1)

These **three extra agents** ask DeepSeek to **propose** a TRADE or SKIP from the
strategy card plus recent bars. Software still sizes, clamps stops, news-pauses,
and caps **5 fills per day**. This is **not** the v1 rule: v1 DeepSeek only
TAKE/PASS mechanical candidates.

| Agent | Folder | Command |
|---|---|---|
| **A v3** | `deepseek_trial/v3_a_knowledge/` | `python3 -m deepseek_trial.v3_a_knowledge` |
| **B v3** | `deepseek_trial/v3_b_knowledge/` | `python3 -m deepseek_trial.v3_b_knowledge` |
| **C v3** | `deepseek_trial/v3_c_knowledge/` | `python3 -m deepseek_trial.v3_c_knowledge` |

### 30-day API cost **if each agent fills 5 times per day** (150 calls)

Peak prices from the spec (28 Sep 2026). ~3,500 input tokens/call.

| Model / output style | **One agent** | **All three** |
|---|---|---|
| **deepseek-v4-pro**, with reasoning (~3,500 out) | **~$2.77** | **~$8.31** |
| **deepseek-v4-pro**, JSON only (~400 out) | **~$0.93** | **~$2.79** |
| **Flash**, with reasoning | **~$0.79** | **~$2.37** |
| **Flash**, JSON only | **~$0.23** | **~$0.69** |

This package looks up to **8 times per day** (including SKIPs), so a live month
can be **240 calls/agent** (~**$4.44** Pro with reasoning, ~**$13.31** for three).

Print the live table:

```bash
python3 -m deepseek_trial.v3_knowledge cost
python3 -m unittest deepseek_trial.test_v3 -v
python3 -m deepseek_trial.v3_a_knowledge replay          # counts looks, no API
python3 -m deepseek_trial.v3_a_knowledge paper --audit   # calls DeepSeek
python3 -m deepseek_trial.v3_knowledge replay --audit    # A+B+C v3 books
```

v3 `replay` without `--audit` does **not** place trades (the model is the signal).
This is API cost only, not trading P&L. v3 is a research arm, not an income claim.

---

## GOLD-ORB-v2 — fixed rules, model only explains

This is a **new research track** in this folder. It does **not** replace the
October A/B/C agents above, and it does **not** replace the Claude day-trading
script in the repo root (`day_trading_insights.py`).

In plain language: the old day-trading helper asks a model to score gold from
the big picture down to the 5-minute chart, using weights of about 50% macro,
20% 4-hour, 12% 1-hour, 10% 15-minute, and 8% 5-minute. Those weights were a
guess. v2 does not use them. Code looks for one specific London setup. If the
data, the calendar, or the account check fails, the answer is **NO-TRADE**. A
written explanation can describe that answer. It cannot change it.

The frozen rules are in `deepseek_trial/research_spec_v2.txt`. They are a
starting specification, **not** a proven edge and **not** a reason to risk more.

### What a cycle is allowed to trust

| Input | May approve a trade? |
|---|---|
| FundedNext MT5 XAUUSD bid/ask and closed bars | Yes, if they are fresh |
| A saved chart snapshot | No. It can be shown, then the decision is no-trade |
| Yahoo `GC=F` or any other proxy | No. Background only |
| Daily 10-year real yield (FRED DFII10) | No. It is yesterday's daily number, labeled with its date |
| Headlines | No. Duplicates are dropped. A headline with no time is unusable |
| The calendar file | Required. If it is missing, no-trade |
| `trade_backlog.txt` | Notes only. It does not set the account balance |
| MT5 balance, equity, open trades, pending orders, and history | Yes, this is the account |

On a Mac the official MetaTrader package often is not available. v2 then
**refuses the live trade**. It does not quietly switch to Yahoo.

### The setup being tested

London time, weekdays only:

- Mark the high and low from **08:00 to 08:30**.
- Between **08:30 and 10:30**, wait for a closed 5-minute bar to close outside
  that box.
- Within the **next three** 5-minute bars, price must touch the broken edge and
  close back on the breakout side.
- The practice entry is the next bar's open (a research stand-in for the first
  live quote). Live mode uses the MT5 ask or bid instead.
- Stop sits just beyond the retest extreme, by 0.10 times the 5-minute ATR.
- Target is twice the stop distance (2R). Be flat by **11:00**.
- At most **one filled trade per day**. No adding to a loser or a winner.
- The default also asks the last **closed** 1-hour bar to agree (close and the
  faster average both on the same side of the slower average). A second variant
  turns that filter off so the two can be compared. VWAP is not included.

Size starts at **0.25% of $25,000, which is $62.50**, then gets smaller if the
$750 daily room or the $23,500 floor is close. The old 1% ($250) figure is not
the design size. If the room cannot fund the smallest lot, the answer is
no-trade.

### Files

| File | What it does |
|---|---|
| `research_spec_v2.txt` | The frozen rules, in plain language |
| `research_types_v2.py` | Shared numbers and records |
| `strategy_engine_v2.py` | The London open-range setup |
| `risk_engine_v2.py` | Size from remaining loss room |
| `compliance_engine_v2.py` | Stale data, news, one-idea limit, mandatory stop |
| `outcome_evaluator_v2.py` | Signals stored separately from completed trades |
| `backtest_engine_v2.py` | Replay, stress checks, and side-by-side variants |
| `fetch_market_data_v2.py` | MT5 prices. Proxy feeds cannot approve a trade |
| `fetch_intraday_v2.py` | Closed bars and features. A saved snapshot cannot trade |
| `prop_account_tracker_v2.py` | MT5 is the account. The backlog is a note |
| `additional_news_v2.py` | Timestamped calendar and de-duplicated headlines |
| `day_trading_insights_v2.py` | One cycle. Failed checks cannot be overridden |
| `test_v2.py` | Checks the plumbing. Not a profitability test |

### Commands

From the Goldielock repo root:

```bash
python3 -m unittest deepseek_trial.test_v2 -v
python3 -m deepseek_trial.day_trading_insights_v2 --spec
python3 -m deepseek_trial.day_trading_insights_v2 --self-check
```

`--self-check` replays a **made-up** Monday so you can see the plumbing, then
shows a live cycle that correctly says no-trade because there is no MT5 feed.
The sample profit is not a result from the market.

A real replay belongs in `backtest_engine_v2.py`, with a loaded calendar and
bars from the account you would actually trade. Judge it on average result per
trade after costs, the worst day, whether the $750 / $1,500 lines were hit,
and profit that would still count after news rules. One good week is not
enough. Do not tune the rules to the best backtest and then treat that as proof.

