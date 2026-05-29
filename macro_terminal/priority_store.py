"""Tiered priority store (JSON-backed, not a LangGraph graph)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from macro_terminal.classifier import normalize_title
from macro_terminal.models import TerminalItem

TIERS = ("P1", "P2", "P3", "expired")
ACTIVE_TIERS = ("P1", "P2", "P3")
EXPIRE_HOURS = 4
TIER_LIMITS = {"P1": 50, "P2": 100, "P3": 100, "expired": 300}
TIER_RANK = {"P1": 0, "P2": 1, "P3": 2, "expired": 3}

NOISE_PREFIXES = ("News scout failed for query:", "GNews API key missing")


def empty_store() -> dict[str, list[dict[str, Any]]]:
    return {"P1": [], "P2": [], "P3": [], "expired": []}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _item_age_hours(item: dict[str, Any], now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    for key in ("published_at", "ingested_at"):
        ts = _parse_ts(item.get(key))
        if ts:
            return max(0.0, (now - ts).total_seconds() / 3600)
    return 0.0


def _is_noise(item: dict[str, Any]) -> bool:
    title = (item.get("title") or "").strip()
    return any(title.startswith(prefix) for prefix in NOISE_PREFIXES)


class MacroPriorityStore:
    """
    Persistent tiered store:
      P1 — immediate market movers (active FOMC, hot war escalation, surprise CB move)
      P2 — high relevance (Fed speakers, CPI beat/miss, sanctions)
      P3 — background context (COT positioning, slow geopolitical drift)
      expired — >4 hours old, excluded from Haiku calls
    """

    def __init__(self, data: dict[str, list[dict[str, Any]]] | None = None):
        self.data = empty_store()
        if data:
            for tier in TIERS:
                bucket = data.get(tier, [])
                self.data[tier] = bucket if isinstance(bucket, list) else []

    def __len__(self) -> int:
        return sum(len(self.data[tier]) for tier in ACTIVE_TIERS)

    @property
    def expired_count(self) -> int:
        return len(self.data["expired"])

    def counts(self) -> dict[str, int]:
        return {tier: len(self.data[tier]) for tier in TIERS}

    def ingest_many(self, items: list[TerminalItem]) -> int:
        added = 0
        for item in items:
            if self.ingest(item):
                added += 1
        return added

    def ingest(self, item: TerminalItem) -> bool:
        payload = item.to_dict()
        if _is_noise(payload):
            return False

        tier = payload.get("priority_band", "P3")
        if tier not in ACTIVE_TIERS:
            tier = "P3"
        payload["priority_band"] = tier

        existing_tier, existing_idx = self._find_item(payload)
        if existing_tier is not None:
            return self._upgrade_or_refresh(existing_tier, existing_idx, payload, tier)

        self.data[tier].append(payload)
        self._sort_tier(tier)
        self._trim_tier(tier)
        return True

    def expire_stale(self, *, hours: float = EXPIRE_HOURS) -> int:
        """Move items older than `hours` from active tiers into expired."""
        moved = 0
        now = datetime.now(timezone.utc)
        for tier in ACTIVE_TIERS:
            kept: list[dict[str, Any]] = []
            for item in self.data[tier]:
                if _item_age_hours(item, now) > hours:
                    item = dict(item)
                    item["expired_at"] = now.isoformat()
                    item["priority_band"] = "expired"
                    self.data["expired"].append(item)
                    moved += 1
                else:
                    kept.append(item)
            self.data[tier] = kept
        self._trim_tier("expired")
        return moved

    def prune_noise(self) -> int:
        removed = 0
        for tier in TIERS:
            before = len(self.data[tier])
            self.data[tier] = [item for item in self.data[tier] if not _is_noise(item)]
            removed += before - len(self.data[tier])
        return removed

    def active_items(self) -> list[dict[str, Any]]:
        """P1 → P2 → P3, score-sorted within each tier (for terminal display)."""
        rows: list[dict[str, Any]] = []
        for tier in ACTIVE_TIERS:
            rows.extend(self.data[tier])
        return rows

    def for_haiku(self, *, max_items: int = 40) -> list[dict[str, Any]]:
        """Active tiers only — expired bucket is excluded from downstream Haiku calls."""
        return self.active_items()[:max_items]

    def top(self, n: int = 15) -> list[dict[str, Any]]:
        return self.active_items()[:n]

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {tier: list(self.data[tier]) for tier in TIERS}

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "MacroPriorityStore":
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()

        if isinstance(payload, dict) and "P1" in payload:
            return cls(payload)

        # Migrate legacy heap queue format: {"queue_depth": N, "items": [...]}
        store = cls()
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            for raw in payload["items"]:
                if not isinstance(raw, dict):
                    continue
                band = raw.get("priority_band", "P3")
                if band == "P0":
                    band = "P1"
                if band not in TIERS:
                    band = "P3"
                raw = dict(raw)
                raw["priority_band"] = band
                if band == "expired":
                    store.data["expired"].append(raw)
                elif band in ACTIVE_TIERS:
                    store.data[band].append(raw)
            for tier in TIERS:
                store._sort_tier(tier)
        return store

    def _find_item(self, payload: dict[str, Any]) -> tuple[str | None, int | None]:
        item_id = payload.get("item_id")
        title_key = normalize_title(payload.get("title", ""))
        for tier in TIERS:
            for idx, existing in enumerate(self.data[tier]):
                if item_id and existing.get("item_id") == item_id:
                    return tier, idx
                if title_key and normalize_title(existing.get("title", "")) == title_key:
                    return tier, idx
        return None, None

    def _upgrade_or_refresh(
        self,
        existing_tier: str,
        existing_idx: int,
        payload: dict[str, Any],
        new_tier: str,
    ) -> bool:
        existing = self.data[existing_tier][existing_idx]
        existing_rank = TIER_RANK.get(existing_tier, 9)
        new_rank = TIER_RANK.get(new_tier, 9)

        merged = dict(existing)
        merged.update(payload)
        if new_rank < existing_rank and existing_tier in ACTIVE_TIERS:
            self.data[existing_tier].pop(existing_idx)
            merged["priority_band"] = new_tier
            self.data[new_tier].append(merged)
            self._sort_tier(new_tier)
            self._trim_tier(new_tier)
            return False

        merged["priority_band"] = existing_tier if existing_tier in ACTIVE_TIERS else new_tier
        if existing_tier == "expired":
            return False
        self.data[existing_tier][existing_idx] = merged
        self._sort_tier(existing_tier)
        return False

    def _sort_tier(self, tier: str) -> None:
        self.data[tier].sort(
            key=lambda item: (
                -float(item.get("priority_score") or 0),
                str(item.get("published_at") or item.get("ingested_at") or ""),
            )
        )

    def _trim_tier(self, tier: str) -> None:
        limit = TIER_LIMITS.get(tier, 100)
        if len(self.data[tier]) > limit:
            overflow = self.data[tier][limit:]
            self.data[tier] = self.data[tier][:limit]
            if tier in ACTIVE_TIERS:
                now = datetime.now(timezone.utc).isoformat()
                for item in overflow:
                    expired = dict(item)
                    expired["expired_at"] = now
                    expired["priority_band"] = "expired"
                    self.data["expired"].append(expired)
                self._trim_tier("expired")
