"""
Email notification system for gold trading predictions.
Sends formatted hourly updates with predictions, costs, trends, and trade outcomes.
"""

import json
import smtplib
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SIGNAL_LOG_PATH = Path(__file__).resolve().parent / "signal_log.csv"
TRADE_BACKLOG_PATH = Path(__file__).resolve().parent / "trade_backlog.txt"
TREND_DASHBOARD_PATH = Path(__file__).resolve().parent / "trend_dashboard.json"
TOKEN_STATS_PATH = Path(__file__).resolve().parent / "token_stats.json"
INTRADAY_DATA_PATH = Path(__file__).resolve().parent / "intraday_data.json"

def read_latest_signal():
    """Read the latest signal from signal_log.csv"""
    try:
        with open(SIGNAL_LOG_PATH, 'r') as f:
            lines = f.readlines()
            if len(lines) < 2:
                return None
            last_line = lines[-1].strip()
            headers = lines[0].strip().split(',')
            values = last_line.split(',')
            signal_dict = dict(zip(headers, values))
            return signal_dict
    except Exception as e:
        return {"error": str(e)}

def read_trend_stats():
    """Read trend statistics from dashboard"""
    try:
        if TREND_DASHBOARD_PATH.exists():
            with open(TREND_DASHBOARD_PATH, 'r') as f:
                return json.load(f)
    except Exception as e:
        return {"error": str(e)}
    return None

def read_token_stats():
    """Read token usage statistics"""
    try:
        if TOKEN_STATS_PATH.exists():
            with open(TOKEN_STATS_PATH, 'r') as f:
                return json.load(f)
    except Exception as e:
        return {"error": str(e)}
    return None

def read_intraday_data():
    """Read latest intraday technicals"""
    try:
        if INTRADAY_DATA_PATH.exists():
            with open(INTRADAY_DATA_PATH, 'r') as f:
                return json.load(f)
    except Exception as e:
        return {"error": str(e)}
    return None

def read_trade_backlog():
    """Read manual trades from trade_backlog.txt"""
    trades = []
    try:
        if TRADE_BACKLOG_PATH.exists():
            with open(TRADE_BACKLOG_PATH, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    trades.append(line)
            return trades[-5:] if trades else []
    except Exception as e:
        return [f"Error reading trades: {e}"]
    return []

def safe_get(d, *keys, default="N/A"):
    """Safely navigate nested dicts"""
    if d is None:
        return default
    for key in keys:
        if isinstance(d, dict):
            d = d.get(key)
        else:
            return default
    return d if d is not None else default

def format_email_body():
    """Build the formatted email body with all data"""
    
    signal = read_latest_signal() or {}
    trends = read_trend_stats() or {}
    tokens = read_token_stats() or {}
    intraday = read_intraday_data() or {}
    trades = read_trade_backlog() or []
    
    # Signal info
    signal_type = "LONG 📈" if signal.get('LONG') == '1' else ("SHORT 📉" if signal.get('SHORT') == '1' else "NEUTRAL ➡️")
    gold_price = safe_get(signal, 'GOLD_PRICE', default='N/A')
    confidence = safe_get(signal, 'CONFIDENCE', default='N/A')
    entry_low = safe_get(signal, 'ENTRY_LOW', default='N/A')
    entry_high = safe_get(signal, 'ENTRY_HIGH', default='N/A')
    target1 = safe_get(signal, 'TARGET1', default='N/A')
    target2 = safe_get(signal, 'TARGET2', default='N/A')
    stop_loss = safe_get(signal, 'STOP_LOSS', default='N/A')
    
    # Intraday data
    price_5m = safe_get(intraday, 'intervals', '5m', 'current_price', default='N/A')
    rsi_5m = safe_get(intraday, 'intervals', '5m', 'rsi', default='N/A')
    sma9_5m = safe_get(intraday, 'intervals', '5m', 'sma_9', default='N/A')
    sma21_5m = safe_get(intraday, 'intervals', '5m', 'sma_21', default='N/A')
    
    price_15m = safe_get(intraday, 'intervals', '15m', 'current_price', default='N/A')
    rsi_15m = safe_get(intraday, 'intervals', '15m', 'rsi', default='N/A')
    sma9_15m = safe_get(intraday, 'intervals', '15m', 'sma_9', default='N/A')
    sma21_15m = safe_get(intraday, 'intervals', '15m', 'sma_21', default='N/A')
    
    price_1h = safe_get(intraday, 'intervals', '1h', 'current_price', default='N/A')
    rsi_1h = safe_get(intraday, 'intervals', '1h', 'rsi', default='N/A')
    sma9_1h = safe_get(intraday, 'intervals', '1h', 'sma_9', default='N/A')
    sma21_1h = safe_get(intraday, 'intervals', '1h', 'sma_21', default='N/A')
    
    # Trends
    overall_acc = safe_get(trends, 'summary', 'overall_accuracy', default='N/A')
    total_sigs = safe_get(trends, 'summary', 'total_signals', default='N/A')
    correct_sigs = safe_get(trends, 'summary', 'correct_signals', default='N/A')
    acc_7d = safe_get(trends, 'time_windows', '7_day', 'accuracy', default='N/A')
    acc_30d = safe_get(trends, 'time_windows', '30_day', 'accuracy', default='N/A')
    long_count = safe_get(trends, 'by_signal_type', 'long', 'count', default='N/A')
    long_acc = safe_get(trends, 'by_signal_type', 'long', 'accuracy', default='N/A')
    short_count = safe_get(trends, 'by_signal_type', 'short', 'count', default='N/A')
    short_acc = safe_get(trends, 'by_signal_type', 'short', 'accuracy', default='N/A')
    neutral_count = safe_get(trends, 'by_signal_type', 'neutral', 'count', default='N/A')
    neutral_acc = safe_get(trends, 'by_signal_type', 'neutral', 'accuracy', default='N/A')
    
    # Tokens
    token_runs = safe_get(tokens, 'total_runs', default=0)
    if token_runs > 0:
        avg_tokens = safe_get(tokens, 'avg_tokens_per_run', default=0)
        avg_cost = safe_get(tokens, 'avg_cost_per_run', default=0)
        total_cost = safe_get(tokens, 'total_cost_usd', default=0)
        daily_proj = safe_get(tokens, 'projected_daily_cost', default=0)
        monthly_proj = safe_get(tokens, 'projected_monthly_cost', default=0)
        token_section = f"""✓ Tracking enabled
Avg per Run: {avg_tokens:.0f} tokens (${avg_cost:.6f})
Total Runs: {token_runs}
Total Cost So Far: ${total_cost:.3f}

Projected:
  Daily:  ${daily_proj:.3f}
  Monthly: ${monthly_proj:.2f}"""
    else:
        token_section = "⏳ First run in progress (no stats yet)"
    
    body = f"""
GOLD TRADING - HOURLY UPDATE
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

═════════════════════════════════════════════════════════════

🎯 CURRENT PREDICTION

Signal: {signal_type}
Confidence: {confidence}
Gold Price: ${gold_price}

Entry Range: ${entry_low} - ${entry_high}
Target 1: ${target1}
Target 2: ${target2}
Stop Loss: {stop_loss}

═════════════════════════════════════════════════════════════

📊 INTRADAY TECHNICALS

5-MIN:  {price_5m} | RSI: {rsi_5m} | SMA(9/21): {sma9_5m}/{sma21_5m}
15-MIN: {price_15m} | RSI: {rsi_15m} | SMA(9/21): {sma9_15m}/{sma21_15m}
1-HOUR: {price_1h} | RSI: {rsi_1h} | SMA(9/21): {sma9_1h}/{sma21_1h}

═════════════════════════════════════════════════════════════

📈 TREND STATISTICS (Model Accuracy)

Overall Accuracy: {overall_acc}%
Total Signals: {total_sigs}
Correct Signals: {correct_sigs}

7-Day Accuracy: {acc_7d}%
30-Day Accuracy: {acc_30d}%

Signal Breakdown:
  LONG:    {long_count} signals | {long_acc}% accuracy
  SHORT:   {short_count} signals | {short_acc}% accuracy
  NEUTRAL: {neutral_count} signals | {neutral_acc}% accuracy

═════════════════════════════════════════════════════════════

💰 TOKEN USAGE & COSTS

{token_section}

═════════════════════════════════════════════════════════════

🏆 RECENT MANUAL TRADES

{chr(10).join(f'  • {trade}' for trade in trades) if trades else '  (No trades logged yet)'}

═════════════════════════════════════════════════════════════

NEXT UPDATE: In 1 hour

Questions? Check /goldproject/REALTIME_SETUP.md or EMAIL_SETUP.md
"""
    
    return body

def send_email(recipient_email, subject="Gold Trading Update", body=None):
    """Send email notification"""
    import os
    
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        return {"status": "error", "message": "python-dotenv not installed. Run: pip3 install --break-system-packages python-dotenv"}
    
    if body is None:
        body = format_email_body()
    
    # Get credentials from .env
    sender_email = os.getenv("GMAIL_EMAIL", "").strip()
    sender_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    
    if not sender_email or not sender_password:
        return {"status": "error", "message": "Gmail credentials not configured. Create .env with GMAIL_EMAIL and GMAIL_APP_PASSWORD. See EMAIL_SETUP.md"}
    
    try:
        # Gmail SMTP server
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        
        # Create message
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        # Send
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        
        return {"status": "success", "message": f"Email sent to {recipient_email}"}
    
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    body = format_email_body()
    print(body)
    print("\n" + "="*60)
    print("To send this email, call:")
    print("send_email('whooshy123@gmail.com')")
