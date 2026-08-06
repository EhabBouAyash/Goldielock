"""
Token usage tracking for gold trading insights pipeline.
Estimates input tokens and tracks actual output tokens from Claude API.
"""

import json
from pathlib import Path
from datetime import datetime

TOKEN_LOG_PATH = Path(__file__).resolve().parent / "token_usage.jsonl"
TOKEN_STATS_PATH = Path(__file__).resolve().parent / "token_stats.json"

def log_token_usage(event_type, input_tokens=None, output_tokens=None, total_tokens=None, cost_usd=None, details=None):
    """
    Log token usage event.
    
    Args:
        event_type: "estimate" (before run) or "actual" (from API response)
        input_tokens: Input tokens used
        output_tokens: Output tokens used
        total_tokens: Total tokens used
        cost_usd: Estimated cost in USD
        details: Any additional details
    """
    entry = {
        "timestamp": datetime.now().isoformat(),
        "type": event_type,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "details": details or {}
    }
    
    # Append to log file
    with open(TOKEN_LOG_PATH, 'a') as f:
        f.write(json.dumps(entry) + "\n")

def calculate_cost(input_tokens, output_tokens, model="claude-3-5-sonnet"):
    """Calculate estimated cost in USD."""
    # Pricing per 1M tokens (thinking tokens are billed as output).
    rates = {
        "claude-3-5-sonnet": (3.0, 15.0),
        "claude-opus-5": (5.0, 25.0),
        "claude-opus-4-8": (5.0, 25.0),
    }
    input_rate, output_rate = rates.get(model, (0.0, 0.0))
    input_cost = input_tokens * (input_rate / 1_000_000)
    output_cost = output_tokens * (output_rate / 1_000_000)
    return round(input_cost + output_cost, 6)

def estimate_prompt_tokens(data_dict):
    """
    Rough estimate of tokens in the prompt data.
    Uses ~4 characters per token as a heuristic.
    """
    # Convert dict to JSON string to estimate
    json_str = json.dumps(data_dict)
    # Rough estimate: ~4 chars = 1 token
    estimated_tokens = len(json_str) // 4
    return estimated_tokens

def update_token_stats():
    """Calculate running token statistics from log file."""
    if not TOKEN_LOG_PATH.exists():
        return None
    
    total_input = 0
    total_output = 0
    total_runs = 0
    all_costs = []
    
    with open(TOKEN_LOG_PATH, 'r') as f:
        for line in f:
            entry = json.loads(line)
            if entry['type'] == 'actual':
                total_input += entry.get('input_tokens', 0) or 0
                total_output += entry.get('output_tokens', 0) or 0
                total_runs += 1
                if entry.get('cost_usd'):
                    all_costs.append(entry['cost_usd'])
    
    if total_runs == 0:
        return None
    
    avg_input = total_input / total_runs
    avg_output = total_output / total_runs
    avg_cost = sum(all_costs) / len(all_costs) if all_costs else 0
    total_cost = sum(all_costs)
    
    # Project monthly cost
    runs_per_day = 24  # 1 per hour
    projected_monthly = total_cost * runs_per_day * 30 if total_runs > 0 else 0
    
    stats = {
        "timestamp": datetime.now().isoformat(),
        "total_runs": total_runs,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "avg_input_tokens_per_run": round(avg_input, 0),
        "avg_output_tokens_per_run": round(avg_output, 0),
        "avg_tokens_per_run": round(avg_input + avg_output, 0),
        "avg_cost_per_run": round(avg_cost, 6),
        "total_cost_usd": round(total_cost, 3),
        "projected_daily_cost": round(avg_cost * 24, 3),
        "projected_monthly_cost": round(projected_monthly, 2)
    }
    
    with open(TOKEN_STATS_PATH, 'w') as f:
        json.dump(stats, f, indent=2, default=str)
    
    return stats

def get_token_stats():
    """Load current token statistics."""
    if TOKEN_STATS_PATH.exists():
        with open(TOKEN_STATS_PATH, 'r') as f:
            return json.load(f)
    return None

if __name__ == "__main__":
    # Example usage
    stats = update_token_stats()
    if stats:
        print("Token Statistics:")
        print(f"  Total Runs: {stats['total_runs']}")
        print(f"  Avg Tokens/Run: {stats['avg_tokens_per_run']}")
        print(f"  Avg Cost/Run: ${stats['avg_cost_per_run']}")
        print(f"  Projected Cost/Month: ${stats['projected_monthly_cost']}")
