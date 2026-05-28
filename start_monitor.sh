#!/bin/bash
# Start continuous gold trading monitor in background
# Runs every 1 hour, sends email updates to whooshy123@gmail.com

cd /home/vboxuser/goldproject

# Check if already running
if pgrep -f "python3 monitor_trades.py" > /dev/null; then
    echo "Monitor already running. PID: $(pgrep -f 'python3 monitor_trades.py')"
    exit 1
fi

# Start in background and log output
nohup python3 monitor_trades.py > monitor_background.log 2>&1 &

PID=$!
echo "Monitor started with PID: $PID"
echo "Logging to: monitor_background.log"
echo "To stop: kill $PID"
echo ""
echo "Monitor will:"
echo "  • Run every 1 hour"
echo "  • Fetch intraday data (5m/15m/1h)"
echo "  • Generate gold predictions"
echo "  • Send email to whooshy123@gmail.com"
echo "  • Track token usage & costs"
echo ""
echo "To follow logs live:"
echo "  tail -f monitor_background.log"
