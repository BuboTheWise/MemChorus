"""
Lifecycle Management — Phase 1 Foundation

Opt-in layer for scheduled memory sweeps, audit logging, and policy scaffolding.
All defaults keep the system write-only unless lifecycle.enabled is explicitly True
(§9 backward compatibility).

Classes:
    AuditLogger          — NDJSON/JSONL writer with configurable rotation
    LifecycleManager     — orchestrates sweeps, holds policy config
    SweepScheduler       — timed execution driver preventing overlapping sweeps
"""

import json
import logging
import os
import pathlib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from memchorus.orchestrator import MemoryOrchestrator

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# §6.2  Configuration schema — defaults and resolver
# -----------------------------------------------------------------------

# Retention days per profile (§3.1). None means "never expire".
_DEFAULT_RETENTION_DAYS: Dict[str, Optional[int]] = {
    "ephemeral": 7,
    "context_sensitive_pref": 30,
    "long_lived_knowledge": 180,
    "large_data_block": 30,
    "user_preference": None,
    "relationship_graph": None,
}


def _resolve_lifecycle_config(
    raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge user-provided lifecycle config with safe defaults.

    Returns a fully populated dict even when *raw* is ``None`` or empty —
    every downstream consumer can assume all keys exist.

    §9: ``enabled`` defaults to ``False`` for backward compatibility.
    """
    if not raw:
        raw = {}

    # Top-level toggles (§6.1)
    config: Dict[str, Any] = {
        "enabled": bool(raw.get("enabled", False)),
        "sweep_interval_hours": int(raw.get("sweep_interval_hours", 8)),
    }

    # Per-profile retention (§3.1)
    user_retention: Dict[str, Any] = raw.get("retention_days", {})
    config["retention_days"] = dict(_DEFAULT_RETENTION_DAYS)
    for key, val in user_retention.items():
        norm_key = key.lower().replace(" ", "_")
        if norm_key in config["retention_days"]:
            config["retention_days"][norm_key] = int(val) if val is not None else None

    # Eviction thresholds (§4.1)
    eviction_raw: Dict[str, Any] = raw.get("eviction", {})
    config["eviction"] = {
        "importance_min": float(eviction_raw.get("importance_min", 0.15)),
        "duplicate_cluster_max": int(eviction_raw.get("duplicate_cluster_max", 3)),
        "similarity_min": float(eviction_raw.get("similarity_min", 0.75)),
    }

    # Archive policy (§4.2)
    archive_raw: Dict[str, Any] = raw.get("archive", {})
    config["archive"] = {
        "grace_days": int(archive_raw.get("grace_days", 30)),
        "score_penalty": float(archive_raw.get("score_penalty", -0.7)),
    }

    # Merge-at-write (§5.1)
    merge_raw: Dict[str, Any] = raw.get("merge_at_write", {})
    config["merge_at_write"] = {
        "enabled": bool(merge_raw.get("enabled", True)),
    }

    # Audit logging (§6.4)
    audit_raw: Dict[str, Any] = raw.get("audit", {})
    default_log_path = os.path.expanduser("~/.hermes/memchorus_audit.jsonl")
    config["audit"] = {
        "enabled": bool(audit_raw.get("enabled", True)),
        "log_path": str(audit_raw.get("log_path", default_log_path)),
        "max_entries": int(audit_raw.get("max_entries", 10_000)),
    }

    return config


# -----------------------------------------------------------------------
# §6.4  Audit Logger — NDJSON writer with rotation
# -----------------------------------------------------------------------

class AuditAction(str, Enum):
    """Human-friendly reason codes for audit entries."""
    ARCHIVE = "archive"
    PURGE = "purge"
    MERGE_OVERWRITE = "merge_overwrite"
    MERGE_APPEND = "merge_append"
    MERGE_UNION = "merge_union"
    BACKEND_UNREACHABLE = "backend_unreachable"


@dataclass
class AuditEntry:
    """Single JSONL audit record (§6.4)."""

    ts: str  # ISO-8601 UTC timestamp
    action: str
    memory_id: str = ""
    source: str = ""
    reason: str = ""
    prev_score: Optional[float] = None
    profile: str = ""
    drawer: str = ""
    replaced: str = ""  # for merge actions, ID of subsumed entry

    def to_json(self) -> str:
        """Serialise to a single NDJSON line (compact)."""
        obj: Dict[str, Any] = {"ts": self.ts, "action": self.action}
        if self.memory_id:
            obj["memory_id"] = self.memory_id
        if self.source:
            obj["source"] = self.source
        if self.reason:
            obj["reason"] = self.reason
        if self.prev_score is not None:
            obj["prev_score"] = self.prev_score
        if self.profile:
            obj["profile"] = self.profile
        if self.drawer:
            obj["drawer"] = self.drawer
        if self.replaced:
            obj["replaced"] = self.replaced
        return json.dumps(obj, separators=(",", ":"))


class AuditLogger:
    """Append-only NDJSON audit writer with size-based rotation.

    Every purge/merge/archive action produces a structured log line so the
    agent can diagnose retrieval gaps (§2.5).

    Thread-safe and silently degrades when the target path is unwritable.
    """

    def __init__(
        self,
        log_path: str = os.path.expanduser("~/.hermes/memchorus_audit.jsonl"),
        max_entries: int = 10_000,
        enabled: bool = True,
    ) -> None:
        self.log_path = os.path.expanduser(log_path)
        self.max_entries = max(1, max_entries)
        self._enabled = enabled
        self._lock = threading.Lock()
        # Ensure the parent directory exists before first write.
        try:
            parent = pathlib.Path(self.log_path).parent
            if parent and str(parent) != ".":
                parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.debug("AuditLogger: cannot create dir for %s — will degrade on write", self.log_path)

    # ---- public API --------------------------------------------------

    def log(self, entry: AuditEntry) -> None:
        """Write an audit entry; rotate if past max_entries. No-ops when disabled."""
        if not self._enabled:
            return
        with self._lock:
            _do_rotate(self.log_path, self.max_entries)
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(entry.to_json() + "\n")
            except OSError as exc:
                logger.debug("AuditLogger: write failed: %s", exc)

    def record(
        self,
        action: str,
        memory_id: str = "",
        source: str = "",
        reason: str = "",
        prev_score: Optional[float] = None,
        profile: str = "",
        drawer: str = "",
        replaced: str = "",
    ) -> None:
        """Convenience wrapper — builds an AuditEntry internally."""
        ts = datetime.now(timezone.utc).isoformat()
        self.log(AuditEntry(ts=ts, action=action, memory_id=memory_id, source=source, reason=reason, prev_score=prev_score, profile=profile, drawer=drawer, replaced=replaced))

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---- back-compat proxy for ``self.lifecycle_manager.audit`` style access --


# -----------------------------------------------------------------------
# Lifecycle Manager — skeleton (§7 + §8 Phase 1)
# -----------------------------------------------------------------------

class LifecycleManager:
    """Phase 2: full lifecycle orchestrator — retention review → eviction pipeline.

    ``LifecycleManager.sweep()`` now runs the complete cycle:
      1. RetentionEngine.review_all() — flags expired memories, scores them
      2. EvictionEngine.evaluate_all() — archive/purge based on AND-logic triggers

    Graceful degradation (§6.3):
      - Per-backend failures are caught, logged, and the sweep continues.
      - After 3 consecutive backend failures, that backend enters a 24-hour cooldown.
      - Concurrency guard prevents overlapping sweeps (delegated to SweepScheduler).

    Score history (consecutive-sweep tracking) is persisted across sweep calls.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        orchestrator: Optional["MemoryOrchestrator"] = None,
    ) -> None:
        self.config = config
        self._orchestrator = orchestrator
        self._audit = AuditLogger(
            log_path=config.get("audit", {}).get("log_path", os.path.expanduser("~/.hermes/memchorus_audit.jsonl")),
            max_entries=config.get("audit", {}).get("max_entries", 10_000),
            enabled=config.get("audit", {}).get("enabled", True),
        )

        # --- Phase 2: engine instances (lazy to avoid import cycles) ---
        self._retention_engine = None
        self._eviction_engine = None
        self._scheduler: Optional["SweepScheduler"] = None
        self._score_history: Dict[str, List[float]] = {}

        # §6.3 — per-backend failure tracking for cooldown logic
        # backend_name → {"count": int, "last_failure_ts": float}
        self._backend_failures: Dict[str, Dict[str, Any]] = {}

        # Concurrency guard: prevent nested/overlapping sweeps internally.
        self._sweep_lock = threading.Lock()

    # ---- lazy engine initialisation ------------------------------------

    def _get_retention_engine(self):
        """Get or create RetentionEngine (lazy to avoid import cycle on init)."""
        if self._retention_engine is None:
            from memchorus.lifecycle_retention import RetentionEngine
            from memchorus.relevance_engine import RelevanceScorer

            half_life = float(self.config.get("half_life_days", 30.0))
            scorer = RelevanceScorer(half_life_days=half_life)
            ret_config = self.config.get("retention_days", {})
            imp_min = float(self.config.get("eviction", {}).get("importance_min", 0.15))
            self._retention_engine = RetentionEngine(
                retention_days=ret_config,
                scorer=scorer,
                importance_min=imp_min,
                score_history=self._score_history,
            )
        return self._retention_engine

    def _get_eviction_engine(self):
        """Get or create EvictionEngine (lazy to avoid import cycle on init)."""
        if self._eviction_engine is None:
            from memchorus.lifecycle_eviction import EvictionEngine

            ev_config = self.config.get("eviction", {})
            ar_config = self.config.get("archive", {})
            self._eviction_engine = EvictionEngine(
                importance_min=float(ev_config.get("importance_min", 0.15)),
                duplicate_cluster_max=int(ev_config.get("duplicate_cluster_max", 3)),
                similarity_min=float(ev_config.get("similarity_min", 0.75)),
                archive_grace_days=int(ar_config.get("grace_days", 30)),
                archive_score_penalty=float(ar_config.get("score_penalty", -0.7)),
            )
        return self._eviction_engine

    # ---- properties --------------------------------------------------

    @property
    def audit(self) -> AuditLogger:
        """Public access to the audit logger."""
        return self._audit

    @property
    def is_enabled(self) -> bool:
        """Whether lifecycle management is active (master toggle)."""
        return bool(self.config.get("enabled", False))

    @property
    def audit_logger(self) -> AuditLogger:
        """Alias for ``self.audit`` — backward compatibility."""
        return self._audit

    # ---- §6.3 helpers ------------------------------------------------

    def _is_backend_in_cooldown(self, backend_name: str) -> bool:
        """Return True if this backend is currently in cooldown after 3+ failures."""
        info = self._backend_failures.get(backend_name)
        if not info or info["count"] < 3:
            return False
        elapsed = time.time() - info["last_failure_ts"]
        # 24-hour cooldown window
        return elapsed < 86400

    def _record_backend_failure(self, backend_name: str) -> None:
        """Increment failure counter for a backend."""
        if backend_name not in self._backend_failures:
            self._backend_failures[backend_name] = {"count": 0, "last_failure_ts": 0.0}
        self._backend_failures[backend_name]["count"] += 1
        self._backend_failures[backend_name]["last_failure_ts"] = time.time()

    def _clear_backend_failure(self, backend_name: str) -> None:
        """Reset failure counter after a successful sweep."""
        if backend_name in self._backend_failures:
            del self._backend_failures[backend_name]

    # ---- gather memories across a backend ----------------------------

    def _gather_memories_from_source(
        self, source_name: str, source_obj
    ) -> List[Dict[str, Any]]:
        """Collect all active memories from one backend via its search API.

        Returns a flat list of dicts with keys: key, content, source, timestamp.
        """
        results = []
        try:
            # broad wildcard search to enumerate all entries
            raw = source_obj.search("*", limit=10_000)
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    item.setdefault("source", source_name)
                    results.append(item)
        except Exception as exc:
            self._record_backend_failure(source_name)
            logger.warning(
                "LifecycleManager: backend %s unreachable during sweep: %s",
                source_name, exc,
            )
            self._audit.record(
                action=AuditAction.BACKEND_UNREACHABLE,
                source=source_name,
                reason=f"backend_unreachable: {exc}",
            )
        return results

    # ---- sweep implementation (Phase 2) -----------------------------

    def sweep(self) -> Dict[str, Any]:
        """Execute one full lifecycle sweep cycle.

        Cycle (§7): RetentionEngine.review_all() → EvictionEngine.evaluate_all()

        Graceful degradation (§6.3):
          - Skips backends in cooldown or that throw during enumeration.
          - Logs failures to audit trail, continues with other backends.
          - After 3 consecutive failures per backend, enters 24-hour cooldown.

        Concurrency guard: uses ``self._sweep_lock`` to prevent overlapping runs.

        Returns a summary dict with sweep time, reviewed/archived/purged counts.
        """
        if not self.is_enabled:
            return {
                "sweep_time": datetime.now(timezone.utc).isoformat(),
                "memories_reviewed": 0,
                "memories_archived": 0,
                "memories_purged": 0,
                "merges_performed": 0,
                "skipped_backends": [],
                "_reason": "lifecycle disabled",
            }

        # Prevent nested/overlapping sweeps at the application level too.
        acquired = self._sweep_lock.acquire(blocking=False)
        if not acquired:
            logger.warning("LifecycleManager: sweep skipped — another sweep in progress")
            return {
                "sweep_time": datetime.now(timezone.utc).isoformat(),
                "memories_reviewed": 0,
                "memories_archived": 0,
                "memories_purged": 0,
                "merges_performed": 0,
                "_reason": "concurrent_sweep",
            }

        try:
            return self._do_sweep()
        finally:
            self._sweep_lock.release()

    # ------------------------------------------------------------------

    def _do_sweep(self) -> Dict[str, Any]:
        """Inner sweep logic (must be called under ``_sweep_lock``)."""
        from memchorus.lifecycle_eviction import EvictionCandidate, PurgeReason

        self._audit.record(
            action=AuditAction.ARCHIVE,
            reason="lifecycle_sweep_started",
        )
        logger.info("LifecycleManager: starting lifecycle sweep")

        all_memories: List[Dict[str, Any]] = []
        skipped_backends: List[str] = []

        # 1. Enumerate memories from each registered backend (with graceful degradation)
        orchestrator = self._orchestrator
        if orchestrator is not None and hasattr(orchestrator, "memory_sources"):
            for src_name, src_obj in orchestrator.memory_sources.items():
                # §6.3 — skip backends in cooldown
                if self._is_backend_in_cooldown(src_name):
                    logger.info(
                        "LifecycleManager: backend %s in cooldown — skipping", src_name
                    )
                    skipped_backends.append(src_name)
                    continue

                memories = self._gather_memories_from_source(src_name, src_obj)
                if memories:
                    self._clear_backend_failure(src_name)  # success resets counter
                    all_memories.extend(memories)
                else:
                    # No data is fine — backend returned empty list successfully.
                    self._clear_backend_failure(src_name)

        # 2. Retention review phase
        retention = self._get_retention_engine()
        review_result = retention.review_all(all_memories, query_hint="*")

        logger.info(
            "LifecycleManager: retention review — scanned=%d flagged=%d exempted=%d "
            "archive_candidates=%d",
            review_result.total_scanned,
            review_result.flagged,
            review_result.exempted,
            review_result.archive_recommended,
        )

        # 3. Eviction phase — build candidate list from archive recommendations
        memories_by_id = {m.get("key", ""): m for m in all_memories}
        archive_candidates = retention.get_archive_candidates(review_result, memories_by_id)

        total_archived = 0
        total_purged = 0

        if archive_candidates:
            eviction = self._get_eviction_engine()

            # Build EvictionCandidate list
            candidates: List[EvictionCandidate] = []
            for mem in archive_candidates:
                mem_id = mem.get("key", "")
                prev_score = 0.0
                history = self._score_history.get(mem_id, [])
                if history:
                    prev_score = history[0]

                candidates.append(EvictionCandidate(
                    memory_id=mem_id,
                    source=mem.get("source", "unknown"),
                    profile=mem.get("_profile", ""),
                    content=mem.get("content", ""),
                    prev_score=prev_score,
                    reason=PurgeReason.AGE if mem.get("_profile") == "ephemeral" else PurgeReason.IMPORTANCE,
                    timestamp=mem.get("timestamp"),
                ))

            # In-memory archive store (used by eviction callbacks)
            _archive_store: Dict[str, Any] = {}

            def _do_archive(
                memory_id: str,
                content_with_penalty: Any,
                source: str,
                prev_score: float,
            ) -> bool:
                """Archive callback — stores penalized content."""
                _archive_store[memory_id] = content_with_penalty
                return True

            def _do_purge(memory_id: str, source: str) -> bool:
                """Purge callback — removes from archive store."""
                if memory_id in _archive_store:
                    del _archive_store[memory_id]
                return True

            eviction_result = eviction.evaluate_all(
                candidates,
                archive_fn=_do_archive,
                purge_fn=_do_purge,
                audit_log=self._audit.record,
            )

            total_archived = eviction_result.archived_count
            total_purged = eviction_result.purged_count

            logger.info(
                "LifecycleManager: eviction — archived=%d purged=%d skipped=%d",
                total_archived, total_purged, eviction_result.skipped_count,
            )

        sweep_summary = {
            "sweep_time": datetime.now(timezone.utc).isoformat(),
            "memories_reviewed": review_result.total_scanned,
            "memories_archived": total_archived,
            "memories_purged": total_purged,
            "memories_flagged": review_result.flagged,
            "memories_exempted": review_result.exempted,
            "merges_performed": 0,
            "skipped_backends": skipped_backends,
        }

        self._audit.record(
            action=AuditAction.ARCHIVE,
            reason="lifecycle_sweep_completed",
        )

        logger.info("LifecycleManager: sweep completed — %s", sweep_summary)
        return sweep_summary


# -----------------------------------------------------------------------
# Sweep Scheduler — timed execution driver (§6.1)
# -----------------------------------------------------------------------

class SweepScheduler:
    """Periodic sweep executor with overlap protection.

    Starts a background thread that calls ``LifecycleManager.sweep()`` every
    *interval_seconds* hours (from config). Stops cleanly via ``stop()``,
    or by calling the destructor.
    """

    def __init__(self, manager: LifecycleManager) -> None:
        self._manager = manager
        self._interval_secs = max(1, int(manager.config.get("sweep_interval_hours", 8)) * 3600)
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._overlapping_sweep = False

    @property
    def is_running(self) -> bool:
        return self._running

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> None:
        """Begin periodic sweeps in the background."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="memchorus-sweep")
        self._thread.start()
        logger.info("SweepScheduler: started (interval=%ds)", self._interval_secs)

    def stop(self) -> None:
        """Stop the scheduler and wait for any in-progress sweep."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None
        logger.info("SweepScheduler: stopped")

    # ---- internal loop -----------------------------------------------

    def _run_loop(self) -> None:
        while self._running:
            time.sleep(self._interval_secs)
            if not self._running:
                break
            with self._lock:
                if self._overlapping_sweep:
                    logger.warning("SweepScheduler: skipping — previous sweep still in progress")
                    continue
                self._overlapping_sweep = True
            try:
                self._manager.sweep()
            except Exception as exc:
                logger.error("SweepScheduler: sweep failed: %s", exc)
                # Record the failure in audit trail (§6.3)
                try:
                    self._manager.audit.record(
                        action=AuditAction.BACKEND_UNREACHABLE,
                        reason=f"sweep_error: {exc}",
                    )
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._overlapping_sweep = False

    # ---- context manager support -------------------------------------

    def __enter__(self) -> "SweepScheduler":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()


# -----------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------

def _do_rotate(log_path: str, max_entries: int) -> None:
    """Shed oldest entries when file exceeds *max_entries* lines."""
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except (FileNotFoundError, OSError):
        return  # new file or unreadable — nothing to rotate yet

    if len(lines) >= max_entries:
        keep = lines[len(lines) - max_entries + 1:]  # drop oldest; buffer 1 for the incoming line
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
        except OSError as exc:
            logger.debug("AuditLogger: rotation failed: %s", exc)
