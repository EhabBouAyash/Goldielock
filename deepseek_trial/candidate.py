"""Shared candidate object for GOLD-OCT26-v1."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Candidate:
    candidate_id: str
    strategy: str
    side: str
    confirm_time: datetime
    confirm_m5: int
    structural_stop: float
    atr: float
    target_r: float
    min_rr: float
    reason: str
    extras: dict = field(default_factory=dict)
    h4_dir: str = "NEUTRAL"
