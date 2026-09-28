"""Frozen trial settings. Loads DEEPSEEK_API_KEY from the repo .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
PACKAGE_DIR = Path(__file__).resolve().parent

load_dotenv(ENV_PATH)

SPEC_VERSION = "2.0"
MODEL_NAME = "deepseek-v4-pro"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_TIMEOUT_SEC = 15.0

STARTING_BALANCE = 25_000.0
RISK_PER_TRADE = 62.50
AGGREGATE_RISK_CAP = 125.0
DAILY_STOP = 250.0
TRIAL_EQUITY_FLOOR = 24_000.0
TRIAL_DRAWDOWN_CAP = 1_000.0
MARGIN_CAP_FRAC = 0.25
HEADROOM = 125.0
FIRM_DAILY_LOSS = 1_250.0
FIRM_MAX_LOSS = 2_500.0
FIRM_EQUITY_FLOOR = 22_500.0

SIGNAL_VALIDITY_SEC = 60
MAX_QUOTE_AGE_SEC = 2
SPREAD_FRAC_OF_STOP = 0.10
COST_FRAC_OF_RISK = 0.10
ENTRY_BLOCK_START_HOUR = 0
ENTRY_BLOCK_END_HOUR = 2
NEWS_BUFFER_MIN = 10
WEEKEND_FLATTEN_MINUTES = 15
NO_ENTRY_BEFORE_WEEKEND_MINUTES = 60

GOLD_SPREAD_BPS = 1.6
GOLD_SLIPPAGE = 0.10
GOLD_TICK = 0.01
GOLD_CONTRACT = 100.0
GOLD_VOLUME_STEP = 0.01
GOLD_MIN_VOLUME = 0.01

BTC_SPREAD_BPS = 5.0
BTC_SLIPPAGE = 1.0
BTC_TICK = 0.01
BTC_CONTRACT = 1.0
BTC_VOLUME_STEP = 0.01
BTC_MIN_VOLUME = 0.01
BTC_LEVERAGE = 1.0


def deepseek_api_key() -> str:
    return (os.getenv("DEEPSEEK_API_KEY") or "").strip()


def env_status() -> dict:
    key = deepseek_api_key()
    return {
        "env_path": str(ENV_PATH),
        "env_exists": ENV_PATH.is_file(),
        "key_present": bool(key),
        "key_len": len(key),
        "key_tail": key[-4:] if len(key) >= 4 else "",
        "model": MODEL_NAME,
    }


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    tick: float
    contract_size: float
    volume_step: float
    min_volume: float
    spread_bps: float
    slippage: float
    leverage: float = 25.0


XAUUSD = SymbolSpec(
    name="XAUUSD",
    tick=GOLD_TICK,
    contract_size=GOLD_CONTRACT,
    volume_step=GOLD_VOLUME_STEP,
    min_volume=GOLD_MIN_VOLUME,
    spread_bps=GOLD_SPREAD_BPS,
    slippage=GOLD_SLIPPAGE,
    leverage=25.0,
)

BTCUSD = SymbolSpec(
    name="BTCUSD",
    tick=BTC_TICK,
    contract_size=BTC_CONTRACT,
    volume_step=BTC_VOLUME_STEP,
    min_volume=BTC_MIN_VOLUME,
    spread_bps=BTC_SPREAD_BPS,
    slippage=BTC_SLIPPAGE,
    leverage=BTC_LEVERAGE,
)
