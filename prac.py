from fetch_intraday import *

intraday = fetch_intraday_bars(symbol="GC=F", intervals=["5m", "15m", "1h"])
print(intraday)
