"""Orchestrates collection, classification, store updates, and snapshots."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from macro_terminal.collector import (
    build_macro_ticker,
    build_regime_items,
    classify_news_batch,
    fetch_additional_news_items,
    fetch_gnews_items,
    fetch_market_bundle,
    _load_intraday_snapshot,
)
from macro_terminal.models import TerminalSnapshot
from macro_terminal.priority_store import MacroPriorityStore
from macro_terminal.regime import interpret_regime

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = PROJECT_ROOT / "macro_terminal_snapshot.json"
PRIORITY_STORE_PATH = PROJECT_ROOT / "macro_terminal_priority_store.json"
LEGACY_QUEUE_PATH = PROJECT_ROOT / "macro_terminal_queue.json"
QUEUE_LOG_PATH = PROJECT_ROOT / "macro_terminal_queue.jsonl"


def candle_15m_window(now: datetime) -> tuple[str, int]:
    """Return ET 15m candle label and 10-minute tick index (0 or 1) within the candle."""
    start_minute = (now.minute // 15) * 15
    start = now.replace(minute=start_minute, second=0, microsecond=0)
    end_minute = start_minute + 15
    end = now.replace(minute=end_minute % 60, hour=now.hour + (1 if end_minute >= 60 else 0), second=0, microsecond=0)
    offset_min = now.minute - start_minute
    tick = 0 if offset_min < 10 else 1
    label = f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')} ET"
    return label, tick


def seconds_until_next_interval(minutes: int = 10) -> float:
    now = datetime.now()
    total_seconds = now.hour * 3600 + now.minute * 60 + now.second
    interval = minutes * 60
    remainder = total_seconds % interval
    if remainder == 0 and now.microsecond == 0:
        return float(interval)
    return float(interval - remainder)


class MacroTerminalEngine:
    def __init__(
        self,
        *,
        snapshot_path: Path = SNAPSHOT_PATH,
        store_path: Path = PRIORITY_STORE_PATH,
        queue_log_path: Path = QUEUE_LOG_PATH,
    ):
        self.snapshot_path = snapshot_path
        self.store_path = store_path
        self.queue_log_path = queue_log_path
        if store_path.exists():
            self.store = MacroPriorityStore.load(store_path)
        elif LEGACY_QUEUE_PATH.exists():
            self.store = MacroPriorityStore.load(LEGACY_QUEUE_PATH)
        else:
            self.store = MacroPriorityStore()

    def run_cycle(self) -> TerminalSnapshot:
        now_et = datetime.now(ET)
        window, tick = candle_15m_window(now_et)
        errors: list[str] = []

        market, market_errors = fetch_market_bundle()
        errors.extend(market_errors)

        intraday = _load_intraday_snapshot()
        if not intraday:
            errors.append("intraday: no cached snapshot (run day_trading_insights or fetch_intraday)")

        regime = interpret_regime(market, intraday)
        macro_ticker = build_macro_ticker(market)

        raw_news = fetch_additional_news_items(max_total=25)
        try:
            raw_news.extend(fetch_gnews_items(market, max_queries=4))
        except Exception as exc:
            errors.append(f"gnews: {exc}")

        classified = classify_news_batch(raw_news)
        classified.extend(build_regime_items(regime))

        self.store.prune_noise()
        expired_moved = self.store.expire_stale()
        added = self.store.ingest_many(classified)
        self._append_store_log(classified, added, expired_moved)

        counts = self.store.counts()
        snapshot = TerminalSnapshot(
            timestamp_et=now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
            candle_15m_window=window,
            tick_in_candle=tick,
            macro_ticker=macro_ticker,
            regime=regime,
            top_items=self.store.top(15),
            priority_store=counts,
            queue_depth=len(self.store),
            expired_count=counts.get("expired", 0),
            errors=errors,
        )
        self._persist(snapshot)
        return snapshot

    def _append_store_log(self, items, added: int, expired_moved: int) -> None:
        if not items and not expired_moved:
            return
        record = {
            "timestamp_et": datetime.now(ET).isoformat(),
            "added": added,
            "classified": len(items),
            "expired_moved": expired_moved,
            "priority_store": self.store.counts(),
            "haiku_eligible": len(self.store.for_haiku()),
        }
        with self.queue_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def _persist(self, snapshot: TerminalSnapshot) -> None:
        payload = snapshot.to_dict()
        payload["priority_store_items"] = self.store.to_dict()
        payload["haiku_items"] = self.store.for_haiku()
        self.snapshot_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.store.save(self.store_path)
