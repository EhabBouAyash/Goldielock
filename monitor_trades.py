#!/usr/bin/env python3
"""
Continuous gold trading monitor.
Runs predictions at intervals, logs results, and maintains trend tracking.
"""

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from fetch_intraday import fetch_intraday_bars, save_intraday_data, get_intraday_summary
from token_tracker import update_token_stats, get_token_stats, log_token_usage
from email_notifier import send_email
from trade_plan_state import cycle_busy

MONITOR_LOG_PATH = Path(__file__).resolve().parent / "monitor.log"
TREND_DASHBOARD_PATH = Path(__file__).resolve().parent / "trend_dashboard.json"
GOLD_PROJECT_DIR = Path(__file__).resolve().parent

def log_monitor_event(event_type, message, details=None):
    """Log monitoring events to file and stdout."""
    timestamp = datetime.now().isoformat()
    log_entry = {
        "timestamp": timestamp,
        "type": event_type,
        "message": message,
        "details": details or {}
    }
    
    # Append to log file
    with open(MONITOR_LOG_PATH, 'a') as f:
        f.write(json.dumps(log_entry) + "\n")
    
    print(f"[{timestamp}] [{event_type}] {message}")
    if details:
        print(f"  Details: {details}")

def run_trading_prediction():
    """Execute generate_insights.py with trading option."""
    try:
        if cycle_busy():
            log_monitor_event(
                "PREDICTION_SKIPPED",
                "Another gold cycle is already running. This run published nothing.",
            )
            return {"status": "skipped"}
        log_monitor_event("PREDICTION_START", "Running trading predictions...")
        
        result = subprocess.run(
            ["python3", "day_trading_insights.py", "--option", "trading"],
            cwd=GOLD_PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=600,
        )
        
        if result.returncode == 0:
            log_monitor_event("PREDICTION_SUCCESS", "Trading predictions completed")
            return {
                "status": "success",
                "stdout": result.stdout[-500:] if result.stdout else ""  # Last 500 chars
            }
        else:
            log_monitor_event("PREDICTION_ERROR", f"Script failed with code {result.returncode}", {
                "stderr": result.stderr[-500:] if result.stderr else ""
            })
            return {
                "status": "error",
                "error": result.stderr[-500:] if result.stderr else "Unknown error"
            }
    except subprocess.TimeoutExpired:
        log_monitor_event("PREDICTION_TIMEOUT", "Prediction script exceeded 600s deadline")
        return {"status": "timeout"}
    except Exception as e:
        log_monitor_event("PREDICTION_EXCEPTION", str(e))
        return {"status": "exception", "error": str(e)}

def fetch_and_log_intraday():
    """Fetch intraday data and log to JSON."""
    try:
        log_monitor_event("INTRADAY_FETCH_START", "Fetching intraday bars...")
        data = fetch_intraday_bars()
        save_intraday_data(data)
        log_monitor_event("INTRADAY_FETCH_SUCCESS", "Intraday data updated")
        return data
    except Exception as e:
        log_monitor_event("INTRADAY_FETCH_ERROR", str(e))
        return None

def update_trend_dashboard():
    """Read signal_log and build trend statistics."""
    try:
        signal_csv = GOLD_PROJECT_DIR / "signal_log.csv"
        if not signal_csv.exists():
            log_monitor_event("DASHBOARD_ERROR", "signal_log.csv not found")
            return
        
        df = pd.read_csv(signal_csv)
        
        # Calculate rolling accuracies and win rates
        total_signals = len(df)
        correct_signals = (df['CORRECT'] == 1).sum()
        
        # By signal type
        long_signals = df[df['LONG'] == 1]
        short_signals = df[df['SHORT'] == 1]
        neutral_signals = df[df['NEUTRAL'] == 1]
        
        dashboard = {
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "population": "directional_comparison",
                "note": "These counts compare a later price with an old signal. They are not closed-trade results.",
                "total_signals": int(total_signals),
                "correct_signals": int(correct_signals),
                "overall_accuracy": round(correct_signals / max(total_signals, 1) * 100, 2),
            },
            "by_signal_type": {
                "long": {
                    "count": len(long_signals),
                    "correct": (long_signals['CORRECT'] == 1).sum(),
                    "accuracy": round((long_signals['CORRECT'] == 1).sum() / max(len(long_signals), 1) * 100, 2),
                    "avg_confidence": long_signals['CONFIDENCE'].fillna('').astype(str).value_counts().to_dict() if len(long_signals) > 0 else {}
                },
                "short": {
                    "count": len(short_signals),
                    "correct": (short_signals['CORRECT'] == 1).sum(),
                    "accuracy": round((short_signals['CORRECT'] == 1).sum() / max(len(short_signals), 1) * 100, 2),
                },
                "neutral": {
                    "count": len(neutral_signals),
                    "correct": (neutral_signals['CORRECT'] == 1).sum(),
                    "accuracy": round((neutral_signals['CORRECT'] == 1).sum() / max(len(neutral_signals), 1) * 100, 2),
                }
            },
            "time_windows": {
                "7_day": {
                    "accuracy": float(df.iloc[-1]['ACCURACY_7D']) if 'ACCURACY_7D' in df.columns and pd.notna(df.iloc[-1]['ACCURACY_7D']) else None,
                    "signals": len(df[df['DATE'] >= (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')]) if 'DATE' in df.columns else None
                },
                "30_day": {
                    "accuracy": float(df.iloc[-1]['ACCURACY_30D']) if 'ACCURACY_30D' in df.columns and pd.notna(df.iloc[-1]['ACCURACY_30D']) else None,
                    "signals": len(df[df['DATE'] >= (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')]) if 'DATE' in df.columns else None
                }
            }
        }
        
        with open(TREND_DASHBOARD_PATH, 'w') as f:
            json.dump(dashboard, f, indent=2, default=str)
        
        log_monitor_event("DASHBOARD_UPDATE", "Trend dashboard updated", dashboard['summary'])
        return dashboard
        
    except Exception as e:
        log_monitor_event("DASHBOARD_ERROR", str(e))
        return None

def continuous_monitor(interval_minutes=60, run_once=False):
    """
    Run monitoring loop at specified interval.
    
    Args:
        interval_minutes: How often to run predictions (default 15)
        run_once: If True, run once and exit. Otherwise run indefinitely.
    """
    log_monitor_event("MONITOR_START", f"Starting continuous monitor (interval: {interval_minutes}m)")
    
    iteration = 0
    while True:
        iteration += 1
        
        try:
            print(f"\n{'='*60}")
            print(f"ITERATION {iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print('='*60)
            
            # Step 1: Fetch intraday data
            fetch_and_log_intraday()
            
            # Step 2: Run trading predictions
            result = run_trading_prediction()
            
            # Step 3: Update trend dashboard
            update_trend_dashboard()
            
            # Step 4: Update token statistics
            token_stats = update_token_stats()
            if token_stats:
                log_monitor_event("TOKEN_STATS", "Token usage updated", token_stats)
            
            # Step 4: Show current status
            dashboard_path = TREND_DASHBOARD_PATH
            if dashboard_path.exists():
                with open(dashboard_path, 'r') as f:
                    dashboard = json.load(f)
                    print(f"\n📊 Current Accuracy: {dashboard['summary']['overall_accuracy']}%")
                    print(f"   7-day: {dashboard['time_windows']['7_day']['accuracy']}%")
                    print(f"   30-day: {dashboard['time_windows']['30_day']['accuracy']}%")
            
            # Step 5: Show token usage
            token_stats = get_token_stats()
            if token_stats:
                print(f"\n💰 Token Usage:")
                print(f"   Avg/Run: {token_stats['avg_tokens_per_run']:.0f} tokens (${token_stats['avg_cost_per_run']:.6f})")
                print(f"   Total Cost: ${token_stats['total_cost_usd']:.3f} ({token_stats['total_runs']} runs)")
                print(f"   Projected: ${token_stats['projected_daily_cost']:.3f}/day | ${token_stats['projected_monthly_cost']:.2f}/month")
            
            # Step 6: Send email notification
            try:
                email_result = send_email("whooshy123@gmail.com")
                if email_result['status'] == 'success':
                    log_monitor_event("EMAIL_SENT", "Prediction email sent", {"recipient": "whooshy123@gmail.com"})
                else:
                    log_monitor_event("EMAIL_ERROR", email_result['message'])
            except Exception as e:
                log_monitor_event("EMAIL_EXCEPTION", str(e))
            
            if run_once:
                log_monitor_event("MONITOR_SINGLE_RUN", "Single run completed, exiting")
                break
            
            # Wait for next interval
            sleep_seconds = interval_minutes * 60
            log_monitor_event("MONITOR_WAIT", f"Sleeping for {interval_minutes}m until next run")
            print(f"\nNext run in {interval_minutes} minutes...\n")
            time.sleep(sleep_seconds)
            
        except KeyboardInterrupt:
            log_monitor_event("MONITOR_INTERRUPTED", "Monitor interrupted by user")
            break
        except Exception as e:
            log_monitor_event("MONITOR_ERROR", f"Unexpected error: {str(e)}")
            if run_once:
                raise
            time.sleep(60)  # Sleep before retry

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        # Run predictions once and exit
        continuous_monitor(run_once=True)
    else:
        # Run continuously
        interval = 60  # Default 60 minutes (1 hour)
        if len(sys.argv) > 1:
            try:
                interval = int(sys.argv[1])
            except ValueError:
                pass
        
        continuous_monitor(interval_minutes=interval)
