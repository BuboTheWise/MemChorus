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

_QUERY_MAP: Dict[DecisionPoint, str] = {
    DecisionPoint.PLANNING_START: (
        "past planning patterns architecture decisions strategy notes"
    ),
    DecisionPoint.TOOL_CALL_INTENT: (
        "tool usage history command conventions domain-specific guidance"
    ),
    DecisionPoint.POST_ACTION_COMPLETE: (
        "post-action learnings outcomes results"
    ),
    DecisionPoint.ERROR_STATE: (
        "errors recovery patterns failure modes known issues"
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


# ---------------------------------------------------------------------------
# Stub for BehavioralTrigger if the file doesn't exist yet (import-safe fallback)
# ---------------------------------------------------------------------------
if "DecisionPoint" not in globals():
    # behavioral_trigger.py is missing — provide a minimal enum so this module still loads.
    class DecisionPoint(Enum):  # type: ignore[misc, no-redef]
        ERROR_STATE = auto()
        PLANNING_START = auto()
        TOOL_CALL_INTENT = auto()
        POST_ACTION_COMPLETE = auto()

    @dataclass
    class DetectedPoint:  # type: ignore[misc, no-redef]
        type: DecisionPoint
        confidence: float
        matched_keyword: str
        text_span: Optional[str] = None

    logger.warning(
        "AutoRecallEngine: behavioral_trigger.py not found — using stub classes. "
        "TODO: import from memchorus.behavioral_trigger when available."
    )
