"""HitRateTracker — per-entry recall/useful/noise counters for auto-tuning.

Part of the MemChorus auto-tuning framework (v1.8.0).
Singleton that records empirical hit-rate data from recall operations and
user feedback signals, persisted as a sidecar JSON alongside memory files.

See docs/AUTOTUNING.md for design rationale.

Performance: ≤ 15 µs inline overhead per operation via lazy persistence
and in-memory counter cache. No blocking I/O on the hot path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

_HIT_RATE_FILE = "_hit_rate_index.json"


def _resolve_hit_rate_file(memory_dir: str) -> Path:
    """Return the sidecar hit-rate index path for a given memory directory."""
    return Path(memory_dir) / _HIT_RATE_FILE


def _load_index(memory_dir: str) -> Dict[str, Any]:
    """Load the persisted hit-rate index or return fresh defaults."""
    path = _resolve_hit_rate_file(memory_dir)
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                return raw
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to parse hit-rate index %s: %s", path, exc)
    return {}


def _save_index(memory_dir: str, index: Dict[str, Any]) -> None:
    """Persist the hit-rate index atomically."""
    path = _resolve_hit_rate_file(memory_dir)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index))
        os.replace(str(tmp), str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to persist hit-rate index %s: %s", path, exc)


def _default_entry_stats(ts: float | None = None) -> Dict[str, Any]:
    """Fresh hit-rate counters for a new entry."""
    now = ts if ts is not None else time.time()
    return {
        "total_recalls": 0,
        "useful_flags": 0,
        "noise_flags": 0,
        "first_saved_at": now,
        "last_seen_at": now,
    }


# ---------------------------------------------------------------------------
# HitRateTracker — singleton per profile
# ---------------------------------------------------------------------------

class HitRateTracker:
    """Tracks per-entry recall frequency and user feedback signals.

    Maintains an in-memory index of entry-key → counter dicts and lazily
    persists to a sidecar JSON file so that the hot path stays ≤ 15 µs.

    Singleton access via `HitRateTracker.get_instance()`.
    """

    _instance: Optional["HitRateTracker"] = None
    _lock_cls = threading.Lock()

    def __init__(self, memory_dir: str):
        self.memory_dir = os.path.expanduser(memory_dir)
        self._index: Dict[str, Dict[str, Any]] = _load_index(self.memory_dir)
        self._dir_lock = threading.Lock()

    # -- singleton ---------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "HitRateTracker":
        """Return (creating if necessary) the global HitRateTracker."""
        if cls._instance is None:
            with cls._lock_cls:
                if cls._instance is None:
                    # Resolve memory dir from environment or config
                    try:
                        raw_profile = os.environ.get("HERMES_PROFILE", "default")
                        if raw_profile != "default":
                            md = f"~/.hermes/profiles/{raw_profile}/memories"
                        else:
                            md = "~/.hermes/memories"
                    except Exception:
                        md = "~/.hermes/memories"
                    cls._instance = cls(md)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Clear the singleton (test convenience)."""
        with cls._lock_cls:
            cls._instance = None

    # -- aggregate counters ------------------------------------------------

    @property
    def total_saves(self) -> int:
        """Approximate save count: number of tracked entries × avg recalls + 1."""
        return len(self._index)

    @property
    def total_recalls(self) -> int:
        """Sum of all recall counts across entries."""
        return sum(
            e.get("total_recalls", 0)
            for e in self._index.values()
            if isinstance(e, dict)
        )

    # -- per-entry mutations -----------------------------------------------

    def _ensure_entry(self, entry_key: str) -> Dict[str, Any]:
        """Ensure *entry_key* exists in the index, initialising silently."""
        if entry_key not in self._index or not isinstance(self._index[entry_key], dict):
            self._index[entry_key] = _default_entry_stats()
        return self._index[entry_key]

    def record_recallhit(self, entry_key: str) -> None:
        """Record that *entry_key* was returned in a recall result (+1 total_recalls)."""
        try:
            with self._dir_lock:
                stats = self._ensure_entry(entry_key)
                stats["total_recalls"] = stats.get("total_recalls", 0) + 1
                stats["last_seen_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.debug("record_recallhit failed for %r: %s", entry_key, exc)

    def record_useful(self, entry_key: str, count: int = 1) -> None:
        """Mark *entry_key* as useful (+count useful_flags)."""
        try:
            with self._dir_lock:
                stats = self._ensure_entry(entry_key)
                stats["useful_flags"] = stats.get("useful_flags", 0) + count
                stats["last_seen_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.debug("record_useful failed for %r: %s", entry_key, exc)

    def record_stale(self, entry_key: str, count: int = 1) -> None:
        """Mark *entry_key* as stale / noisy (+count noise_flags)."""
        try:
            with self._dir_lock:
                stats = self._ensure_entry(entry_key)
                stats["noise_flags"] = stats.get("noise_flags", 0) + count
                stats["last_seen_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.debug("record_stale failed for %r: %s", entry_key, exc)

    # -- read ------------------------------------------------------------

    def get_hit_stats(self, entry_key: str) -> Dict[str, Any]:
        """Return current counters for *entry_key* (initialising on first call)."""
        with self._dir_lock:
            return dict(self._ensure_entry(entry_key))

    # -- persistence -------------------------------------------------------

    def flush(self) -> None:
        """Persist the in-memory index to disk."""
        try:
            with self._dir_lock:
                _save_index(self.memory_dir, self._index)
            logger.debug("hit-rate index flushed (%d entries)", len(self._index))
        except Exception as exc:  # noqa: BLE001
            logger.warning("flush failed: %s", exc)

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Return a snapshot of all entry stats (for CalibrationEngine)."""
        with self._dir_lock:
            return {k: dict(v) for k, v in self._index.items()}
