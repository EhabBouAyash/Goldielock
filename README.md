# Goldielock — day-trading insights agent

This is the original Goldielock **spot-gold day-trading helper**. It is a **Claude** agent (not the DeepSeek paper bots in `deepseek_trial/`). It does **not** place trades. It collects gold charts and macro context, scores the setup top-down, and writes a **LONG / SHORT / NO-TRADE** note you can use on TradingView paper or a FundedNext Stellar 1-Step **$25,000** CFD (manual trading only).

In plain language: it looks at news, the dollar, yields, and gold candles from **4-hour down to 5-minute**, then asks Claude whether a trade is worth taking under prop-account loss limits.

---

## What to run

From this folder (where `.env` lives):

```bash
pip install -r requirements.txt

# One analysis, print to the terminal (does not log a signal)
python3 day_trading_insights.py --option default

# One analysis and log it to signal_log.csv
python3 day_trading_insights.py --option trading

# Keep running: scout + signal every 5 minutes (change with --interval-minutes)
python3 day_trading_insights.py --option agent
python3 day_trading_insights.py --option agent --once
python3 day_trading_insights.py --option agent --interval-minutes 15 --max-cycles 4
```

Same pipeline, then an **interactive Claude chat** about the last signal:

```bash
python3 day_trading_insights_chat.py
python3 day_trading_insights_chat.py --option trading
python3 day_trading_insights_chat.py --no-chat
```

---

## Setup

Put keys in `.env` (do not commit this file):

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | Claude (required) |
| `GNEWS_API_KEY` | Extra headline search (optional; agent still runs without it) |

Also useful: `pip` packages in `requirements.txt` (`requests`, `pandas`, `python-dotenv`, `langgraph`, `beautifulsoup4`, `yfinance`).

---

## How one cycle works

LangGraph runs four steps in order:

1. **Collect market context** — gold/silver price, DXY, 10Y real yield and breakeven, Treasury and Fed snapshots, 5m/15m/1h/4h XAUUSD bars, prop-account snapshot from your trade backlog.
2. **Scout the internet** — GNews disruption queries plus extra metals/macro RSS and a USD high-impact calendar (when `additional_news.py` is present).
3. **Generate trade signal** — Claude scores **macro 50% → 4H 20% → 1H 12% → 15M 10% → 5M 8%**, then prints a signal with entry, stop, targets, and size under $25k prop guardrails.
4. **Persist** — writes the note and supporting files listed below.

`--option default` skips writing a row to `signal_log.csv`. `--option trading` and `agent` log the parsed signal.

This is **advice on a chart**, not a broker connection. Match-Trader / FundedNext CFD is **manual**; do not wire this script to an EA.

---

## Files around `day_trading_insights.py`

### Scripts you run or that it imports

| File | Role |
|---|---|
| `day_trading_insights.py` | Main agent: collect → scout → Claude → save |
| `day_trading_insights_chat.py` | Same agent, then Q&A on the last output |
| `generate_insights.py` | Shared fetchers (gold, DXY, FRED-style macros, Treasury/Fed) and `signal_log` / trade-log helpers |
| `fetch_intraday.py` | 5m / 15m / 1h / 4h bars and confluence scores |
| `fetch_market_data.py` | Yahoo chart API used by the bar fetcher |
| `additional_news.py` | Extra RSS/calendar if GNews is thin |
| `prop_account_tracker.py` | Reads `trade_backlog.txt` / `.csv`, applies $25k daily/max-loss math for the prompt |
| `token_tracker.py` | Appends Claude usage to `token_usage.jsonl` |

`generate_insights.py` can still be run on its own as the older **macro / slower-timeframe** insights script. Day-trading mode is the path above.

### Outputs this agent writes or updates

| File | Role |
|---|---|
| `day_trading_insights.txt` | Latest Claude write-up |
| `day_trading_live_data.txt` | Latest user prompt (the data pack sent to Claude) |
| `day_trading_intraday_snapshot.json` | Last good bar snapshot (fallback if a live fetch fails) |
| `day_trading_intraday_archive.jsonl` | History of those snapshots |
| `gold_agent_state.json` | Last agent cycle snapshot |
| `gold_disruption_events.json` | Ranked disruption headlines |
| `gold_loopback_evaluations.jsonl` | How this cycle compared with the previous signal |
| `signal_log.csv` / `signal_log.pkl` | Logged LONG/SHORT/NO-TRADE rows (`trading` / `agent`) |
| `token_usage.jsonl` | Token/cost log |
| `trade_backlog.txt` / `trade_backlog.csv` | **Your** manual trades; the agent reads these for remaining daily loss room |

Older notes in `DAY_TRADING_MODE.md` describe an earlier prompt and account size. Prefer this README and the prompt inside `day_trading_insights.py` as current.

---

## Prop numbers the prompt assumes

FundedNext Stellar 1-Step style **$25,000** CFD (initial balance):

- Daily loss: soft **$625** / hard **$750** (including open P&L)
- Max loss: soft **$1,250** / hard **$1,500** (equity floor **$23,500**)
- Pass target: **+$2,500**; at least **two** days with a trade
- Per-trade risk cap in the prompt: **1% ($250)**
- TradingView paper leverage presets: 1 / 10 / 50 / 100

Fill in `trade_backlog.txt` after you trade so the next cycle knows how much daily room is left.

---

## Related (not this agent)

`deepseek_trial/` is a separate GOLD-OCT26 paper replay (DeepSeek, no Claude). See `INSTRUCTIONS_TO_RUN.md` and `deepseek_trial/INSTRUCTIONS.md` for those bots.
