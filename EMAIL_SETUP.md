# Email Setup Guide - Hourly Trading Predictions

## Overview

Your trading monitor will send you **formatted email updates every hour** with:
- 🎯 Current prediction (LONG/SHORT/NEUTRAL + confidence)
- 📊 Intraday technicals (5m/15m/1h prices + RSI)
- 📈 Trend statistics (7-day/30-day accuracy)
- 💰 Token usage & projected costs
- 🏆 Recent manual trades you logged

Email recipient: **whooshy123@gmail.com**

---

## Setup (5 minutes)

### Step 1: Create Gmail App Password

The monitor uses Gmail SMTP to send emails. You need a special **App Password** (not your regular Gmail password).

**Instructions:**

1. Go to: https://myaccount.google.com/apppasswords
2. You'll be asked to sign in (use your Gmail credentials)
3. Select:
   - Device type: **Select other (custom name)**
   - Type: **Trading Monitor**
4. Click **Generate**
5. Google will show a **16-character password** (looks like: `abcd efgh ijkl mnop`)
6. **Copy this password exactly** (it has spaces)

### Step 2: Configure .env File

Create a `.env` file in `/home/vboxuser/goldproject/`:

```bash
cat > /home/vboxuser/goldproject/.env << 'EOF'
GMAIL_EMAIL=whooshy123@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
EOF
```

**Replace `abcd efgh ijkl mnop` with your actual App Password from Step 1.**

Verify it worked:
```bash
cat /home/vboxuser/goldproject/.env
```

Should show:
```
GMAIL_EMAIL=whooshy123@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
```

---

## Start the Monitor

### Option A: Start Now (Test Mode)

Run one prediction cycle with email:
```bash
cd /home/vboxuser/goldproject
python3 monitor_trades.py --once
```

Check if email was sent:
```bash
tail monitor.log | grep -i "email"
```

### Option B: Start Continuous Background Job (Recommended)

Start the monitor to run every 1 hour indefinitely:

```bash
cd /home/vboxuser/goldproject
chmod +x start_monitor.sh
./start_monitor.sh
```

This will:
- Start `python3 monitor_trades.py` in the background
- Run every 1 hour automatically
- Send email after each prediction
- Log to `monitor_background.log`

**To follow logs live:**
```bash
tail -f monitor_background.log
```

**To stop the monitor:**
```bash
pkill -f "python3 monitor_trades.py"
```

**To restart:**
```bash
./start_monitor.sh
```

---

## Manual Trade Logging

After each trade you execute, add a line to `trade_backlog.txt`:

```
DATE, TRADE_ID, SIGNAL, ENTRY_PRICE, EXIT_PRICE, OUTCOME, R_MULTIPLE, NOTES
```

### Example:

```
2026-04-28, TRADE_001, LONG, 4720.0, 4850.0, WIN, 1.7, Hit first target on bounce. Volume confirmed.
2026-04-29, TRADE_002, SHORT, 4650.0, 4600.0, LOSS, -1.0, Got stopped out. False breakout.
```

**Format Details:**

| Field | Example | Notes |
|-------|---------|-------|
| DATE | 2026-04-28 | YYYY-MM-DD format |
| TRADE_ID | TRADE_001 | Unique ID (TRADE_002, TRADE_003, etc.) |
| SIGNAL | LONG | Original signal: LONG, SHORT, or NEUTRAL |
| ENTRY_PRICE | 4720.0 | Price you entered at |
| EXIT_PRICE | 4850.0 | Price you exited at (or 0.0 if still OPEN) |
| OUTCOME | WIN | WIN, LOSS, PARTIAL, or OPEN |
| R_MULTIPLE | 1.7 | Risk/reward ratio (negative if LOSS) |
| NOTES | Hit target on bounce | What worked/didn't, lessons learned (no commas) |

**When you log a trade:**
- It appears in the next hourly email
- Claude reads it and learns from your execution
- Helps the system improve predictions over time

See `trade_backlog.txt` for full format documentation.

---

## Email Content Example

When you receive an email, it will look like:

```
GOLD TRADING - HOURLY UPDATE
Generated: 2026-04-29 05:00:00 UTC

═════════════════════════════════════════════════════════════

🎯 CURRENT PREDICTION

Signal: LONG 📈
Confidence: MEDIUM
Gold Price: $4720.0

Entry Range: $4680.0 - $4720.0
Target 1: $4850.0
Target 2: $5100.0
Stop Loss: $4620.0

═════════════════════════════════════════════════════════════

📊 INTRADAY TECHNICALS

5-MIN:  4720.5 | RSI: 60.2 | SMA(9/21): 4719.1/4718.5
15-MIN: 4720.3 | RSI: 58.7 | SMA(9/21): 4718.9/4717.3
1-HOUR: 4720.1 | RSI: 55.3 | SMA(9/21): 4717.2/4715.8

═════════════════════════════════════════════════════════════

📈 TREND STATISTICS (Model Accuracy)

Overall Accuracy: 55.5%
Total Signals: 18
Correct Signals: 10

7-Day Accuracy: 57.14%
30-Day Accuracy: 50.0%

Signal Breakdown:
  LONG:    5 signals | 60.0% accuracy
  SHORT:   3 signals | 33.3% accuracy
  NEUTRAL: 10 signals | 60.0% accuracy

═════════════════════════════════════════════════════════════

💰 TOKEN USAGE & COSTS

Avg per Run: 15000 tokens ($0.050000)
Total Runs: 10
Total Cost So Far: $0.500

Projected:
  Daily:  $1.200
  Monthly: $36.00

═════════════════════════════════════════════════════════════

🏆 RECENT MANUAL TRADES

  • 2026-04-28, TRADE_001, LONG, 4720.0, 4850.0, WIN, 1.7, Hit target on bounce
  • 2026-04-29, TRADE_002, SHORT, 4650.0, 4600.0, LOSS, -1.0, Got stopped out

═════════════════════════════════════════════════════════════

NEXT UPDATE: In 1 hour
```

---

## Troubleshooting

### Email not sending?

**Check Gmail credentials:**
```bash
cat /home/vboxuser/goldproject/.env
```

Should show both GMAIL_EMAIL and GMAIL_APP_PASSWORD.

**Test email manually:**
```bash
cd /home/vboxuser/goldproject
python3 -c "from email_notifier import send_email; print(send_email('whooshy123@gmail.com'))"
```

### "Gmail credentials not configured" error?

Make sure `.env` file exists:
```bash
ls -la /home/vboxuser/goldproject/.env
```

If it doesn't exist, create it (see Step 2 above).

### Monitor not running?

Check if it's still active:
```bash
ps aux | grep "monitor_trades.py"
```

Check the background log:
```bash
tail -100 monitor_background.log
```

Restart:
```bash
pkill -f "monitor_trades.py"
sleep 2
./start_monitor.sh
```

### Gmail app password expired?

Gmail app passwords don't expire, but if there's an auth error:
1. Regenerate new app password (https://myaccount.google.com/apppasswords)
2. Update `.env` with the new password
3. Restart monitor: `./start_monitor.sh`

---

## Email Frequency

- **Default: Every 1 hour** (24 emails/day)
- To change interval, edit `monitor_trades.py` line ~10 or run:
  ```bash
  python3 monitor_trades.py 120  # Every 2 hours
  python3 monitor_trades.py 240  # Every 4 hours
  ```

---

## Cost

Gmail SMTP is **free**. No per-email charges.

API costs are from Claude predictions (~$0.05/run), tracked in `token_stats.json`.

---

## Next Steps

1. ✅ Create Gmail App Password
2. ✅ Create `.env` file with credentials
3. ✅ Run `./start_monitor.sh` to start background job
4. ✅ Check email inbox in ~1 minute for first update
5. ✅ Log trades in `trade_backlog.txt` after you trade
6. ✅ Monitor learns from your outcomes

Done! You'll now get hourly updates with predictions, costs, and trend stats.
