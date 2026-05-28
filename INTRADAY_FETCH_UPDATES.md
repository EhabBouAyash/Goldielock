# Intraday Data Fetch Updates

## Problem Resolved

**Error encountered:**
```
Failed download: ['GC=F']: possibly delisted; no price data found (period=5d)
```

**Root cause:** yfinance occasionally returns empty or fails on first attempt due to:
- Transient network issues
- Rate limiting from yfinance/Yahoo API
- API temporary unavailability
- Stale cache

**Solution:** Added retry logic with exponential backoff + fallback to cached data.

---

## Changes to `fetch_intraday.py`

### 1. Retry Logic (Exponential Backoff)

```python
def fetch_intraday_bars(symbol="GC=F", intervals=["5m", "15m", "1h"], 
                        retries=3, backoff_base=2):
```

**Behavior:**
- Attempt 1: Immediate (0 seconds wait)
- Attempt 2: Wait 2^1 = 2 seconds, then retry
- Attempt 3: Wait 2^2 = 4 seconds, then retry
- If all fail: Return error with attempt count

**Example:**
```
[5M] ERROR (attempt 1/3): No data returned
→ Wait 2 seconds
[5M] ERROR (attempt 2/3): Connection timeout
→ Wait 4 seconds
[5M] ERROR (attempt 3/3): Final attempt failed
→ Fall back to cached data
```

### 2. Cached Data Fallback

`get_intraday_summary()` now:
1. Tries to fetch fresh data (with retries)
2. If ALL intervals fail, loads `intraday_data.json` (cached from last successful run)
3. Marks data as fallback: `"note": "Using cached data (fetch failed)"`
4. Sends cached data to Claude instead of failing

**Benefit:** 
- System doesn't crash on transient API issues
- Uses last-known-good data
- Alerts user that data is stale

### 3. Better Error Messages

Now shows:
- **With attempt info:** `ERROR (attempt 3/3): yfinance network timeout`
- **Fallback note:** `[NOTE] Using cached data (fetch failed)`
- **Bars loaded:** `Bars=50` shows how many candles were fetched

---

## Testing Results

✅ Ran test after update: Fetch succeeded with 50 bars per interval

```
[5M]  Price=4646.8, SMA(9)=4650.52, RSI=61.95, Bars=50
[15M] Price=4647.1, SMA(9)=4650.99, RSI=54.25, Bars=50
[1H]  Price=4646.6, SMA(9)=4619.59, RSI=59.86, Bars=50
```

---

## How It Affects the Monitor

When `monitor_trades.py` runs `day_trading_insights.py`:

**Before (if fetch fails):**
- Error printed to stderr
- Analysis still runs but with no intraday data
- Claude says "Error: No data" for chart patterns

**After (with retry logic):**
- Retries 3 times with exponential backoff (max 6 seconds)
- If all fail, uses cached `intraday_data.json` from last successful run
- Claude gets intraday data (fresh or cached)
- Prints `[NOTE] Using cached data...` to alert you it's stale

---

## Configuration

If you want to adjust retry behavior:

```python
# In day_trading_insights.py or any script calling fetch_intraday_bars():

# More aggressive retries (4 attempts, wait longer)
data = fetch_intraday_bars(retries=4, backoff_base=2)

# Fast retries (2 attempts, wait shorter)
data = fetch_intraday_bars(retries=2, backoff_base=1.5)
```

Default is good for most cases: **3 retries, 2x backoff = 0s, 2s, 4s total**

---

## No Changes Needed

- ✅ `day_trading_insights.py` — Already uses new retry logic
- ✅ `monitor_trades.py` — No changes needed
- ✅ `email_notifier.py` — Works with cached data
- ✅ All other scripts — Unaffected

---

## What's Cached

**File:** `intraday_data.json`

Contains:
```json
{
  "timestamp": "2026-04-29T08:16:00.123456",
  "symbol": "GC=F",
  "intervals": {
    "5m": { "current_price": 4646.8, "sma_9": 4650.52, "rsi": 61.95, ... },
    "15m": { ... },
    "1h": { ... }
  }
}
```

- **Updated:** Every time fetch completes (success or fallback)
- **Stale after:** 1+ hour without successful fetch (not ideal but acceptable)
- **Used when:** All retries fail

---

## Monitoring Tips

If you see `[NOTE] Using cached data` in the analysis:
1. Data is 1+ hours old
2. yfinance is having issues (temporary)
3. Monitor will retry next run (in 1 hour)
4. If persists > 2 cycles, check yfinance/network connectivity

Check network:
```bash
# Quick test
python3 -c "import yfinance as yf; print(yf.download('GC=F', period='1d', interval='1h'))"
```

---

## Summary

✅ **Error handling:** Transient fetch failures no longer crash the system
✅ **Retry logic:** 3 attempts with exponential backoff (0s, 2s, 4s)
✅ **Fallback:** Uses cached data if all retries fail
✅ **Transparency:** Alerts user when using stale data
✅ **No disruption:** All other scripts work unchanged

System is now more resilient. 🛡️
