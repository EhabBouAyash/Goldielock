# Pipeline Updates - April 18, 2026

## Summary
Implemented 5 major enhancements to the gold/silver trading insights pipeline based on gap analysis.

---

## 1. Added Missing Market Data Sources

### New Data Points (all via yfinance or FRED):
- **GVZ (Gold Volatility Index)** - `^GVZ` - Options fear gauge for gold
- **Bitcoin (BTC-USD)** - BTC/gold correlation indicator for anti-fiat sentiment
- **S&P 500 (^GSPC)** - Stocks/gold correlation for risk-on/risk-off regime
- **10Y Breakeven Inflation (T10YIE)** - FRED series to separate inflation expectations from real yields
- **GLD ETF Holdings** - Institutional gold flow proxy (price + 5-day change)

These were all explicitly referenced in the Claude system prompt but never fetched. Now the LLM has access to the data it was trained to analyze.

---

## 2. Added Silver Technical Indicators

Previously only gold had moving averages. Now silver has:
- **200-day MA**
- **50-day MA**

This matches the gold technical structure and allows the LLM to properly assess silver's positioning.

---

## 3. Multi-Timeframe RSI for Both Metals

The system prompt calls monthly RSI "the regime indicator" but we were only computing 14-day RSI. Now tracking:

| Metal | Timeframes |
|---|---|
| Gold | 14-day, Weekly, Monthly |
| Silver | 14-day, Weekly, Monthly |

This allows proper regime identification (bull/bear/consolidation) as the prompt expects.

---

## 4. Enhanced Signal Tracking & Accuracy Dashboard

### Old Signal Log Columns:
```
DATE, GOLD_PRICE, LONG, SHORT, NEUTRAL, CORRECT
```

### New Signal Log Columns:
```
DATE, GOLD_PRICE, LONG, SHORT, NEUTRAL, 
CONFIDENCE, ENTRY_LOW, ENTRY_HIGH, TARGET1, TARGET2, STOP_LOSS,
CORRECT, HIT_TARGET1, HIT_TARGET2, HIT_STOP,
ACCURACY_7D, ACCURACY_30D, ACCURACY_ALL
```

### What's Tracked Now:
- **Confidence level** (LOW/MEDIUM/HIGH) from Claude output
- **Entry zones** (low and high bounds)
- **Target prices** (T1 and T2)
- **Stop loss** level
- **Target hits** (did price reach T1/T2 within 5 days?)
- **Stop hits** (did price hit stop within 5 days?)
- **Rolling accuracy** (7-day, 30-day, all-time windows)

### Enhanced Parsing:
`parse_gold_signal()` now returns a dict with all signal components instead of just "LONG/SHORT/NEUTRAL" string. This extracts structured data from the Claude SIGNAL SUMMARY section using regex.

### Better Evaluation:
`_evaluate_previous_row()` now checks not just next-day direction but also whether targets/stops were hit in the following 5 trading days. This gives a true picture of whether the signal was actionable.

---

## 5. Switched to Daily Execution

### Before:
- Ran **Fridays only at 3:30 PM ET**
- 6 days a week of blindness despite daily data availability

### After:
- Runs **daily at 6:00 PM ET** (after US market close)
- COT data (weekly, released Fridays) is included whenever available
- All other data (price, macro, news, technicals) updates daily

### Updated Files:
- `launchd/com.ehab.cot-insights-friday.plist` - removed Weekday constraint, changed time to 18:00
- `scripts/friday_cot_insights.sh` - fixed venv path reference

---

## Architecture Notes

### Data Flow:
1. `analyze()` fetches all data sources
2. Builds `live_data` string with formatted output
3. Writes to `live_data.txt` for inspection
4. Sends to Claude API with system prompt
5. Parses response for signal details
6. Updates `signal_log.pkl` and `signal_log.csv` with:
   - Today's signal + all metadata
   - Yesterday's evaluation (correct? targets hit? stop hit?)
   - Rolling accuracy stats

### New Dependencies:
None! All new data sources use existing libraries:
- yfinance (already used for DXY, oil, VIX)
- FRED API (already used for real yields, CPI, etc.)

### Backward Compatibility:
- Old signal log rows get new columns filled with default values (0, NaN, None as appropriate)
- Pickle is primary storage (fast), CSV is mirror (human-readable)

---

## Testing Checklist

Before first run:
- [ ] Reload launchd service: `launchctl unload ~/Library/LaunchAgents/com.ehab.cot-insights-friday.plist && launchctl load ~/Library/LaunchAgents/com.ehab.cot-insights-friday.plist`
- [ ] Check logs after next scheduled run: `tail -f logs/cot_insights.out.log`
- [ ] Verify new columns appear in `signal_log.csv`
- [ ] Confirm `live_data.txt` includes GVZ, BTC, S&P, breakeven inflation, GLD, and all new RSI/MA values

---

## What's Still Missing (Future Work)

From the original gap analysis, these weren't implemented yet:

### Tier 2 (High Value):
- Gold ETF flows from World Gold Council (need new API/scraper)
- COMEX warehouse/inventory data (need COMEX data feed)
- Shanghai Gold Premium (SGE vs LBMA spread) - strong China demand signal
- Sentiment scoring on headlines (NLP before sending to LLM)

### Tier 3 (Infrastructure):
- Alerting (webhook to Telegram/Discord/email on HIGH confidence signals)
- Data staleness checks (flag if FRED/gold-api returns stale data)
- API keys cleanup (move FRED and GNEWS keys from source to .env)

---

## Impact Summary

| Enhancement | Before | After | Benefit |
|---|---|---|---|
| Market data coverage | 6 APIs, missing key indicators | 6 APIs, all prompt-referenced data | LLM has complete context |
| Technical indicators | Gold only | Gold + Silver, 3 timeframes each | Proper regime identification |
| Signal tracking | Binary next-day direction | Full target/stop/confidence tracking | Measure real profitability |
| Accuracy metrics | None (just 2 rows of data) | 7d/30d/all-time rolling accuracy | Know if system works |
| Execution frequency | 1x/week (Fridays) | 7x/week (daily) | React to daily moves |

