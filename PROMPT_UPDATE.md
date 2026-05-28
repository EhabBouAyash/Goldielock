# Prompt Update: Intraday Trading Layer (2026-04-29)

## Summary

Modified the system prompt in `generate_insights.py` to add **intraday entry timing** as a secondary layer while preserving the macro-based directional framework.

**Goal:** Catch intraday moves on already-confirmed macro directions without sacrificing accuracy.

---

## What Changed

### Added Section: "INTRADAY ENTRY TIMING"

This new framework provides specific entry rules for day trades when:
1. **Macro regime already supports the direction** (real yields, DXY, Fed, etc.)
2. **Intraday technical structure aligns** (RSI, moving averages, MACD, consolidation breaks)

### Four Intraday Triggers

#### 1. **RSI Oversold Bounce** (Scalp)
- 5m/15m RSI dips below 30, then bounces back above 30
- Entry: When RSI crosses above 30
- Stop: 2-3 pips below the bounce candle
- Target: 0.5-1R (quick exit)
- Leverage: 2x-4x (tight stop)

#### 2. **Moving Average Bounce** (Scalp)
- Price touches 5m or 15m SMA(9) from above
- SMA(21) still pointing up
- Entry: At the MA or first candle to close above it
- Stop: 2-3 pips below the MA touch
- Target: 0.5-1R (scalp exit)
- Leverage: 2x-4x

#### 3. **MACD/Momentum Confirmation** (Swing)
- 15m/1h MACD histogram turns positive
- Price above 15m SMA(9)
- RSI 50-70 (rising, not overbought)
- Entry: First 5m candle above previous high
- Stop: Below recent swing low
- Target 1: 1.0-1.5R | Target 2: 2.5-3.5R
- Leverage: 3x-5x

#### 4. **Consolidation Break** (Swing)
- Price consolidates 3-5 candles sideways on 5m
- Breaks above the range with momentum
- Macro is bullish
- Entry: At break or pullback into broken level
- Stop: Below consolidation low
- Target 1: 1.0-1.5R minimum
- Leverage: 3x-5x

---

## Key Safety Rules

### Macro Gate (Mandatory)
- **Intraday setups only work if macro already supports the direction**
- If macro is NEUTRAL or bearish → NO intraday trades in the opposite direction
- Macro is the filter; technicals are just entry timing

### When NOT to Trade Intraday
- Macro is unclear or bearish
- Price within 1% of all-time high (extended)
- COT shows managed money at extreme long (washout risk)
- Gold already moved 2-3% in your intended direction (momentum exhaustion)
- DXY at support and oversold (bounce risk)
- Real yields rising (macro actually bearish)
- GVZ extremely compressed (breakout volatility risk)
- News expected within 2 hours (wait for the event)

### Position Sizing & Liquidation
- Scalps: 2x-4x leverage (tight stops: 2-5 pips)
- Swings: 3x-5x leverage (wider stops: 20-40 pips)
- Liquidation must sit 50-100 pips away from stop on 4x+ leverage
- If liquidation too close → reduce size or pass on trade

---

## Scalp vs. Swing Strategy

### SCALP (0.5-1.0R)
- Use RSI/MA bounces
- Enter, take quick profit, move on
- Risk: $50-80 per trade on $800 account
- Hold: Minutes to 1-2 hours
- Exit at Target 1 automatically (no greed)
- Best during London overlap session (high volume)

### SWING (1.5-3.0R+)
- Use MACD/consolidation breaks
- Macro must be clearly bullish
- Risk: $100-150 per trade
- Hold: 4-24 hours
- Use trailing stops once up 1.0-1.5R
- Let winners run; exit on macro breakdown

---

## Output Format Update

### Old Format
```
SIGNAL: [LONG/SHORT/NEUTRAL] XAU
Entry zone: $X - $X
Stop loss: $X
...
```

### New Format
```
SIGNAL: [LONG/SHORT/NEUTRAL] XAU

MACRO VIEW:
Entry zone: $X - $X (primary swing entry)
Stop loss: $X
Target 1/2: $X / $X
[macro analysis]

INTRADAY ENTRY CANDIDATES (only if macro is LONG/SHORT):
- SCALP: Enter $X on RSI bounce, stop $X, target $X (0.7R, 2x-4x lev)
- SWING: Enter $X on consolidation break, stop $X, target $X (1.3R + 2.8R, 4x lev)
[or: "No intraday entries until macro direction confirmed."]
```

---

## Validation Checks (Still Required)

Every signal must still satisfy:
1. **Macro confluence:** 2+ macro/weekly indicators align
2. **Math coherence:** Entry/stop/targets match risk/reward and leverage
3. **Liquidation safety:** Liquidation > 50-100 pips from stop
4. **Account sizing:** Max loss at stop < risk cap for selected mode

---

## What DIDN'T Change

- ✅ Macro regime framework (real yields, DXY, Fed, CB demand)
- ✅ Geopolitical analysis (still cautious, not reactive)
- ✅ COT positioning warnings (avoid crowded trades)
- ✅ "2+ confluence rule" (still required for macro)
- ✅ Leverage calculation formula
- ✅ Risk management and stops
- ✅ The structural bullish bias on gold (CB demand, de-dollarization)

---

## Expected Impact

### What This Fixes
- No more "macro bullish but miss the 2% intraday move" complaints
- Explicit permission to scalp bounces when macro supports
- Clearer entry timing on already-confirmed moves
- Distinction between quick scalps and extended swings

### What It Preserves
- Macro edge (the reason you have any accuracy at all)
- Risk management (still requires tight stops, liquidation buffers)
- Safety nets (macro gate, positioning warnings, consolidation rules)

### Winrate Expectation
- **Macro directional accuracy:** Should remain ~55-57% (unchanged)
- **Intraday execution:** Should improve entry timing, not frequency
- **Overall win rate:** May see slight uptick if the scalp targets (0.5-1R) execute cleanly
- **Risk:** If you ignore the "macro gate" rule, scalps will fail frequently

---

## How to Use

### For the Monitor
1. Runs every 1 hour as before
2. Includes intraday data (5m/15m/1h) in the prompt
3. Claude generates both macro direction AND intraday entry candidates
4. You see both swing targets and scalp opportunities in each email

### For Manual Trading
- Read the MACRO VIEW for the primary direction
- Check INTRADAY ENTRY CANDIDATES for timing
- If macro is NEUTRAL → ignore the intraday section, wait for clarity
- If macro is LONG/SHORT → use the scalp/swing setups as reference

### Risk
- Remember: scalp targets are tight (0.5-1R). If you miss, move on.
- Swings are for extended moves. Only take if macro is really bullish.
- Never trade intraday against the macro direction.

---

## Testing

The updated prompt is live in `generate_insights.py`. 

Run a test:
```bash
python3 monitor_trades.py --once
```

You should see:
- MACRO VIEW section (directional call)
- INTRADAY ENTRY CANDIDATES section (2-3 specific scalp/swing setups)

---

## Questions?

Refer to this file for the full framework. The prompt update preserves all safety nets while adding explicit intraday entry rules.

**Key principle: Macro first, technicals second. Always.**
