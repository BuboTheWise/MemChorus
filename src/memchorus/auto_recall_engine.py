"""
AutoRecallEngine: Automatic context injection at decision points.

Wires behavioral enforcement into the orchestrator search pipeline by detecting
decision points from BehavioralTrigger and automatically querying MemoryOrchestrator
search to inject retrieved context inline — no manual recall needed.

Decision point -> query mapping (deterministic):

  PLANNING_START       -> "past planning patterns architecture decisions strategy"
  TOOL_CALL_INTENT     -> "tool usage history command conventions domain guidance"
  POST_ACTION_COMPLETE -> "post-action learnings outcomes results"
  ERROR_STATE          -> "errors recovery patterns failure modes known issues"

Acceptance criteria:

  AC-1: Constructor accepts MemoryOrchestrator + BehavioralTrigger.
  AC-2: on_decision_point returns deterministic queries per DP type.
  AC-3: Early termination caching prevents redundant queries within window.
  AC-4: Graceful degradation returns [] when orchestrator unavailable.
  AC-5: Result count hard-limited to 3.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional

from memchorus.behavioral_trigger import DecisionPoint, DetectedPoint  # type: ignore[import-not-found]
from memchorus.recall_config import RECALL_CONFIG  # type: ignore[import-not-found]


logger = logging.getLogger(__name__)

# Item budget K for *implied* (decision-point / auto) recall — read from the
# single named config root (spec §4.3).  Decision-point recall is by
# definition "implied", so its hard cap is ``k_implied`` (default 3).  This is
# the ONE place the implied item budget is named in this module; the literal
# ``3`` that used to be scattered through the limit logic below now derives
# from here, so drift is caught at a single site (see
# ``tests/test_recall_config_root.py``).
_IMPLIED_ITEM_BUDGET: int = RECALL_CONFIG.k_implied


# ---------------------------------------------------------------------------
# MC-001 recursion guard sentinel (module-level)
# ---------------------------------------------------------------------------

_REC_GUARD: bool = False  # flipped during enforce() to catch re-entry


# ---------------------------------------------------------------------------
# Query templates per decision point type
# ---------------------------------------------------------------------------

# GAP P0-3 FIX (2026-07-19): Expanded query templates to cover real-world recall needs.
# The original templates were engineering-focused, missing key terms from actual stored
# memories like user preferences, project conventions, debug notes, etc.
_QUERY_MAP: Dict[DecisionPoint, Optional[str]] = {
    DecisionPoint.PLANNING_START: (
        "past planning patterns architecture decisions strategy notes "
        "project organization conventions documentation standards workflow"
    ),
    DecisionPoint.TOOL_CALL_INTENT: (
        "tool usage history command conventions domain-specific guidance "
        "preferences user context setup configuration environment "
        "debug findings verification testing procedures scripts"
    ),
    DecisionPoint.POST_ACTION_COMPLETE: (
        "post-action learnings outcomes results decisions made changes "
        "completed tasks progress milestones reviews improvements"
    ),
    DecisionPoint.ERROR_STATE: (
        "errors recovery patterns failure modes known issues bugs fixes "
        "troubleshooting diagnostic root cause debugging steps workarounds"
    ),
    DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION: (
        "synthesis analysis findings key insight understanding learned important "
        "patterns review summary conclusions takeaways documentation research"
    ),
    # IMPL #163.2 — sentinel: PROJECT_START is a *keyed* record lookup, not a
    # semantic query. ``None`` means "resolve the project:<name> record via
    # orchestrator.resolve_project_record(); do not run orchestrator.search()".
    # The dispatch branch in ``on_decision_point`` catches this DP **before**
    # ``_extract_query`` is consulted, so the sentinel is defensive only
    # (spec §4.2).
    DecisionPoint.PROJECT_START: None,
}


# ---------------------------------------------------------------------------
# Cache entry for early-termination caching
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    result: List[Dict[str, Any]]
    timestamp: float  # time.time() when the cache was populated


# ---------------------------------------------------------------------------
# AutoRecallEngine
# ---------------------------------------------------------------------------


class AutoRecallEngine:
    """Automatically queries memory sources at detected decision points.

    Constructor arguments:

      orchestrator (MemoryOrchestrator): source for ``search(query, limit)``
      trigger (BehavioralTrigger): used only by the public ``fire_for_text``
        convenience method — it delegates to ``trigger.fire(text)`` internally.
    """

    def __init__(
        self,
        orchestrator: Any,  # MemoryOrchestrator (no type-signal needed)
        trigger: Any,  # BehavioralTrigger
        cache_ttl: float = 5.0,  # seconds before cache expires per DP type
        aug_fetcher: Optional[Any] = None,
        aug_config: Optional[Any] = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._trigger = trigger
        self._cache_ttl = cache_ttl
        # Pillar 3 (issue #225, spec §6) — optional augmentation fetcher.
        # Injected by the caller (usually the MCP transport bridge); when None
        # the augmentation path degrades to an empty reserved list.
        self._aug_fetcher = aug_fetcher
        self._aug_config = aug_config

        # Per-type cache: maps DecisionPoint value (int) -> _CacheEntry
        self._cache: Dict[int, _CacheEntry] = {}

        # MC-001 re-entry guard — flipped during enforce() to block recursive calls
        self._in_enforcement_recall = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_decision_point(self, decision_point: DetectedPoint) -> List[Dict[str, Any]]:
        """Retrieve context for a single *decision_point* from the orchestrator.

        Args:
            decision_point: A DetectedPoint emitted by BehavioralTrigger.

        Returns:
            Up to the implied item budget (K = ``RECALL_CONFIG.k_implied``,
            default 3) highest-relevance search results, or ``[]`` on degradation.
        """
        global _REC_GUARD

        # MC-001 guard: block recursive entry during enforcement cycle
        if _REC_GUARD or self._in_enforcement_recall:
            logger.debug(
                "AutoRecallEngine: recursion guard blocked re-entry for %s",
                decision_point.type.name,
            )
            return []

        dp_type = decision_point.type

        # Early-termination cache check
        cached = self._get_cached(dp_type)
        if cached is not None:
            return cached

        # Activate recursion guard before doing work
        _REC_GUARD = True
        self._in_enforcement_recall = True
        try:
            # IMPL #163.2 (spec §4.1) — PROJECT_START is a *keyed* record
            # resolution, NOT a free-text semantic query.  Dispatch it to the
            # orchestrator's ``resolve_project_record(name)`` API so the ranked
            # search path is never consulted with the ``None`` sentinel (the
            # bug this branch prevents: ``_do_search(None)`` falling through
            # to ``orchestrator.search(None, limit=_IMPLIED_ITEM_BUDGET)``).
            if dp_type is DecisionPoint.PROJECT_START:
                results = self._do_resolve_project_record(decision_point)
            else:
                query = self._extract_query(dp_type)
                results = self._do_search(query)

            # Harden: enforce the implied item budget (K) regardless of
            # orchestrator output.  K for implied recall is the named config
            # key (spec §4.3), not a scattered literal.
            results = results[:_IMPLIED_ITEM_BUDGET]

            # Stash in cache
            self._cache[dp_type.value] = _CacheEntry(
                result=list(results), timestamp=time.time()
            )

            return results
        finally:
            # Always deactivate guard, even on exception
            _REC_GUARD = False
            self._in_enforcement_recall = False

    def reserved_slots_headroom(self, *, explicit: bool = False) -> int:
        """Pillar 3 (spec §6.2) — reserved-slot headroom for this recall mode.

        Returns the number of K-budget slots that the pillar-3 augmentation
        channel may claim, drawn from the shared K (implied/selected) budget:

          implied  →  ``RECALL_CONFIG.reserved_slots_implied``  (1)
          explicit →  ``RECALL_CONFIG.reserved_slots_explicit`` (2)

        The pillar-1 select() path passes this value in as ``reserved_slots``
        so the main ranking path holds that headroom for pillar 3 (additive,
        spec §6.4: reserved slots reduce the slots available to main ranked
        items, never displacing them by out-scoring).  Deterministic — reads
        only from the named config root, no defaults duplicated here.
        """
        from memchorus.augmentation import reserved_budget

        return reserved_budget(
            explicit=explicit,
            config=self._aug_config or None,
        )

    def augment(
        self,
        task: Dict[str, Any],
        *,
        fetcher: Optional[Any] = None,
        explicit: bool = False,
    ) -> List[Dict[str, Any]]:
        """Execute pillar-3 (spec §6) reserved augmentation for *task*.

        Returns a budget-capped list of reserved-item dicts (each stamped
        ``source``/``provenance``/``section``/``reserved=True``).  Graceful
        degradation per spec §6.7: a missing fetcher, a missing orchestrator,
        or a per-source fetch failure all contribute an empty list for that
        source — never an exception propagating to the caller.

        Args:
            task:      task-signal dict (``kind`` / ``domain`` / ``text`` /
                       ``explicit``).  The same dict a recall pipeline would
                       build for pillar 1/2; pillar 3 consumes it read-only.
            fetcher:   override for the injected ``aug_fetcher`` (test hook).
            explicit:  True for explicit-recall budget (2), False for implied (1).
        """
        from memchorus.augmentation import augment as _augment

        eff_fetcher = fetcher if fetcher is not None else self._aug_fetcher
        if isinstance(task, dict) and "explicit" in task:
            explicit_eff = bool(task["explicit"])
        else:
            explicit_eff = explicit
        try:
            items = _augment(
                task=task,
                fetcher=eff_fetcher,
                explicit=explicit_eff,
                config=self._aug_config or None,
            )
        except Exception as exc:
            logger.warning(
                "AutoRecallEngine.augment: pillar-3 pipeline raised — "
                "returning empty list. %s",
                exc,
            )
            return []
        return [it.to_dict() for it in items]

    def fire_for_text(self, text: str) -> Dict[str, List[Dict[str, Any]]]:
        """Convenience wrapper: call BehavioralTrigger on *text*, then retrieve
        context for every detected decision point.

        Returns a dict keyed by decision-point type *name* (str, via
        ``point.type.name``) -> the result list for that point.
        """
        if self._trigger is None:
            logger.warning("AutoRecallEngine has no trigger; cannot fire_for_text")
            return {}

        points = self._trigger.fire(text)
        # Group by type so rapid-fire returns consistent cache hits
        output: Dict[str, List[Dict[str, Any]]] = {}
        for point in points:
            key = point.type.name
            output[key] = self.on_decision_point(point)
        return output

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_query(self, dp_type: DecisionPoint) -> str:
        """Return the deterministic search query for *dp_type*."""
        return _QUERY_MAP.get(dp_type, "")

    def _do_search(self, query: str) -> List[Dict[str, Any]]:
        """Call orchestrator.search() with graceful degradation."""
        if query == "":
            logger.warning(
                "AutoRecallEngine: no query defined for this decision point type"
            )
            return []

        try:
            results = self._orchestrator.search(query, limit=_IMPLIED_ITEM_BUDGET)
        except Exception as exc:
            logger.warning(
                "AutoRecallEngine: orchestrator search failed — returning empty list. %s",
                exc,
            )
            return []

        if not results:
            logger.warning(
                "AutoRecallEngine: search returned no results for query '%s'", query
            )
            return []

        return results

    def _do_resolve_project_record(self, point: DetectedPoint) -> List[Dict[str, Any]]:
        """Resolve the *keyed* project record for a PROJECT_START decision point.

        Returns the §3.3 record contract as a **1-element list** so the caller's
        ``List[Dict]`` shape and the ``[:_IMPLIED_ITEM_BUDGET]`` hard-limit are
        preserved.  Graceful degradation: a missing/blank project name, a missing
        orchestrator API, or any exception all return ``[]`` — never an escape
        (spec §4.2/§4.5).
        """
        name = getattr(point, "project_name", None)
        if not name:
            # Fall back to the matched keyword (e.g. a project name surfaced in
            # a session-start banner), stripping any ``project:`` prefix.
            name = getattr(point, "matched_keyword", None)
        if not name or not str(name).strip():
            logger.warning(
                "AutoRecallEngine: PROJECT_START has no project_name to resolve — "
                "returning empty list (no keyed lookup possible)"
            )
            return []
        name = str(name).strip()
        # resolve_project_record strips its own ``project:`` prefix, but it can
        # also accept a bare slug (spec §3.1).  Ensure a bare slug is keyed so
        # normalize_project_key always receives the canonical ``project:<slug>``
        # form.  A name that is *already* keyed is left untouched.
        if name and not name.lower().startswith("project:"):
            name = "project:" + name

        resolver = getattr(self._orchestrator, "resolve_project_record", None)
        if not callable(resolver):
            logger.warning(
                "AutoRecallEngine: orchestrator has no resolve_project_record API — "
                "degrading PROJECT_START to empty list"
            )
            return []
        try:
            record = resolver(name)
        except Exception as exc:
            logger.warning(
                "AutoRecallEngine: resolve_project_record failed for '%s' — "
                "returning empty list. %s",
                name,
                exc,
            )
            return []
        if not isinstance(record, dict):
            logger.warning(
                "AutoRecallEngine: resolve_project_record returned %s (expected dict) — "
                "degrading to empty list",
                type(record).__name__,
            )
            return []
        return [record]

    def _get_cached(self, dp_type: DecisionPoint) -> Optional[List[Dict[str, Any]]]:
        """Return cached context if the same DP fired within cache_ttl; else None."""
        entry = self._cache.get(dp_type.value)
        if entry is None:
            return None
        if time.time() - entry.timestamp < self._cache_ttl:
            return list(entry.result)  # defensive copy
        # Expired — remove stale entry
        del self._cache[dp_type.value]
        return None

    def clear_cache(self) -> None:
        """Remove all cached entries."""
        self._cache.clear()


# NOTE: No stub/try-except fallback around the top-level import on line 30.
# The unguarded ``from memchorus.behavioral_trigger import DecisionPoint, DetectedPoint``
# either succeeds (putting DecisionPoint into globals, so this block is dead code) or raises
# ImportError immediately and aborts module loading — the if-statement below never executes
# because Python never reaches it when the import fails.  A stub here would give false
# confidence: the module would appear to load but all decision-point logic would silently use
# locally-defined enums with no real BehavioralTrigger wiring, defeating enforcement.
# The correct fix for missing behavior_trigger is: ensure behavioral_trigger.py ships
# correctly alongside this module; don't silently degrade enforcement to stub classes in-use.
# If a packaging scenario ever requires graceful degradation (e.g., wheels that omit optional
# dependencies), replace the top-level import with ``try … except ImportError`` and gate the
# entire class behind an availability check, not a stub enum.
