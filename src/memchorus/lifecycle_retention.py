"""
Lifecycle Management — Phase 2: Retention Engine

Per-profile age tracking, review scoring integration, and exemption logic.

Design spec references: §3 (Retention Policy), §4.1 (Eviction Triggers)

Classes:
    RetentionEngine   — scans backends, flags expired memories, scores for archive
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from memchorus.relevance_engine import RelevanceScorer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# §3.1  Default retention periods (days). None = never expire.
# ---------------------------------------------------------------------------
_DEFAULT_RETENTION_DAYS: Dict[str, Optional[int]] = {
    "ephemeral": 7,
    "context_sensitive_pref": 30,
    "long_lived_knowledge": 180,
    "large_data_block": 30,
    "user_preference": None,
    "relationship_graph": None,
}

# §3.3 — High-importance exemption threshold
_IMPORTANCE_EXEMPTION_THRESHOLD = 0.85


@dataclass
class RetentionFlag:
    """A memory flagged for review by the retention engine."""

    memory_id: str
    source: str
    profile: str
    age_days: float
    retention_limit: int
    score: Optional[float] = None
    is_exempt: bool = False
    was_flagged_before: bool = False  # flagged in >=1 previous sweep


@dataclass
class RetentionReviewResult:
    """Summary of one ``review_all()`` pass."""

    total_scanned: int = 0
    flagged: int = 0
    exempted: int = 0
    archive_recommended: int = 0
    per_source: Dict[str, int] = field(default_factory=dict)


class RetentionEngine:
    """Evaluate memory freshness against per-profile retention windows.

    Workflow per sweep (§3.2):
    1. Enumerate memories across all registered backends (via search).
    2. Determine each memory's profile and corresponding retention period.
    3. Skip exempted memories (§3.3) — importance >= 0.85 or ``_pinned: true``.
    4. Flag memories older than their retention limit.
    5. Score flagged memories through the ``RelevanceScorer``; if below
       ``importance_min`` for two or more consecutive sweeps, recommend archive.

    The engine does NOT mutate storage — it only produces recommendations that
    the EvictionEngine consumes.
    """

    def __init__(
        self,
        retention_days: Dict[str, Optional[int]],
        scorer: RelevanceScorer,
        importance_min: float = 0.15,
        score_history: Optional[Dict[str, List[float]]] = None,
    ) -> None:
        """
        Args:
            retention_days: Per-profile mapping (profile_name → days or None).
            scorer: Shared RelevanceScorer for scoring flagged memories.
            importance_min: Below this for 2 consecutive sweeps → archive.
            score_history: Tracks how many sweeps each memory was already below
                threshold (persisted across sweeps inside LifecycleManager).
        """
        # Merge with defaults so we always have every profile covered.
        self.retention_days: Dict[str, Optional[int]] = dict(_DEFAULT_RETENTION_DAYS)
        self.retention_days.update(retention_days)

        self._scorer = scorer
        self._importance_min = importance_min

        # memory_id → list of recent scores (most recent first, truncated).
        # NOTE: use `is not None` — an empty dict {} is a valid live reference
        # that the caller (LifecycleManager) wants mutations to flow back through.
        self._score_history: Dict[str, List[float]] = (
            score_history if score_history is not None else {}
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def review_all(
        self,
        memories: List[Dict[str, Any]],
        query_hint: str = "*",
    ) -> RetentionReviewResult:
        """Scan all provided memories and flag those past retention limit.

        Args:
            memories: Flat list of memory dicts. Each must carry at least:
                - ``key`` (str): the memory identifier
                - ``source`` (str): backend name
                - ``content`` (Any): the stored payload
                Optional keys used by retention logic:
                - ``timestamp`` (str, ISO-8601): write time
                - ``_profile`` (str): profile name override
                - ``_pinned`` (bool): pin flag
                - ``_importance`` (float): pre-computed importance
            query_hint: Passed to the scorer for quality scoring.

        Returns:
            RetentionReviewResult with counts and per-source breakdowns.
        """
        result = RetentionReviewResult()
        now = datetime.now(timezone.utc)

        for mem in memories:
            result.total_scanned += 1
            mem_id = mem.get("key", "")
            source = mem.get("source", "unknown")
            result.per_source[source] = result.per_source.get(source, 0) + 1

            # — Check exemptions first (§3.3) —
            is_exempt = self._is_exempt(mem, mem_id)
            if is_exempt:
                result.exempted += 1
                continue

            # — Determine profile and retention window —
            profile = mem.get("_profile", "")
            retention_limit = self._get_retention_days(profile)
            if retention_limit is None:
                # Permanent profile — never expires.
                result.exempted += 1
                continue

            # — Compute age —
            age_days = self._compute_age_days(mem, now)
            if age_days <= retention_limit:
                # Still within retention window — no review needed.
                continue

            # — Past retention limit → flag for review —
            result.flagged += 1
            score = self._score_memory(mem, query_hint)

            # Track consecutive-sweep failure (§4.3 — two consecutive sweeps)
            was_below = mem_id in self._score_history and len(self._score_history[mem_id]) > 0
            history = self._score_history.setdefault(mem_id, [])
            history.insert(0, score if score is not None else 0.0)

            # Keep only last 2 scores — we need at most two sweeps of failure.
            while len(history) > 2:
                history.pop()

            # Archive recommendation: consecutive failures below threshold.
            below_threshold = (score if score is not None else 0.0) < self._importance_min
            if below_threshold and (was_below or len(history) >= 2):
                result.archive_recommended += 1

        return result

    def get_archive_candidates(
        self,
        reviews: RetentionReviewResult,
        memories_by_id: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Return memories that were recommended for archive.

        This is a convenience wrapper around ``review_all`` output — it returns
        the actual memory dicts whose ids have consecutive low scores in history.

        Args:
            reviews: Output from a prior ``review_all()`` call.
            memories_by_id: Mapping of memory key → full dict for reference.
        """
        candidates = []
        for mem_id, history in self._score_history.items():
            if len(history) >= 2 and all(s < self._importance_min for s in history):
                if mem_id in memories_by_id:
                    candidates.append(memories_by_id[mem_id])
        return candidates

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_exempt(self, mem: Dict[str, Any], mem_id: str) -> bool:
        """Check §3.3 exemptions: pinned flag or high importance."""
        if mem.get("_pinned"):
            return True
        importance = mem.get("_importance")
        if isinstance(importance, (int, float)) and importance >= _IMPORTANCE_EXEMPTION_THRESHOLD:
            return True
        return False

    def _get_retention_days(self, profile: str) -> Optional[int]:
        """Look up retention limit for a profile name.

        Returns None for permanent profiles (user_preference, relationship_graph),
        which signals "never expire" to the caller.
        Falls back to ephemeral for completely unknown profiles.
        """
        # Normalise to snake_case if needed.
        norm = profile.lower().replace("-", "_").replace(" ", "_")
        if norm in self.retention_days:
            days = self.retention_days[norm]
            return None if days is None else int(days)
        # Unknown profile: fall back to ephemeral default (§3.1 AUTO behaviour).
        return self.retention_days.get("ephemeral")

    @staticmethod
    def _compute_age_days(mem: Dict[str, Any], now: datetime) -> float:
        """Compute age in days from ``timestamp`` or meta info."""
        ts_str = mem.get("timestamp")
        if ts_str:
            try:
                write_time = datetime.fromisoformat(str(ts_str))
                if write_time.tzinfo is None:
                    write_time = write_time.replace(tzinfo=timezone.utc)
                delta = (now - write_time).total_seconds() / 86400.0
                return max(delta, 0.0)
            except (ValueError, TypeError):
                pass
        # No parseable timestamp — assume neutral (not expired).
        return 0.0

    def _score_memory(
        self, mem: Dict[str, Any], query_hint: str
    ) -> Optional[float]:
        """Score a memory through the RelevanceScorer."""
        try:
            result = {
                "key": mem.get("key", ""),
                "content": mem.get("content", ""),
                "source": mem.get("source", "unknown"),
                "timestamp": mem.get("timestamp"),
            }
            return self._scorer.score(result, query_hint)
        except Exception as exc:
            logger.warning(
                "RetentionEngine: scoring failed for %s: %s", mem.get("key", "?"), exc
            )
            return None
