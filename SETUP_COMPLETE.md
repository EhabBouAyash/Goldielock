# Setup Complete - Automated Gold Trading Monitor

Everything is ready to run. Here's what you have:

---

## 📋 What You Have

### Core System
- ✅ **fetch_intraday.py** — Real-time 5m/15m/1h data
- ✅ **generate_insights.py** — Claude-powered predictions
- ✅ **monitor_trades.py** — Orchestrates everything hourly
- ✅ **token_tracker.py** — Cost tracking & reporting

### Email Notifications
- ✅ **email_notifier.py** — Formats and sends hourly updates
- ✅ **EMAIL_SETUP.md** — Complete setup instructions
- ✅ **start_monitor.sh** — One-command background job startup

### Trade Logging
- ✅ **trade_backlog.txt** — Manual trade logging (detailed format)
- ✅ Reads automatically → Claude learns → predictions improve

### Dashboards & Logs
- ✅ **intraday_data.json** — Latest bars + technicals
- ✅ **signal_log.csv** — All predictions + accuracy
- ✅ **trend_dashboard.json** — Win rates & accuracy stats
- ✅ **token_usage.jsonl** — Per-run token logs
- ✅ **monitor.log** — Event logs
- ✅ **monitor_background.log** — Background job logs

---

## 🚀 Quick Start (3 Steps)

### Step 1: Set Up Gmail (5 min)

Create a Gmail App Password and save it to `.env`:

```bash
# 1. Go to: https://myaccount.google.com/apppasswords
#    Generate a 16-character password

# 2. Create .env file:
cat > /home/vboxuser/goldproject/.env << 'EOF'
GMAIL_EMAIL=whooshy123@gmail.com
GMAIL_APP_PASSWORD=YOUR_16_CHAR_PASSWORD
EOF
```

Replace `YOUR_16_CHAR_PASSWORD` with your actual password from Step 1.

### Step 2: Test (1 min)

Run one prediction cycle:

```bash
cd /home/vboxuser/goldproject
python3 monitor_trades.py --once
```

Check your email for an update within 30 seconds.

### Step 3: Start Background Job (1 min)

Run the monitor 24/7:

```bash
cd /home/vboxuser/goldproject
chmod +x start_monitor.sh
./start_monitor.sh
```

That's it! The monitor now:
- Runs every 1 hour automatically
- Fetches live gold data
- Generates predictions
- Sends you an email
- Tracks costs
- Logs everything

---

## 📧 What You'll Receive

**Each hourly email includes:**

```
🎯 CURRENT PREDICTION
   Signal: LONG/SHORT/NEUTRAL
   Confidence: HIGH/MEDIUM/LOW
   Entry / Target 1 / Target 2 / Stop Loss

📊 INTRADAY TECHNICALS
   5-MIN:  Price | RSI | SMA(9/21)
   15-MIN: Price | RSI | SMA(9/21)
   1-HOUR: Price | RSI | SMA(9/21)

📈 TREND STATISTICS (Model Accuracy)
   7-Day: X%  |  30-Day: X%  |  All-Time: X%
   LONG accuracy / SHORT accuracy / NEUTRAL accuracy

💰 TOKEN USAGE & COSTS
   Avg: ~15,000 tokens/run ($0.05)
   Projected: $1.20/day | $36/month

🏆 RECENT MANUAL TRADES
   Your logged trades from trade_backlog.txt
```

---

## 📝 Logging Your Trades

After you execute a trade, add it to `trade_backlog.txt`:

**Format:**
```
DATE, TRADE_ID, SIGNAL, ENTRY_PRICE, EXIT_PRICE, OUTCOME, R_MULTIPLE, NOTES
```

**Example:**
```
2026-04-29, TRADE_001, LONG, 4720.0, 4850.0, WIN, 1.7, Hit first target on bounce
2026-04-29, TRADE_002, SHORT, 4650.0, 4600.0, LOSS, -1.0, Stopped out. False breakout.
```

**OUTCOME values:**
- `WIN` — Hit first target or better
- `LOSS` — Hit stop loss
- `PARTIAL` — Closed some position
- `OPEN` — Still holding (0.0 for exit price)

**R_MULTIPLE:**
- Wins: positive (e.g., 1.7 = earned 1.7x your risk)
- Losses: negative (e.g., -1.0 = lost your full risk)

Claude reads these on the next hourly run and learns from your execution.

---

## 🛑 Stop/Restart Commands

**Check if running:**
```bash
ps aux | grep monitor_trades.py
```

**Stop monitoring:**
```bash
pkill -f "monitor_trades.py"
```

**View logs:**
```bash
tail -f monitor_background.log
```

**Restart:**
```bash
./start_monitor.sh
```

---

## 💾 Files You'll See Generate

After each run:
- `intraday_data.json` — Latest bars (5m/15m/1h)
- `signal_log.csv` — New row added with prediction
- `trend_dashboard.json` — Updated accuracy stats
- `token_usage.jsonl` — New line with token count
- `monitor.log` — Event entries
- **Email** — Sent to whooshy123@gmail.com

All append-only, never overwrite. Your data is preserved.

---

## 📊 Cost Projection

**At 1-hour intervals (24/day):**
- ~$0.05 per run
- **~$1.20 per day**
- **~$36 per month** (continuous 24/7)

Tracked in `token_stats.json`:
```bash
cat token_stats.json | python3 -m json.tool
```

You can adjust interval to reduce costs:
```bash
python3 monitor_trades.py 120  # Every 2 hours = $18/month
python3 monitor_trades.py 240  # Every 4 hours = $9/month
```

---

## 🔧 Troubleshooting

### Email not sending?

```bash
# Check .env exists:
cat /home/vboxuser/goldproject/.env

# Test email manually:
python3 -c "from email_notifier import send_email; print(send_email('whooshy123@gmail.com'))"
```

### Monitor crashed?

```bash
# Check logs:
tail -100 monitor_background.log

# Restart:
./start_monitor.sh
```

### Want to change interval?

```bash
pkill -f monitor_trades.py
python3 monitor_trades.py 120  # Every 2 hours
```

---

## 📚 Documentation

- **EMAIL_SETUP.md** — Gmail password setup + troubleshooting
- **REALTIME_SETUP.md** — Overview of all components
- **TOKEN_TRACKING.md** — Cost optimization tips
- **trade_backlog.txt** — Trade logging format (with examples)
- **SETUP_COMPLETE.md** — This file

---

## ✅ You're Ready

Everything is configured and tested. Just:

1. Create `.env` with Gmail credentials
2. Run `./start_monitor.sh`
3. Receive hourly emails with predictions
4. Log your trades in `trade_backlog.txt`
5. Watch the system improve as it learns from your execution

Questions? Check the documentation files or review `monitor.log` for event details.

**Start monitoring now:**
```bash
cd /home/vboxuser/goldproject
./start_monitor.sh
```

Good luck! 🚀
