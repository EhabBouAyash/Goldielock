"""API budget for v3 knowledge agents. Peak prices from GOLD-OCT26-v1 §9 (28 Sep 2026)."""
from __future__ import annotations

# Peak (conservative). Not a price guarantee.
PRO_IN = 1.32 / 1_000_000
PRO_OUT = 3.96 / 1_000_000
FLASH_IN = 0.30 / 1_000_000
FLASH_OUT = 1.20 / 1_000_000

# v3 snapshot + strategy card. Conservative vs v1 audit (~1800 in).
INPUT_TOKENS = 3_500
# Observed deepseek-v4-pro with reasoning: ~1.8k–5.3k completion (mostly reasoning).
OUTPUT_THINKING = 3_500
# JSON-only, thinking disabled.
OUTPUT_NO_THINK = 400

DAYS = 30
TRADES_PER_DAY = 5
CALLS_IF_MAX_TRADES = DAYS * TRADES_PER_DAY  # 150 — one call per fill
# Engine also looks without filling (SKIP). Cap 8 looks/day so a dry day is not 24+ calls.
LOOKS_PER_DAY = 8
CALLS_IF_LOOK_CAP = DAYS * LOOKS_PER_DAY  # 240


def usd(n_calls: int, in_tok: int, out_tok: int, in_px: float, out_px: float) -> float:
    return n_calls * (in_tok * in_px + out_tok * out_px)


def budget() -> dict:
    rows = []
    for label, n in (
        ("5 fills/day only (150 calls)", CALLS_IF_MAX_TRADES),
        ("8 looks/day cap, 30 days (240 calls)", CALLS_IF_LOOK_CAP),
    ):
        rows.append({
            "scenario": label,
            "calls": n,
            "pro_thinking_usd": round(usd(n, INPUT_TOKENS, OUTPUT_THINKING, PRO_IN, PRO_OUT), 2),
            "pro_no_think_usd": round(usd(n, INPUT_TOKENS, OUTPUT_NO_THINK, PRO_IN, PRO_OUT), 2),
            "flash_thinking_usd": round(usd(n, INPUT_TOKENS, OUTPUT_THINKING, FLASH_IN, FLASH_OUT), 2),
            "flash_no_think_usd": round(usd(n, INPUT_TOKENS, OUTPUT_NO_THINK, FLASH_IN, FLASH_OUT), 2),
        })
    per_agent_max_trades_pro_think = rows[0]["pro_thinking_usd"]
    return {
        "assumption": (
            f"{TRADES_PER_DAY} fills/day × {DAYS} days = {CALLS_IF_MAX_TRADES} API calls "
            "if DeepSeek is invoked only when a trade is placed. "
            f"This package looks up to {LOOKS_PER_DAY}/day (SKIP or TRADE), so budget the 240-call line "
            "unless you only bill fills."
        ),
        "tokens_per_call": {
            "input": INPUT_TOKENS,
            "output_thinking": OUTPUT_THINKING,
            "output_no_think": OUTPUT_NO_THINK,
        },
        "prices_per_million_peak": {
            "deepseek-v4-pro": {"input": 1.32, "output": 3.96},
            "deepseek-flash": {"input": 0.30, "output": 1.20},
        },
        "per_agent_30d": rows,
        "three_agents_30d": {
            "if_each_hits_5_fills_day_pro_thinking": round(3 * per_agent_max_trades_pro_think, 2),
            "if_each_uses_8_looks_day_pro_thinking": round(3 * rows[1]["pro_thinking_usd"], 2),
            "if_each_hits_5_fills_day_pro_no_think": round(3 * rows[0]["pro_no_think_usd"], 2),
        },
        "note": (
            "v3 uses deepseek-v4-pro with thinking disabled when the API allows it. "
            "If the model still bills reasoning tokens, use the thinking column. "
            "This is API cost only, not spread/commission/P&L."
        ),
    }
