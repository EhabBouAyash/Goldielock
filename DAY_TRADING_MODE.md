# Day Trading Mode - Separate Endpoint

## Overview

`day_trading_insights.py` is a **separate, day-trading-focused** script that runs alongside the original `generate_insights.py` (macro-focused).

Key differences from the macro version:

| Aspect | Macro Mode (`generate_insights.py`) | Day Trading Mode (`day_trading_insights.py`) |
|--------|-------------------------------------|---------------------------------------------|
| **Primary timeframe** | Daily, weekly, monthly | 5m, 15m, 1h |
| **Decision driver** | Macro regime (real yields, DXY, Fed) | Chart patterns, intraday structure |
| **Data fetched** | News (Kitco, GNews), COT, macro indicators | Intraday bars + minimal macro context |
| **Signal threshold** | 2+ macro indicators must align | Pattern recognition + confluence |
| **Best for** | Position trading, weekly swings | Day trading, scalps, intraday swings |
| **Token cost per run** | ~15,000 tokens (~$0.05) | ~6,500 tokens (~$0.04) |
| **Allows NO-TRADE** | Says NEUTRAL | Says NO-TRADE explicitly |

---

## Token Breakdown

**Day Trading Mode:**
- System prompt: **~2,522 tokens** (vs ~3,000 for macro)
- User message: ~3,000-5,000 tokens (intraday data + trade history)
- **Total input: ~6,500 tokens**
- Output: ~2,000 tokens
- **Cost per run: ~$0.04**

**Cost projection (1h interval, 24/day):**
- Daily: $0.96
- Monthly: **~$28.80**
- (vs ~$36/month for macro mode)

---

## How to Run

### Standalone (Test Mode)
```bash
cd /home/vboxuser/goldproject

# Analysis only (no signal logging)
python3 day_trading_insights.py --option default

# Full trading mode (logs to signal_log.csv)
python3 day_trading_insights.py --option trading
```

### Continuous Monitor (Already Configured)
The monitor now uses `day_trading_insights.py` instead of `generate_insights.py`:

```bash
python3 monitor_trades.py --once    # Single run
python3 monitor_trades.py            # Continuous (1h intervals)
./start_monitor.sh                   # Background job
```

The monitor runs the day trading version, sends emails, tracks tokens.

---

## What's in the Day Trading Prompt

### Pattern Library (5 patterns the model looks for)

1. **Trend Continuation** — Pullback to MA in established trend
2. **Range Breakout** — Consolidation break with volume
3. **RSI Extremes + Divergence** — Counter-trend mean reversion
4. **Momentum Breakout** — MACD/RSI/price alignment
5. **News/Event Fade** — Reversal of news-driven extremes

### Decision Tree (5 steps)

1. What's the 1h trend? (UP/DOWN/SIDEWAYS)
2. What's the 15m structure? (TRENDING/CONSOLIDATING/CHOPPY)
3. What's the 5m setup? (Clean entry / wait / NO-TRADE)
4. Risk check (stop, R/R, liquidation)
5. Execute or pass

### Signal Output

Each analysis includes:
```
SIGNAL: LONG / SHORT / NO-TRADE
Type: SCALP / SWING
Entry: $X
Stop: $X (with technical reason)
Target 1: $X (>= 1.5R)
Target 2: $X (>= 2.5R)

Risk math:
  Account: $800
  Stop distance: X%
  Risk mode: AGGRESSIVE/BALANCED/NO-LEVERAGE
  Max risk: $X
  Implied leverage: Xx
  Chosen leverage: Xx
  Liquidation: $X (must be > 50 pips from stop)

Confidence: HIGH/MEDIUM/LOW
Why this trade has edge: [2-3 reasons]
```

### Or NO-TRADE Output:
```
SIGNAL: NO-TRADE
Reason: [Specific - choppy chart, conflicting timeframes, etc.]
What would change: [Conditions to watch]
Next check: [When to re-evaluate]
```

---

## Critical Rules (Built into the Prompt)

1. Every trade has a stop. NO EXCEPTIONS.
2. Stop must be at a technical level.
3. Liquidation > 50 pips from stop on 5x+ leverage.
4. R/R minimum is 1.5:1.
5. If pattern unclear → NO-TRADE.
6. If 15m and 1h disagree → NO-TRADE.
7. Don't chase moves > 2% intraday.
8. Don't trade 30 min before/after major news.
9. Never risk more than 18% per trade (sub-$1k account).
10. After 2 consecutive losses → mandatory cooldown.

---

## Differences in Practice

### Old (Macro Mode):
> "Real yields are falling, DXY is weak, Fed is dovish. SIGNAL: LONG XAU. Entry $4700-4720, stop $4620, target $4850 ($4850 = +1.8R). Confidence MEDIUM."

### New (Day Trading Mode):
> "1h trend: UP. 15m structure: pullback to SMA(9). 5m RSI bounced above 30. PATTERN: Trend Continuation. SIGNAL: LONG (SCALP). Entry $4715, stop $4710, target 1: $4722 (1.4R), target 2: $4730 (3.0R). Leverage: 5x. Confidence: HIGH."

---

## Files

| File | Purpose |
|------|---------|
| `day_trading_insights.py` | The day trading script (NEW) |
| `day_trading_insights.txt` | Latest analysis output (auto-generated) |
| `day_trading_live_data.txt` | Latest user message sent to Claude (auto-generated) |
| `signal_log.csv` | Same as before — predictions logged here |
| `token_usage.jsonl` | Includes endpoint marker: `"endpoint": "day_trading_insights"` |

---

## Switching Back to Macro Mode

If you want to test the old macro-focused mode:

```bash
# Edit monitor_trades.py
# Change "day_trading_insights.py" back to "generate_insights.py"
# Or run directly:
python3 generate_insights.py --option trading
```

Both scripts write to `signal_log.csv` so you can compare accuracy across modes.

---

## What to Watch

Now that day trading mode is active, monitor for:

1. **NO-TRADE frequency** — How often the model sits out (good thing if it's choppy)
2. **Pattern matches** — Does it correctly identify Trend Continuation vs Breakout vs Reversal?
3. **R/R quality** — Are targets actually hitting? (Track in `signal_log.csv`)
4. **Leverage discipline** — Is it choosing reasonable leverage based on stop distance?
5. **Win rate** — Compare to macro mode after 50+ signals

If accuracy drops significantly below macro mode, switch back. If it's similar or better, day trading mode is the new default.
