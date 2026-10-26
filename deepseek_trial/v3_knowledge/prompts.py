"""Strategy cards for v3. DeepSeek proposes TRADE/SKIP; code owns size, risk, 5-fill cap."""

STRATEGY_A = (
    "Strategy A — H4 trend, M15 pullback and reclaim. Gold XAUUSD only. "
    "Windows 08:00-11:00 London and 08:00-11:00 New York, weekdays; no new entries at the end time. "
    "Long: last completed H4 UP (close > EMA20 > EMA50, EMA20 higher than 3 H4 ago). "
    "Arm when M15 closes back through EMA20 from above but stays above EMA50. "
    "Confirm within the next 4 M15 closes: close above EMA20 and above the prior M15 high. "
    "Stop under the pullback low minus a small ATR buffer. Target about 2.5R. "
    "Short: mirror. Max one idea per window in the original spec; this v3 book allows up to 5 fills/day."
)

STRATEGY_B = (
    "Strategy B — London overnight sweep and reclaim. "
    "Overnight box [00:00, 08:00) London; trade 08:00-10:30 London. "
    "Width should be roughly 0.5-1.5× the recent overnight widths. "
    "Long: H4 UP or NEUTRAL; sweep below the overnight low, then close back above the low and a short-term high K. "
    "Target toward the midpoint, at least 1.5R. Short: mirror above the high. "
    "Do not arm if the same candle takes out both high and low."
)

STRATEGY_C = (
    "Strategy C — New York 09:30-09:45 range, break, then a later retest. "
    "Entries 09:45-11:15 New York; breakout must appear before 10:45. "
    "Breakout: M5 close beyond the box with body at least 60% of the candle range — that only arms. "
    "Retest on a later candle, then target about 2.5R. Same-candle break-and-retest is invalid. "
    "No H4 filter required."
)
