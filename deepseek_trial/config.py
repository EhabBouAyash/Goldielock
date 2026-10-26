"""GOLD-OCT26-v1 frozen settings. Paper only. No funded-account connection."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
PACKAGE_DIR = Path(__file__).resolve().parent

load_dotenv(ENV_PATH)

SPEC_VERSION = "GOLD-OCT26-v1"
MODEL_NAME = "deepseek-v4-pro"
V3_MODEL_NAME = "deepseek-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
# Spec live deadline is 8s (late = PASS). Replay audit uses a longer HTTP
# wait so the model can finish; a live loop must still treat >8s as PASS.
DEEPSEEK_LIVE_DEADLINE_SEC = 8.0
DEEPSEEK_TIMEOUT_SEC = 45.0

STARTING_BALANCE = 25_000.0
RISK_PER_TRADE = 125.0
DAILY_STOP = 375.0
TRIAL_DRAWDOWN = 1_500.0
MARGIN_CAP_FRAC = 0.25

DECISION_DELAY_SEC = 10.0
MAX_QUOTE_AGE_SEC = 2.0
CHASE_ATR_FRAC = 0.20
STOP_FLOOR_ATR = 0.80
STOP_CAP_ATR = 2.50
FRICTION_MAX_FRAC = 0.15
COMMISSION_PER_SIDE = 0.000016
SLIPPAGE_SPREAD_FRAC = 0.25

H4_WARMUP = 300
EMA_FAST = 20
EMA_SLOW = 50
ATR_N = 14

GOLD_TICK = 0.01
GOLD_CONTRACT = 100.0
GOLD_VOLUME_STEP = 0.01
GOLD_MIN_VOLUME = 0.01
GOLD_SPREAD_BPS = 1.6
GOLD_LEVERAGE = 25.0


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
        "spec": SPEC_VERSION,
    }


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    tick: float
    contract_size: float
    volume_step: float
    min_volume: float
    spread_bps: float
    leverage: float


XAUUSD = SymbolSpec(
    name="XAUUSD",
    tick=GOLD_TICK,
    contract_size=GOLD_CONTRACT,
    volume_step=GOLD_VOLUME_STEP,
    min_volume=GOLD_MIN_VOLUME,
    spread_bps=GOLD_SPREAD_BPS,
    leverage=GOLD_LEVERAGE,
)
