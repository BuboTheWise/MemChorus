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


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query templates per decision point type
# ---------------------------------------------------------------------------

# GAP P0-3 FIX (2026-07-19): Expanded query templates to cover real-world recall needs.
# The original templates were engineering-focused, missing key terms from actual stored
# memories like user preferences, project conventions, debug notes, etc.
_QUERY_MAP: Dict[DecisionPoint, str] = {
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
        "synthesis analysis findings insights patterns understanding conclusions "
        "research outcomes knowledge distillation key takeaways learnings"
    ),
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
        orchestrator: Any,          # MemoryOrchestrator (no type-signal needed)
        trigger: Any,               # BehavioralTrigger
        cache_ttl: float = 5.0,     # seconds before cache expires per DP type
    ) -> None:
        self._orchestrator = orchestrator
        self._trigger = trigger
        self._cache_ttl = cache_ttl

        # Per-type cache: maps DecisionPoint value (int) -> _CacheEntry
        self._cache: Dict[int, _CacheEntry] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_decision_point(self, decision_point: DetectedPoint) -> List[Dict[str, Any]]:
        """Retrieve context for a single *decision_point* from the orchestrator.

        Args:
            decision_point: A DetectedPoint emitted by BehavioralTrigger.

        Returns:
            Up to 3 highest-relevance search results, or ``[]`` on degradation.
        """
        dp_type = decision_point.type

        # Early-termination cache check
        cached = self._get_cached(dp_type)
        if cached is not None:
            return cached

        query = self._extract_query(dp_type)
        results = self._do_search(query)

        # Harden: enforce hard limit of 3 regardless of orchestrator output
        results = results[:3]

        # Stash in cache
        self._cache[dp_type.value] = _CacheEntry(result=list(results), timestamp=time.time())

        return results

    def fire_for_text(self, text: str) -> Dict[str, List[Dict[str, Any]]]:
        """Convenience wrapper: call BehavioralTrigger on *text*, then retrieve
        context for every detected decision point.

        Returns a dict mapping DecisionPoint enum -> list of cached contexts.
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
            logger.warning("AutoRecallEngine: no query defined for this decision point type")
            return []

        try:
            results = self._orchestrator.search(query, limit=3)
        except Exception as exc:
            logger.warning(
                "AutoRecallEngine: orchestrator search failed — returning empty list. %s", exc
            )
            return []

        if not results:
            logger.warning(
                "AutoRecallEngine: search returned no results for query '%s'", query
            )
            return []

        return results

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
