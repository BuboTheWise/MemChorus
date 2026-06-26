"""
Tests for AutoRecallEngine decision-point context injection and retrieval logic.

Acceptance criteria covered:

  AC-1: Constructor accepts MemoryOrchestrator + BehavioralTrigger.
  AC-2: on_decision_point returns deterministic queries per DP type.
  AC-3: Early termination caching prevents redundant queries within window.
  AC-4: Graceful degradation returns [] when orchestrator unavailable.
  AC-5: Result count hard-limited to 3.

Dependencies:
  - behavioral_trigger.py must exist (supplied by parent task t_6886c9dc).
"""

import os
import sys
import time
import unittest
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ---------------------------------------------------------------------------
# Imports — behavioral_trigger and orchestrator should both exist now
# ---------------------------------------------------------------------------

try:
    from memchorus.behavioral_trigger import BehavioralTrigger, DecisionPoint, DetectedPoint
except ImportError:
    # Fallback stub for environments where behavioral_trigger.py is missing
    from enum import Enum, auto
    from dataclasses import dataclass

    class DecisionPoint(Enum):  # type: ignore[misc]
        ERROR_STATE = auto()
        PLANNING_START = auto()
        TOOL_CALL_INTENT = auto()
        POST_ACTION_COMPLETE = auto()

    @dataclass
    class DetectedPoint:  # type: ignore[misc]
        type: DecisionPoint
        confidence: float
        matched_keyword: str
        text_span: Optional[str] = None

    class BehavioralTrigger:  # type: ignore[misc]
        def __init__(self) -> None: ...
        def fire(self, text: str): return []


from memchorus.auto_recall_engine import AutoRecallEngine


# ---------------------------------------------------------------------------
# Helpers — mock orchestrator that tracks search calls
# ---------------------------------------------------------------------------

class _MockOrchestrator:
    """Lightweight stand-in for MemoryOrchestrator."""

    def __init__(
        self,
        results: Optional[List[Dict[str, Any]]] = None,
        raise_on_search: bool = False,
        available: bool = True,
    ) -> None:
        self.results = results or []
        self.raise_on_search = raise_on_search
        self.available = available
        self.call_log: List[str] = []  # queries that were searched

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        if self.raise_on_search:
            raise RuntimeError("mock orchestrator unavailable")
        if not self.available:
            return []
        self.call_log.append(query)
        # Return exact mock results (caller controls content)
        return list(self.results)

    def is_available(self) -> bool:
        return self.available


def _make_decision_point(dp_type: DecisionPoint, keyword: str = "test"):
    return DetectedPoint(
        type=dp_type,
        confidence=0.8,
        matched_keyword=keyword,
        text_span=None,
    )


def _make_results(n: int) -> List[Dict[str, Any]]:
    """Create n mock search results with distinct keys for testing."""
    return [{"key": f"result_{i}", "score": float(10 - i)} for i in range(n)]


# ---------------------------------------------------------------------------
# AC-1: Constructor accepts orchestrator + trigger
# ---------------------------------------------------------------------------

class TestConstructor(unittest.TestCase):
    """AC-1: constructor takes MemoryOrchestrator (mocked) and BehavioralTrigger."""

    def test_accepts_mock_orchestrator_and_trigger(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)
        self.assertIsNotNone(engine._orchestrator)
        self.assertIsNotNone(engine._trigger)

    def test_default_cache_ttl_is_5_seconds(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)
        self.assertEqual(engine._cache_ttl, 5.0)

    def test_custom_cache_ttl(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger, cache_ttl=10.0)
        self.assertEqual(engine._cache_ttl, 10.0)


# ---------------------------------------------------------------------------
# AC-2: Deterministic query extraction per decision point type
# ---------------------------------------------------------------------------

class TestDeterministicQueryExtraction(unittest.TestCase):
    """AC-2 on_decision_point generates the right query for each DP type."""

    def test_planning_start_query(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)
        dp = _make_decision_point(DecisionPoint.PLANNING_START)
        engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 1)
        self.assertIn("planning pattern", mock_orch.call_log[0])
        self.assertIn("architecture decision", mock_orch.call_log[0])

    def test_tool_call_intent_query(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)
        dp = _make_decision_point(DecisionPoint.TOOL_CALL_INTENT)
        engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 1)
        self.assertIn("tool usage", mock_orch.call_log[0])
        self.assertIn("command convention", mock_orch.call_log[0])

    def test_post_action_complete_query(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)
        dp = _make_decision_point(DecisionPoint.POST_ACTION_COMPLETE)
        engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 1)
        self.assertIn("post-action learn", mock_orch.call_log[0])
        self.assertIn("outcome", mock_orch.call_log[0])

    def test_error_state_query(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)
        dp = _make_decision_point(DecisionPoint.ERROR_STATE)
        engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 1)
        self.assertIn("error", mock_orch.call_log[0])
        self.assertIn("recovery pattern", mock_orch.call_log[0])

    def test_each_dp_type_gets_different_query(self) -> None:
        """Verify all four DP types produce distinct queries."""
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)

        for dp_type in DecisionPoint:  # type: ignore[attr-defined]
            engine.on_decision_point(_make_decision_point(dp_type))

        queries = mock_orch.call_log
        assert len(queries) == 4, f"Expected 4 queries, got {len(queries)}"
        self.assertEqual(len(set(queries)), 4, "All queries should be distinct")


# ---------------------------------------------------------------------------
# AC-3: Early termination caching (rapid-fire within 5-second window)
# ---------------------------------------------------------------------------

class TestEarlyTerminationCaching(unittest.TestCase):
    """AC-3 same DP type within cache_ttl returns cached result without another search."""

    def test_rapid_fire_returns_cache_hit(self) -> None:
        mock_orch = _MockOrchestrator([{"key": "result_a", "score": 0.9}])
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger, cache_ttl=5.0)

        dp = _make_decision_point(DecisionPoint.PLANNING_START)

        # First call — should go to orchestrator
        r1 = engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 1)
        self.assertEqual(len(r1), 1)
        self.assertEqual(r1[0]["key"], "result_a")

        # Second call within TTL — should return cached, not call search again
        r2 = engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 1)  # still only 1 call
        self.assertEqual(r2, r1)

    def test_cache_miss_after_ttl_expires(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger, cache_ttl=0.1)

        dp = _make_decision_point(DecisionPoint.ERROR_STATE)

        # First call
        engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 1)

        # Wait for TTL to expire
        time.sleep(0.15)

        # Second call — should miss cache and search again
        engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 2)

    def test_different_dp_types_dont_share_cache(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger, cache_ttl=10.0)

        ep = _make_decision_point(DecisionPoint.PLANNING_START)
        e_err = _make_decision_point(DecisionPoint.ERROR_STATE)

        engine.on_decision_point(ep)
        engine.on_decision_point(e_err)

        # Both DP types should have hit the search (2 distinct calls)
        self.assertEqual(len(mock_orch.call_log), 2)

    def test_clear_cache_works(self) -> None:
        mock_orch = _MockOrchestrator()
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger, cache_ttl=10.0)

        dp = _make_decision_point(DecisionPoint.TOOL_CALL_INTENT)

        engine.on_decision_point(dp)
        self.assertEqual(len(mock_orch.call_log), 1)

        engine.clear_cache()
        engine.on_decision_point(dp)

        # After clearing cache, should search again
        self.assertEqual(len(mock_orch.call_log), 2)


# ---------------------------------------------------------------------------
# AC-4: Graceful degradation when orchestrator unavailable
# ---------------------------------------------------------------------------

class TestGracefulDegradation(unittest.TestCase):
    """AC-4 if orchestrator raises or returns empty, engine returns [] without crashing."""

    def test_raises_exception_returns_empty(self) -> None:
        mock_orch = _MockOrchestrator(raise_on_search=True)
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)

        dp = _make_decision_point(DecisionPoint.PLANNING_START)
        result = engine.on_decision_point(dp)
        self.assertEqual(result, [])

    def test_no_results_returns_empty(self) -> None:
        mock_orch = _MockOrchestrator(results=[])
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)

        dp = _make_decision_point(DecisionPoint.PLANNING_START)
        result = engine.on_decision_point(dp)
        self.assertEqual(result, [])

    def test_unavailable_orchestrator_returns_empty(self) -> None:
        mock_orch = _MockOrchestrator(available=False)
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)

        dp = _make_decision_point(DecisionPoint.TOOL_CALL_INTENT)
        result = engine.on_decision_point(dp)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# AC-5: Result count hard-limited to 3
# ---------------------------------------------------------------------------

class TestResultCountLimit(unittest.TestCase):
    """AC-5 returned results are capped at 3 regardless of orchestrator output."""

    def test_more_than_three_results_are_capped(self) -> None:
        """Orchestrator returns 10 results — engine should return max 3."""
        fake_results = [
            {"key": f"big_{i}", "score": float(i)}
            for i in range(10)
        ]
        mock_orch = _MockOrchestrator(results=fake_results)
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)

        dp = _make_decision_point(DecisionPoint.POST_ACTION_COMPLETE)
        result = engine.on_decision_point(dp)
        self.assertEqual(len(result), 3)

    def test_exactly_three_results_unchanged(self) -> None:
        fake_results = [{"key": f"exact_{i}"} for i in range(3)]
        mock_orch = _MockOrchestrator(results=fake_results)
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)

        dp = _make_decision_point(DecisionPoint.PLANNING_START)
        result = engine.on_decision_point(dp)
        self.assertEqual(len(result), 3)

    def test_fewer_than_three_returned_as_is(self) -> None:
        fake_results = [{"key": "only_one"}]
        mock_orch = _MockOrchestrator(results=fake_results)
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)

        dp = _make_decision_point(DecisionPoint.ERROR_STATE)
        result = engine.on_decision_point(dp)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# fire_for_text convenience method
# ---------------------------------------------------------------------------

class TestFireForText(unittest.TestCase):
    """Test the fire_for_text convenience wrapper."""

    def test_fire_for_text_calls_trigger_and_retrieves(self) -> None:
        mock_orch = _MockOrchestrator(results=[{"key": "ctx1"}])
        trigger = BehavioralTrigger()  # type: ignore[operator]
        engine = AutoRecallEngine(mock_orch, trigger)

        result = engine.fire_for_text(
            "Something went wrong with the API call. Next I will call search."
        )
        self.assertIsInstance(result, dict)
        # Should have ERROR_STATE and TOOL_CALL_INTENT entries
        self.assertIn("ERROR_STATE", result)
        self.assertIn("TOOL_CALL_INTENT", result)

    def test_fire_for_text_no_trigger_returns_empty(self) -> None:
        mock_orch = _MockOrchestrator()
        engine = AutoRecallEngine(mock_orch, trigger=None)

        result = engine.fire_for_text("Some text")
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
