# How to run things in this repo

## Day-trading gold advisory (`day_trading_insights.py`)

GPT-6.1 Sol reads the chart and the news and may suggest an idea. Code then decides whether that idea is allowed to be shown. It does not send orders. The model key in `.env` is `OPENAI_API_KEY`. GOLD-ORB-v2 under `deepseek_trial/` is a separate research track and is not this pipeline.

```bash
# one pass. The signal prints in the terminal. MetaTrader is not used.
python3 day_trading_insights.py

# one pass, plus a directional comparison log (not a closed-trade win rate)
python3 day_trading_insights.py --option trading

# keep running. Default gap is 5 minutes.
python3 day_trading_insights.py --option agent

# same pipeline, then ask questions. Chat cannot publish its own card.
python3 day_trading_insights_chat.py
```

Checks only, no model call and no broker call:

```bash
python3 -m unittest tests.test_pipeline_fixes -v
```

The run prints a screen signal: LONG, SHORT, WAIT, or NO TRADE, with the entry, stop, and target. MetaTrader is not contacted. That signal is not an order. A card cleared for a funded account still needs three things at once:

- `GOLD_PIPELINE_MODE=APPROVED_MANUAL_ADVISORY`
- `GOLD_ACCOUNT_PROFILE=stellar_1step_25k` (the profile is explicit; the program does not guess it from the old dollar limits)
- a fresh MetaTrader 5 quote and account read. If MetaTrader is missing, the account stays unknown. It is not treated as flat, and the website gold price is not used as the order price.

The card that matters is `gold_authoritative_plan.json`, also printed in `day_trading_insights.txt`. An expired card, a research note, or the model's raw wording is not an order. Email uses that same checked card.

`scripts/run_day_trading_agent.sh` is the single scheduled entry. The Friday COT job must not start this pipeline as well.

News sources live in `additional_news.py` (`RSS_FEEDS`). Run `python3 additional_news.py` to see what each feed returned. GNews on the free plan delays articles by about 12 hours, so the same searches also run on Google News, and GNews items are labelled as delayed.

Set `GOLD_LEGACY_VOLATILITY_GATE=1` only when you want the old "skip the model in a quiet market" behavior for a comparison. Leave it unset for normal runs.

Each run also includes a gold direction dashboard (`gold_direction.py`), which you can print on its own with `python3 gold_direction.py`. It covers the 10-year yield, the VIX fear index, stocks, oil, the dollar, silver, copper, Fed rate expectations from Fed funds futures, weekly fund positioning from the CFTC, and GLD activity. Those readings feed the model's picture of the market. They are not a trade direction.

### Autopilot with paper trades (`gold_autopilot.py`)

```bash
python3 gold_autopilot.py            # run continuously (Ctrl+C to stop; it resumes where it left off)
python3 gold_autopilot.py --status   # what it is doing right now
python3 gold_autopilot.py --reset    # drop a watch or an unfilled order (refused while a trade is open)
```

- **WAIT / NO TRADE:** the card gives two prices, for example "15M close above $4167.70 or below $4129.40". After each 15-minute candle the autopilot checks live spot from gold-api. A close beyond either price runs the pipeline again. If neither is reached within 4 hours, or within 60 minutes when the card gave no prices, it runs again to refresh the levels.
- **LONG / SHORT:** a paper order waits for price to enter the entry zone before the card expires, which is about 15 minutes. If it fills, that one paper trade stays open until spot touches the stop or the target. The pipeline does not run and no other trade opens until it closes. An order that never fills is logged as `MISSED_UNFILLED`, and the pipeline runs again.
- **Journal:** every result is added to `trade_backlog.csv` as `PAPER_001`, `PAPER_002`, and so on. The P&L subtracts $0.40 per ounce for costs. Paper rows are shown to the model, but they never change the real account balance or the daily loss figures.
- It only calls the model while gold is trading: Sunday 6pm to Friday 5pm New York time, with a daily break from 5pm to 6pm.
- Settings: `GOLD_PAPER_RISK_USD` sets the dollar risk per paper trade (default 125). `GOLD_RECHECK_MAX_MINUTES` (default 240) and `GOLD_RECHECK_FALLBACK_MINUTES` (default 60) set the re-run timers. `GOLD_PAPER_POLL_SECONDS` (default 15) sets how often the price is checked.
- Limits: spot is checked every 15 seconds, so a brief spike between checks can be missed. Fills use the live spot price, which has no bid/ask spread. Nothing is sent to a broker.

## Supply / demand research bot

```bash
python3 -m supply_demand_strat --macro
```

## GOLD-OCT26-v1 paper agents (A / B / C)

These are the three gold strategies from `October_2026_Gold_Trading_System.txt`.
They paper-trade on this machine only. They do **not** connect to a funded
account or MetaTrader.

| Agent | Folder | What it does | How to run |
|---|---|---|---|
| **A** | `deepseek_trial/a_h4_m15_pullback/` | H4 trend + M15 pullback/reclaim | `python3 -m deepseek_trial.a_h4_m15_pullback replay` |
| **B** | `deepseek_trial/b_london_sweep/` | London overnight sweep + reclaim | `python3 -m deepseek_trial.b_london_sweep replay` |
| **C** | `deepseek_trial/c_ny_range_retest/` | NY 09:30–09:45 break + later retest | `python3 -m deepseek_trial.c_ny_range_retest replay` |

Shared code (risk, calendar, DeepSeek TAKE/PASS, six books): `deepseek_trial/`.

```bash
# tests
python3 -m unittest deepseek_trial.test_oct26 -v

# one agent, mechanical replay (no API)
python3 -m deepseek_trial.a_h4_m15_pullback replay

# one agent, mechanical + DeepSeek TAKE/PASS (needs DEEPSEEK_API_KEY in .env)
python3 -m deepseek_trial.a_h4_m15_pullback paper --audit

# all six books + combined mechanical portfolio (no API)
python3 -m deepseek_trial

# same, with DeepSeek TAKE/PASS on the AI books
python3 -m deepseek_trial --audit
```

Full clocks, risk, news rules, and automation split:
`deepseek_trial/INSTRUCTIONS.md`.

## v3 DeepSeek knowledge agents (propose TRADE/SKIP, max 5 fills/day)

Print **30-day API cost first** (5 trades/day/agent):

```bash
python3 -m deepseek_trial.v3_knowledge cost
python3 -m deepseek_trial.v3_a_knowledge cost
python3 -m deepseek_trial.v3_b_knowledge cost
python3 -m deepseek_trial.v3_c_knowledge cost
```

| Agent | Folder |
|---|---|
| A | `deepseek_trial/v3_a_knowledge/` |
| B | `deepseek_trial/v3_b_knowledge/` |
| C | `deepseek_trial/v3_c_knowledge/` |

```bash
python3 -m deepseek_trial.v3_a_knowledge replay
python3 -m deepseek_trial.v3_a_knowledge paper --audit
```

