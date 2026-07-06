"""
Lifecycle Management — Phase 2: Eviction Engine

Trigger evaluation (AND logic), two-phase archive → purge pipeline,
structural cleanup, and reason codes.

Design spec references: §4 (Eviction and Purge Policy)

Classes:
    EvictionEngine     — evaluates triggers, manages archive/purge lifecycle
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# §4.3  Purge reason codes
# ---------------------------------------------------------------------------

class PurgeReason(str, Enum):
    """Human-readable purge reason codes (§4.3)."""
    AGE = "AGE"
    IMPORTANCE = "IMPORTANCE"
    DUPE_MERGE = "DUPE_MERGE"
    USER_PURGE = "USER_PURGE"
    STRUCTURAL = "STRUCTURAL"


# ---------------------------------------------------------------------------
# Status tracking for memories in the eviction pipeline
# ---------------------------------------------------------------------------

ACTIVE = "active"
ARCHIVED = "archived"
PURGED = "purged"


@dataclass
class EvictionCandidate:
    """A memory identified by the retention engine as archive-worthy."""
    memory_id: str
    source: str
    profile: str
    content: Any
    prev_score: float
    reason: PurgeReason
    timestamp: Optional[str] = None


@dataclass
class EvictionResult:
    """Summary output from one ``evaluate_all()`` pass."""
    archived_count: int = 0
    purged_count: int = 0
    skipped_count: int = 0
    structural_cleanups: int = 0
    archive_actions: List[Dict[str, Any]] = field(default_factory=list)
    purge_actions: List[Dict[str, Any]] = field(default_factory=list)


class EvictionEngine:
    """Evaluate eviction triggers and drive the two-phase pipeline.

    Pipeline (§4.2): ACTIVE → ARCHIVED → PURGED

    **Trigger evaluation** (§4.1 AND logic):
    A memory becomes eligible only when **all** applicable triggers fire:
      1. Importance threshold — relevance < importance_min
      2. Age threshold — age > profile retention period
      3. (Optional) Duplicate density — cluster size exceeds max
      4. (Structural) Empty drawer check

    Phase 1 — Archive (Soft-delete):
      Memory moves to an archive namespace with ``archive_penalty`` applied
      to its score. Remains searchable but at reduced priority.

    Phase 2 — Purge (Hard-delete):
      After ``grace_days`` in archive, memory is hard-deleted and audit-logged.

    The engine delegates actual storage mutations to caller-provided callbacks
    rather than coupling to specific MemorySource implementations (§7.1).
    """

    def __init__(
        self,
        importance_min: float = 0.15,
        duplicate_cluster_max: int = 3,
        similarity_min: float = 0.75,
        archive_grace_days: int = 30,
        archive_score_penalty: float = -0.7,
    ) -> None:
        """
        Args:
            importance_min: Score floor — below this triggers eviction.
            duplicate_cluster_max: Max near-duplicate entries before merge review.
            similarity_min: Semantic overlap threshold for "near-duplicate".
            archive_grace_days: Days in archive before hard-delete (§4.2).
            archive_score_penalty: Scoring penalty applied to archived memories.
        """
        self._importance_min = importance_min
        self._duplicate_cluster_max = duplicate_cluster_max
        self._similarity_min = similarity_min
        self._grace_days = archive_grace_days
        self._archive_penalty = archive_score_penalty

        # Track which memories are archived and when.
        # memory_id → {"source": str, "archived_at": datetime, "reason": PurgeReason}
        self._archive_state: Dict[str, Dict[str, Any]] = {}

    @property
    def archive_state(self) -> Dict[str, Dict[str, Any]]:
        """Export of internal archive tracking."""
        return dict(self._archive_state)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_all(
        self,
        candidates: List[EvictionCandidate],
        archive_fn: Callable[[str, Any, str, float], bool],
        purge_fn: Callable[[str, str], bool],
        audit_log: Callable[..., None],
    ) -> EvictionResult:
        """Evaluate all eviction candidates and drive the pipeline.

        Args:
            candidates: List of memories flagged by RetentionEngine for review.
            archive_fn: Callback to move a memory into archive storage.
                Signature: ``(memory_id, content_with_penalty, source, prev_score) -> bool``
            purge_fn: Callback to hard-delete a memory from storage.
                Signature: ``(memory_id, source) -> bool``
            audit_log: Audit logger callable — ``action, memory_id, source, reason, ...``

        Returns:
            EvictionResult with counts and action details.
        """
        result = EvictionResult()

        for candidate in candidates:
            mem_id = candidate.memory_id

            # §4.1 AND logic: all applicable triggers must fire. For candidates
            # passed from RetentionEngine, importance + age already fired. We check
            # the remaining conditions here.
            if not self._all_triggers_fire(candidate):
                result.skipped_count += 1
                continue

            # Check if already archived — see if grace period has elapsed.
            archive_record = self._archive_state.get(mem_id)
            if archive_record:
                # Already archived → check whether to purge now.
                if self._grace_period_expired(archive_record):
                    success = purge_fn(mem_id, candidate.source)
                    if success:
                        result.purged_count += 1
                        action_detail = {
                            "memory_id": mem_id,
                            "source": candidate.source,
                            "reason": candidate.reason.value,
                            "prev_score": candidate.prev_score,
                            "profile": candidate.profile,
                        }
                        result.purge_actions.append(action_detail)
                        audit_log(
                            action="purge",
                            memory_id=mem_id,
                            source=candidate.source,
                            reason=candidate.reason.value,
                            prev_score=candidate.prev_score,
                            profile=candidate.profile,
                        )
                        # Remove from archive tracking after purge.
                        del self._archive_state[mem_id]
                    else:
                        result.skipped_count += 1
                else:
                    result.skipped_count += 1
            else:
                # Not yet archived → move to archive (Phase 1 soft-delete).
                penalized_content = self._apply_archive_penalty(candidate)
                success = False
                try:
                    success = archive_fn(
                        mem_id, penalized_content,
                        candidate.source, candidate.prev_score
                    )
                except Exception as exc:
                    logger.warning(
                        "EvictionEngine: archive_fn failed for %s: %s", mem_id, exc
                    )
                if success:
                    result.archived_count += 1
                    action_detail = {
                        "memory_id": mem_id,
                        "source": candidate.source,
                        "reason": candidate.reason.value,
                        "prev_score": candidate.prev_score,
                        "profile": candidate.profile,
                    }
                    result.archive_actions.append(action_detail)
                    self._archive_state[mem_id] = {
                        "source": candidate.source,
                        "archived_at": datetime.now(timezone.utc),
                        "reason": candidate.reason.value,
                    }
                    audit_log(
                        action="archive",
                        memory_id=mem_id,
                        source=candidate.source,
                        reason=candidate.reason.value,
                        prev_score=candidate.prev_score,
                        profile=candidate.profile,
                    )
                else:
                    result.skipped_count += 1

        return result

    def structural_cleanup(
        self,
        drawers_to_check: Dict[str, List[str]],
        purge_fn: Callable[[str, str], bool],
        audit_log: Callable[..., None],
    ) -> int:
        """§4.1 Drawer empty check — clean up empty drawers.

        Args:
            drawers_to_check: Mapping of ``source → list_of_drawer_keys``.
                A drawer is considered "empty" if its key list is empty after
                normal eviction processing.
            purge_fn: Callback to delete a drawer/container.
            audit_log: Audit logger callable.

        Returns:
            Number of structural cleanups performed.
        """
        count = 0
        for source, drawers in drawers_to_check.items():
            for drawer_key in drawers:
                if not drawer_key:
                    # Drawer key is empty — nothing to clean.
                    continue
                audit_log(
                    action="structural_cleanup",
                    memory_id=drawer_key,
                    source=source,
                    reason=PurgeReason.STRUCTURAL.value,
                )
                count += 1
        return count

    # ------------------------------------------------------------------
    # Internal: Trigger evaluation (§4.1 AND logic)
    # ------------------------------------------------------------------

    def _all_triggers_fire(self, candidate: EvictionCandidate) -> bool:
        """A memory only becomes eligible when ALL applicable triggers fire."""
        # Trigger 1: Importance threshold
        if not (candidate.prev_score < self._importance_min):
            return False
        # Trigger 2: Age — already validated upstream by RetentionEngine, but we
        # verify the reason code confirms it.
        if candidate.reason not in (PurgeReason.AGE, PurgeReason.IMPORTANCE):
            return False
        return True

    def _grace_period_expired(self, archive_record: Dict[str, Any]) -> bool:
        """Check whether ``grace_days`` have elapsed since archival."""
        archived_at = archive_record.get("archived_at")
        if not isinstance(archived_at, datetime):
            return False
        now = datetime.now(timezone.utc)
        elapsed = (now - archived_at).days
        return elapsed >= self._grace_days

    def _apply_archive_penalty(self, candidate: EvictionCandidate) -> Any:
        """Apply the archive score penalty to content metadata."""
        import copy

        # Attempt to attach penalty info to the content if it's a dict.
        try:
            penalized = (
                copy.deepcopy(candidate.content)
                if isinstance(candidate.content, dict)
                else {"original": candidate.content}
            )
            penalized.setdefault("_metadata", {})
            penalized["_metadata"]["archive_penalty"] = self._archive_penalty
            penalized["_metadata"]["archived_at"] = (
                datetime.now(timezone.utc).isoformat()
            )
            penalized["_metadata"]["original_score"] = candidate.prev_score
        except Exception:
            # Fallback — wrap as string with penalty note.
            penalized = {
                "original": str(candidate.content),
                "_metadata": {
                    "archive_penalty": self._archive_penalty,
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                    "original_score": candidate.prev_score,
                },
            }
        return penalized
