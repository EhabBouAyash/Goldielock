# How to run the three paper agents

Plain-language guide for T1, T2, and T3. These programs **simulate** a $25,000 account on your computer. They do **not** log into FundedNext, Match-Trader, TradingView, or MetaTrader. No real or challenge account is connected.

DeepSeek (`deepseek-v4-pro`) is a checker, not a trader. The Python rules decide. The model only copies an already-computed entry ticket. If it disagrees, times out, or returns bad JSON, that entry is skipped. Stops and weekend exits never wait for the model.

---

## What you need first

1. Work from the Goldielock repo root (the folder that contains `.env`).
2. Put this line in `.env` (you already have the key there):

```
DEEPSEEK_API_KEY=your_key_here
```

3. Check that the key loads:

```bash
python3 -m deepseek_trial.t1_gold_trend status
```

You want `"key_present": true` and `"model": "deepseek-v4-pro"`.

4. Optional: run the self-checks:

```bash
python3 -m unittest deepseek_trial.test_trial -v
```

---

## The three agents, in plain English

### T1 — Gold “trend or sit in cash”

- **Folder:** `deepseek_trial/t1_gold_trend/`
- **Market:** gold (XAUUSD)
- **Chart:** **one candle per gold broker day**, aligned to **5:00 PM New York time** (the usual gold daily break). In Beirut that is **midnight** when New York is on standard time, or **11:00 PM** when New York is on daylight time.
- **How it works:** After each daily candle closes, it asks two yes/no questions:
  1. Did gold rise over the past year, ignoring the most recent month?
  2. Is today’s close **above** the 200-day average?
- If **both** are yes and you are flat, it **buys**. If either turns no, it **exits**. It **never shorts**.
- **Stop:** 2 × daily ATR below the fill, and it may only **tighten** (never loosen).
- **Weekend:** it is allowed to hold gold over the weekend in this trial.
- **Right now:** gold is below the 200-day average, so T1 should stay in **cash**. Zero trades is a correct result, not a broken bot.
- **Also:** risk is $62.50 per trade (0.25% of $25,000). The smallest gold size is 0.01 lot = 1 ounce. If the stop is wider than about $62.50, the trade is skipped (`LOT_TOO_SMALL`).

### T2 — Gold “quiet then break” (experimental)

- **Folder:** `deepseek_trial/t2_gold_compression/`
- **Market:** gold (XAUUSD)
- **Chart:** **1-hour candles**, clocked in **UTC** (in Beirut, that is **3 hours ahead of UTC**, so the 12:00 UTC bar is 3:00 PM Beirut).
- **How it works:** It only looks for a trade when the last 10-hour ATR is in the **quietest 20%** of the last 100 hours. Then:
  - close **above** the prior 20-hour high → **buy**
  - close **below** the prior 20-hour low → **sell**
- If the market is not quiet, a breakout is ignored.
- **Stop:** 2 × 20-hour ATR. **No take-profit.** Flatten after **24 hours**, or on stop, or before the weekend.
- Treat every T2 result as **experimental**. This is the weakest of the three.

### T3 — Bitcoin “uptrend or cash”

- **Folder:** `deepseek_trial/t3_btc_ma/`
- **Market:** Bitcoin (BTCUSD CFD in the spec; history is loaded as BTC-USD)
- **Chart:** **one candle per UTC day** (closes **3:00 AM Beirut**).
- **How it works:** Fast average (5 days) vs slow average (100 days). It is **long** only when the 5-day is **more than 1% above** the 100-day. Otherwise **cash**. It **never shorts**.
- It only **enters** on a flip from cash → long (not because it has been long for months). At most **two new entries per week**. After a stop-out, it must go back to cash before it may buy again.
- **Stop:** 2 × 14-day ATR. **Weekend:** Bitcoin CFDs are treated as weekdays-only, so T3 **must flatten before the weekend**.

---

## How “paper trading” works here

There is **no broker platform to log into** for these agents.

| What people often mean | What this project actually does |
|---|---|
| TradingView paper | Not connected |
| FundedNext / Match-Trader demo | Not connected. Do **not** turn an EA on the FundedNext **Free Trial** (EAs are banned there). |
| MetaTrader 5 demo | Allowed in principle on some FundedNext products with an extra EA fee. **These scripts do not talk to MT5.** |
| This repo | A **local paper ledger** on your Mac: fake $25,000, spread, slippage, and stops |

**Commands:**

```bash
# History only. No DeepSeek calls. Writes books/reference.json
python3 -m deepseek_trial.t1_gold_trend replay
python3 -m deepseek_trial.t2_gold_compression replay
python3 -m deepseek_trial.t3_btc_ma replay

# Same walk, but DeepSeek must agree before an ENTRY is kept.
# Writes books/supervised.json
python3 -m deepseek_trial.t1_gold_trend paper --audit
python3 -m deepseek_trial.t2_gold_compression paper --audit
python3 -m deepseek_trial.t3_btc_ma paper --audit
```

Each run walks the **available history** and overwrites that agent’s ledger. It is **not** a 24/7 bot sitting on live ticks. To use it “from today onward,” run the same command after the relevant candle has closed (daily for T1/T3, hourly for T2) and read whether a new `OPEN` appeared.

**Where to look:**

- T1: `deepseek_trial/t1_gold_trend/books/`
- T2: `deepseek_trial/t2_gold_compression/books/`
- T3: `deepseek_trial/t3_btc_ma/books/`

`reference_summary.json` / `supervised_summary.json` are the short scorecards.

**If you want a trade on a real paper platform:** copy the latest `OPEN` (side, stop, size) by hand into TradingView paper or an MT5 **demo**. That is optional and not automated.

**News window:** entries are blocked 10 minutes around events listed in `deepseek_trial/calendars/events.json`. That file is currently **empty**, so the news filter is off until you add dated events.

---

## Account numbers these agents assume

These are the **simulation** settings, not a live FundedNext login:

- Starting balance: **$25,000** per agent (separate books; do not add the three risks together on one real account)
- Risk per accepted trade: **$62.50** (0.25%)
- Internal daily stop: flatten if the day is down **$250**
- Internal trial stop: flatten if equity hits **$24,000** or drawdown from the high is **$1,000**
- Gold spread model: about **1.6 bp** + $0.10 slippage
- Bitcoin spread model: about **5 bp** + $1 slippage

The spec’s FundedNext notes (Stellar 2-Step 5% / 10% loss, crypto weekdays, news profit haircut) are **rules we size against in software**. They are not proof that a challenge account is attached.

---

## Suggested order of use

1. `status` on T1 so you know the API key loaded.
2. `replay` all three. T1 may print **0 trades**. That is expected in this gold tape.
3. Only then `paper --audit` if you want DeepSeek in the loop (this costs API tokens).
4. Keep T2 labelled experimental in any note you write to yourself.

Do not loosen T1’s 200-day filter just to generate gold trades. Sitting in cash is the strategy.
