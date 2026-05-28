# Token Tracking & Cost Management

## Summary

Every run now automatically tracks API usage and cost. Default interval is **1 hour** (down from 15 min) to balance freshness vs. cost.

## Cost Projection

**At 1-hour intervals (24 runs/day):**
- ~15,000 tokens/run × $3.50/1M = **~$0.05 per run**
- 24 runs/day × $0.05 = **$1.20/day**
- **~$36/month** (24/7 continuous monitoring)

**Cost comparison:**
| Interval | Runs/Day | Cost/Day | Cost/Month |
|----------|----------|----------|-----------|
| 15 min | 96 | $4.80 | $150 |
| 30 min | 48 | $2.40 | $75 |
| **1 hour (default)** | **24** | **$1.20** | **$36** |
| 2 hours | 12 | $0.60 | $18 |

## File Structure

**New files:**
- `token_tracker.py` — Utilities for token counting & cost calculation
- `token_usage.jsonl` — Per-run logs (JSON lines, append-only)
- `token_stats.json` — Aggregate statistics

**Generated files:**
- `token_usage.jsonl` — Grows with each run
- `token_stats.json` — Updated after each run

## How It Works

### 1. During Each Run

The `generate_insights.py` script now:
1. Calls Claude's API (as before)
2. Captures `usage.input_tokens` and `usage.output_tokens` from response
3. Logs to `token_tracker.log_token_usage()` with:
   - Timestamp
   - Event type: "actual"
   - Input/output/total tokens
   - Cost in USD
   - Model name

### 2. After Each Run

`monitor_trades.py` calls `update_token_stats()` which:
- Reads all entries from `token_usage.jsonl`
- Calculates totals and averages
- Projects daily and monthly costs
- Writes summary to `token_stats.json`

### 3. Display

After each run, monitor shows:
```
💰 Token Usage:
   Avg/Run: 15342 tokens ($0.050670)
   Total Cost: $1.265 (25 runs)
   Projected: $1.215/day | $36.45/month
```

## Commands

**View all token events:**
```bash
cat token_usage.jsonl
```

**Pretty-print a single event:**
```bash
tail -1 token_usage.jsonl | python3 -m json.tool
```

**View aggregated stats:**
```bash
cat token_stats.json | python3 -m json.tool
```

**Monitor stats in real-time:**
```bash
watch -n 60 'python3 -c "import json; s=json.load(open(\"token_stats.json\")); print(f\"${s[\"projected_monthly_cost\"]:.2f}/month | {s[\"total_runs\"]} runs\")"'
```

## API Pricing (Claude 3.5 Sonnet, as of April 2026)

- **Input tokens:** $3.00 per 1M tokens
- **Output tokens:** $15.00 per 1M tokens

Typical distribution per run:
- ~12k input tokens (data + system prompt)
- ~3k output tokens (Claude response)
- Total: ~15k tokens

## Optimization Tips

If you want to reduce costs further:

1. **Skip news** — Comment out Kitco/GNews fetching = ~6k tokens saved
   ```python
   # kitco_news = fetch_kitco_news()  # Comment this line
   kitco_news = "(SKIPPED FOR COST)"
   ```

2. **Reduce intraday bars** — Use 25 instead of 50 candles
   ```python
   bars = hist.tail(25).copy()  # was 50
   ```

3. **Run less frequently** — 2-hour or 4-hour intervals
   ```bash
   python3 monitor_trades.py 120  # 2 hours
   ```

4. **Batch mode** — Run once daily, manually review charts
   ```bash
   python3 monitor_trades.py --once
   ```

## Troubleshooting

**Token stats not updating:**
```bash
python3 token_tracker.py  # Run directly to check
```

**No token_usage.jsonl file:**
- Make sure `generate_insights.py` ran successfully
- Check `monitor.log` for errors

**Cost projection wrong:**
- Delete `token_stats.json` and let the next run regenerate it
- Or manually recalculate:
  ```python
  from token_tracker import update_token_stats
  update_token_stats()
  ```

## Notes

- Token counts are from Anthropic's API response headers (`usage.input_tokens`, `usage.output_tokens`)
- Pricing is based on Claude 3.5 Sonnet rates (may change)
- Cache hits (if you re-send identical data) would reduce token counts
- All numbers are projected; actual costs may vary slightly
