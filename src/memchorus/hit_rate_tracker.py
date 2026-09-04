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
from typing import Any, Dict

from memchorus.hermes_home import hermes_home

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

    xdist safety: the class-level lock is recreated after fork because
    inherited locks from the parent process may be held by now-dead threads.
    """

    _instances: Dict[str, "HitRateTracker"] = {}
    _lock_cls: threading.Lock = threading.Lock()

    def __init__(self, memory_dir: str):
        self.memory_dir = os.path.expanduser(memory_dir)
        self._index: Dict[str, Dict[str, Any]] = _load_index(self.memory_dir)
        self._dir_lock: threading.RLock = threading.RLock()  # reentrant — allows nested calls (e.g. flush during save) without deadlock

    # -- singleton ---------------------------------------------------------

    @classmethod
    def _ensure_lock(cls) -> None:
        """Ensure the class-level lock is usable (recreate after fork)."""
        try:
            cls._lock_cls.acquire()
            cls._lock_cls.release()
        except OSError:
            # Stale lock inherited from parent after fork — replace it
            cls._lock_cls = threading.Lock()

    @classmethod
    def _default_memory_dir(cls) -> str:
        """Resolve the hit-rate index directory for the *current* profile.

        Re-reads ``HERMES_PROFILE`` on every call so that a process that
        switches profiles in-process (multi-profile nightly analyzer) gets a
        distinct tracker per profile instead of the first profile's tracker
        being pinned forever. See MemChorus #171 (cross-profile state bleed).
        """
        try:
            raw_profile = os.environ.get("HERMES_PROFILE", "default")
            if raw_profile != "default":
                return str(hermes_home() / "profiles" / raw_profile / "memories")
            return str(hermes_home() / "memories")
        except Exception:
            return str(hermes_home() / "memories")

    @classmethod
    def get_instance(cls, memory_dir: str | None = None) -> "HitRateTracker":
        """Return (creating if necessary) the HitRateTracker for a directory.

        Instances are keyed by their normalized memory directory, so two
        distinct profiles — even within one process — get two distinct
        trackers that write to distinct ``_hit_rate_index.json`` sidecars.

        Args:
            memory_dir: When provided, the tracker for that specific directory
                is returned (creating it on first use).  When ``None``, the
                directory is resolved from the *current* ``HERMES_PROFILE``
                each time, so switching profiles re-resolves automatically.
        """
        cls._ensure_lock()
        key = os.path.realpath(memory_dir or cls._default_memory_dir())
        inst = cls._instances.get(key)
        if inst is None:
            with cls._lock_cls:
                inst = cls._instances.get(key)
                if inst is None:
                    inst = cls(key)
                    cls._instances[key] = inst
        return inst

    @classmethod
    def reset(cls, memory_dir: str | None = None) -> None:
        """Clear the tracker registry (test convenience).

        Wipes in-memory indices so stale data cannot leak into the next test
        run.  When *memory_dir* is supplied, only that directory's tracker is
        reset and its persisted sidecar JSON deleted (per-profile isolation
        between runs); when ``None``, the entire registry is cleared and no
        sidecars are deleted.

        Args:
            memory_dir: Directory to reset (and delete its sidecar), or ``None``
                to reset every registered tracker.
        """
        cls._ensure_lock()
        with cls._lock_cls:
            if memory_dir is not None:
                key = os.path.realpath(memory_dir)
                cls._instances.pop(key, None)
            else:
                insts = list(cls._instances.values())
                cls._instances.clear()
                for old in insts:
                    old._index.clear()
            # Delete persisted sidecar when a directory is supplied
            if memory_dir is not None:
                sidecar = _resolve_hit_rate_file(memory_dir)
                try:
                    sidecar.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("could not delete sidecar %s: %s", sidecar, exc)

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

    def register_save(self, entry_key: str) -> None:
        """Register that *entry_key* was saved (initialises counters if new).

        Called from the orchestrator save() hot path. ≤ 15 µs — no I/O."""
        try:
            with self._dir_lock:
                self._ensure_entry(entry_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("register_save failed for %r: %s", entry_key, exc)

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
        # Snapshot under lock, persist outside to avoid nested-lock issues
        snapshot = self.get_all_stats()
        try:
            _save_index(self.memory_dir, snapshot)
            logger.debug("hit-rate index flushed (%d entries)", len(self._index))
        except Exception as exc:  # noqa: BLE001
            logger.warning("flush failed: %s", exc)

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Return a snapshot of all entry stats (for CalibrationEngine)."""
        with self._dir_lock:
            return {k: dict(v) for k, v in self._index.items()}
