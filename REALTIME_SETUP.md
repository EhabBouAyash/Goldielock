# Real-Time Gold Trading Pipeline - Setup & Usage

## Overview

Your gold trading system now has:

1. **Intraday data fetching** (`fetch_intraday.py`) — 5m, 15m, 1h bars with technicals
2. **Continuous monitoring** (`monitor_trades.py`) — Runs predictions on schedule + tracks trends
3. **Trade logging** (`trade_backlog.txt`) — Manual trade outcomes for feedback loop
4. **Trend dashboard** (`trend_dashboard.json`) — Win rate, accuracy, confidence stats

---

## Quick Start

### 1. One-Time Prediction (Test)

```bash
python3 monitor_trades.py --once
```

This runs a single cycle:
- Fetches intraday bars → `intraday_data.json`
- Runs trading predictions → updates `signal_log.csv`
- Updates trend stats → `trend_dashboard.json`

### 2. Continuous Monitoring (1h intervals)

```bash
python3 monitor_trades.py
```

Or specify custom interval (in minutes):
```bash
python3 monitor_trades.py 30  # Run every 30 minutes
python3 monitor_trades.py 15  # Run every 15 minutes
```

**Default is now 1 hour** (was 15 min) to reduce API costs.

This will:
- Run predictions at your interval
- Log all events to `monitor.log`
- Update intraday data continuously
- Track accuracy metrics in real-time

### 3. Manual Trade Logging

After each _real trade_, add a line to `trade_backlog.txt`:

```
2026-04-28, TRADE_001, LONG, 4720.0, 4850.0, WIN, 1.7, Hit first target on bounce. Volume confirmed.
```

Format:
```
DATE, TRADE_ID, SIGNAL, ENTRY_PRICE, EXIT_PRICE, OUTCOME, R_MULTIPLE, NOTES
```

Where `OUTCOME` is one of: `WIN`, `LOSS`, `PARTIAL`, `OPEN`

Next time you run predictions, Claude sees your trades and learns from them.

---

## Files Reference

### Token Tracking

Each run now logs token usage automatically:

```bash
cat token_usage.jsonl        # All token events (JSON lines)
cat token_stats.json         # Summary statistics
```

The monitor displays token stats after each run:
```
💰 Token Usage:
   Avg/Run: 15000 tokens ($0.050000)
   Total Cost: $1.234 (25 runs)
   Projected: $1.200/day | $36.00/month
```

### New Files

| File | Purpose |
|------|---------|
| `token_tracker.py` | Token counting & cost calculation |
| `token_usage.jsonl` | Per-run token logs (JSON lines) |
| `token_stats.json` | Aggregated token statistics |
| `fetch_intraday.py` | Fetch 5m/15m/1h bars + technicals (SMA, RSI, MACD) |
| `monitor_trades.py` | Continuous monitoring loop, logging, dashboard updates |
| `intraday_data.json` | Latest 5m/15m/1h bars (updated per run) |
| `monitor.log` | All monitoring events (JSON lines, append-only) |
| `trend_dashboard.json` | Summary stats: accuracy, win rate, confidence |
| `trade_backlog.txt` | Your manual trade outcomes (input for Claude feedback) |
| `REALTIME_SETUP.md` | This guide |

### Existing Files (Enhanced)

| File | Changes |
|------|---------|
| `signal_log.csv` | Now tracked by monitor for accuracy trends |
| `generate_insights.py` | Can be run via monitor (unchanged, still works standalone) |

---

## Understanding the Data Flow

```
┌─────────────────────────────────────────────┐
│ monitor_trades.py (runs on schedule)        │
├─────────────────────────────────────────────┤
│ 1. fetch_intraday.py → intraday_data.json   │
│    (5m/15m/1h bars + SMA/RSI/MACD)          │
├─────────────────────────────────────────────┤
│ 2. generate_insights.py → signal_log.csv    │
│    (Claude analyzes all data + intraday)    │
│    Outputs: LONG/SHORT/NEUTRAL + confidence │
├─────────────────────────────────────────────┤
│ 3. Your manual trades → trade_backlog.txt   │
│    (You log outcomes when you trade)        │
├─────────────────────────────────────────────┤
│ 4. Update trend_dashboard.json              │
│    (Accuracy, win rate, confidence stats)   │
└─────────────────────────────────────────────┘
```

---

## Monitoring Logs

`monitor.log` is JSON lines format. Read it like this:

```bash
# See all events
cat monitor.log

# Follow real-time
tail -f monitor.log

# Pretty-print latest
tail -1 monitor.log | python3 -m json.tool
```

Example log entry:
```json
{
  "timestamp": "2026-04-29T00:45:30.123456",
  "type": "PREDICTION_SUCCESS",
  "message": "Trading predictions completed",
  "details": {
    "stdout": "Signal: LONG, Confidence: MEDIUM..."
  }
}
```

---

## Trend Dashboard

Check performance anytime:

```bash
# Pretty-print current stats
python3 -c "import json; print(json.dumps(json.load(open('trend_dashboard.json')), indent=2))"
```

Output:
```json
{
  "timestamp": "2026-04-29T00:45:35...",
  "summary": {
    "total_signals": 18,
    "correct_signals": 9,
    "overall_accuracy": 50.0
  },
  "by_signal_type": {
    "long": {
      "count": 5,
      "correct": 3,
      "accuracy": 60.0
    },
    "short": {...},
    "neutral": {...}
  },
  "time_windows": {
    "7_day": {"accuracy": 57.14, "signals": 7},
    "30_day": {...}
  }
}
```

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'yfinance'"

Install dependencies:
```bash
pip3 install pandas yfinance requests beautifulsoup4 python-dotenv
```

### Intraday data empty

yfinance may rate-limit or have connectivity issues. Check:
```bash
python3 -c "from fetch_intraday import fetch_intraday_bars; import json; print(json.dumps(fetch_intraday_bars(), indent=2))" 
```

### Predictions taking too long

Increase the timeout in `monitor_trades.py` line ~50:
```python
timeout=180  # was 120, now 3 minutes
```

### Trade backlog not being read

Ensure `trade_backlog.txt` exists and has the right format. Check:
```bash
cat trade_backlog.txt
```

Should look like:
```
# Trade Backlog - Manual Trade Logging
# Format: DATE, TRADE_ID, ...
2026-04-28, TRADE_001, LONG, 4720.0, 4850.0, WIN, 1.7, Notes here
```

---

## Next Steps

1. **Run one test cycle:**
   ```bash
   python3 monitor_trades.py --once
   ```

2. **Check outputs:**
   ```bash
   cat intraday_data.json          # Intraday bars
   tail signal_log.csv             # Latest signals
   cat trend_dashboard.json        # Accuracy stats
   ```

3. **Log your first real trade:**
   ```
   echo "2026-04-29, TRADE_001, LONG, 4600.0, 4720.0, WIN, 2.0, Test trade" >> trade_backlog.txt
   ```

4. **Run continuous monitoring:**
   ```bash
   nohup python3 monitor_trades.py 15 > monitor_run.log 2>&1 &
   ```

---

## Tips & Tricks

- **Lightweight check:** Just fetch intraday without full prediction:
  ```bash
  python3 -c "from fetch_intraday import get_intraday_summary; print(get_intraday_summary())"
  ```

- **Dry-run monitor:** Run once, review outputs, then schedule if satisfied.

- **Background job (Linux/Mac):**
  ```bash
  nohup python3 monitor_trades.py 15 &  # Runs every 15m in background
  disown                                 # Detach from terminal
  ```

- **Cron job (Linux/Mac):**
  ```bash
  */15 * * * * cd /home/vboxuser/goldproject && python3 monitor_trades.py --once >> cron.log 2>&1
  ```

---

That's it! Your system is now streaming intraday data, generating predictions continuously, and tracking performance in real-time.

Questions? Check `generate_insights.py` for the Claude prompt that drives predictions.
