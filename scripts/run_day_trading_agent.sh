#!/bin/bash
# One scheduled entry point for the gold advisory. Do not also start
# day_trading_insights.py from another job at the same time.
cd "$(dirname "$0")/.."
exec python3 day_trading_insights.py --option agent --interval-minutes 5
