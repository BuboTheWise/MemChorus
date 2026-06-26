"""
Tests for BehavioralEnforcementManager — the wiring layer connecting trigger → recall + storage.

Acceptance criteria covered (from spec BE-1 through BE-5):
  BE-1: Constructor accepts MemoryOrchestrator (or None for degradation).
  BE-2: enforce(text) auto-fires through trigger -> recall -> storage pipeline.
  BE-3: Individual engines can be toggled on/off independently.
  BE-4: Graceful degradation when orchestrator is unavailable or None.
  BE-5: Returns structured EnforcementResult with per-step outcomes.

Uses Mock objects to decouple from real orchestrator implementation.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.enforcement_manager import (
    BehavioralEnforcementManager,
    EnforcementResult,
)
from memchorus.behavioral_trigger import DecisionPoint, DetectedPoint


# ---------------------------------------------------------------------------
# Mock orchestrator helpers
# ---------------------------------------------------------------------------

class _MockOrchestrator:
    """Minimal mock that records save calls."""

    def __init__(self) -> None:
        self.saved_calls = []  # [(key, value)]

    def save(self, key: str, value: dict) -> bool:
        self.saved_calls.append((key, value))
        return True

    def retrieve(self, key: str):
        return None


def _make_manager(orch=None):
    if orch is None:
        orch = _MockOrchestrator()
    return BehavioralEnforcementManager(orchestrator=orch)


# ---------------------------------------------------------------------------
# BE-1 & BE-5: Constructor exists, accepts orchestrator/None, returns structured result
# ---------------------------------------------------------------------------

class TestEnforcementManagerInit(unittest.TestCase):
    """BE-1: class exists, constructor accepts orchestrator or None."""

    def test_class_exists(self) -> None:
        self.assertIsNotNone(BehavioralEnforcementManager)

    def test_constructor_with_mock(self) -> None:
        mgr = _make_manager()
        self.assertIsNotNone(mgr._orchestrator)
        self.assertTrue(mgr.is_available)

    def test_constructor_with_none(self) -> None:
        mgr = BehavioralEnforcementManager(orchestrator=None)
        self.assertIsNone(mgr._orchestrator)
        self.assertFalse(mgr.is_available)


# ---------------------------------------------------------------------------
# BE-2: Full pipeline fires through trigger -> recall -> storage
# ---------------------------------------------------------------------------

class TestFullPipeline(unittest.TestCase):
    """BE-2: enforce() runs the complete pipeline."""

    def test_planning_text_triggers_pipeline(self) -> None:
        mgr = _make_manager()
        result = mgr.enforce("I need to implement a new feature for memory management")

        self.assertIsInstance(result, EnforcementResult)
        self.assertGreaterEqual(result.triggered_points, 1)
        self.assertIsNotNone(result.storage_outcome)
        self.assertGreater(result.timing_ms, 0.0)

    def test_planning_text_gets_recall_and_storage(self) -> None:
        orch = _MockOrchestrator()
        mgr = BehavioralEnforcementManager(orchestrator=orch)

        # "need to implement" matches the PLANNING_START trigger pattern
        result = mgr.enforce("I need to implement a refactoring of the entire system")

        # Should detect PLANNING_START (at least 1 trigger point)
        self.assertGreater(result.triggered_points, 0)
        # Storage should have fired
        assert result.storage_outcome is not None
        self.assertIn("saved", result.storage_outcome)

    def test_error_text_triggers_pipeline(self) -> None:
        mgr = _make_manager()
        result = mgr.enforce("The deployment failed with an error in the pipeline")

        self.assertGreater(result.triggered_points, 0)

    def test_irrelevant_text_no_trigger(self) -> None:
        orch = _MockOrchestrator()
        mgr = BehavioralEnforcementManager(orchestrator=orch)
        result = mgr.enforce("12345 abcdef random text")

        self.assertEqual(result.triggered_points, 0)


# ---------------------------------------------------------------------------
# BE-3: Toggle recall and storage independently
# ---------------------------------------------------------------------------

class TestToggleEngines(unittest.TestCase):
    """BE-3: individual engines can be toggled on/off independently."""

    def test_disable_recall(self) -> None:
        orch = _MockOrchestrator()
        mgr = BehavioralEnforcementManager(orchestrator=orch)
        mgr.enable_recall(False)

        result = mgr.enforce("I need to implement a new module")

        # Trigger should still fire (trigger is unaffected by recall toggle)
        self.assertGreater(result.triggered_points, 0)
        # Recall context should be empty since disabled
        self.assertEqual(len(result.recall_context), 0)

    def test_disable_storage(self) -> None:
        orch = _MockOrchestrator()
        mgr = BehavioralEnforcementManager(orchestrator=orch)
        mgr.enable_storage(False)

        result = mgr.enforce("I learned something valuable today")

        # Storage outcome should be None since disabled
        self.assertIsNone(result.storage_outcome)

    def test_disable_both(self) -> None:
        orch = _MockOrchestrator()
        mgr = BehavioralEnforcementManager(orchestrator=orch)
        mgr.enable_recall(False)
        mgr.enable_storage(False)

        result = mgr.enforce("I realized the system had an issue")

        # Trigger still works but recall and storage are off
        self.assertEqual(len(result.recall_context), 0)
        self.assertIsNone(result.storage_outcome)


# ---------------------------------------------------------------------------
# BE-4: Graceful degradation when orchestrator is unavailable or None
# ---------------------------------------------------------------------------

class TestGracefulDegradation(unittest.TestCase):
    """BE-4: pipeline handles missing/broken orchestrator."""

    def test_none_orchestrator_no_crash(self) -> None:
        mgr = BehavioralEnforcementManager(orchestrator=None)
        result = mgr.enforce("I decided to use the new approach completely")

        self.assertIsInstance(result, EnforcementResult)
        # Should not crash; storage outcome may or may not have key depending on engine behavior
        self.assertGreaterEqual(result.timing_ms, 0.0)

    def test_orchestrator_that_fails_returns_no_error_in_result(self) -> None:
        mock_orch = MagicMock()
        mock_orch.save.return_value = False

        mgr = BehavioralEnforcementManager(orchestrator=mock_orch)
        result = mgr.enforce("The benchmark achieved 95% accuracy")

        # Pipeline should still complete without raising
        self.assertIsInstance(result, EnforcementResult)


# ---------------------------------------------------------------------------
# BE-2/BE-5: Timing and structured results
# ---------------------------------------------------------------------------

class TestEnforcementResult(unittest.TestCase):
    """BE-5: structured result contains expected fields."""

    def test_result_has_all_fields(self) -> None:
        orch = _MockOrchestrator()
        mgr = BehavioralEnforcementManager(orchestrator=orch)

        result = mgr.enforce("I need to implement something")

        self.assertIsInstance(result, EnforcementResult)
        self.assertTrue(hasattr(result, "triggered_points"))
        self.assertTrue(hasattr(result, "recall_context"))
        self.assertTrue(hasattr(result, "storage_outcome"))
        self.assertTrue(hasattr(result, "timing_ms"))
        self.assertTrue(hasattr(result, "errors"))

    def test_timing_positive(self) -> None:
        orch = _MockOrchestrator()
        mgr = BehavioralEnforcementManager(orchestrator=orch)

        result = mgr.enforce("I finished the task and achieved the goal")
        self.assertGreater(result.timing_ms, 0.0)


# ---------------------------------------------------------------------------
# Integration: full orchestrator pipeline exercises save calls
# ---------------------------------------------------------------------------

class TestIntegration(unittest.TestCase):
    """End-to-end integration with mock orchestrator."""

    def test_storage_saves_via_mock_orchestrator(self) -> None:
        orch = _MockOrchestrator()
        mgr = BehavioralEnforcementManager(orchestrator=orch)

        text = "I learned that the benchmark achieved 95% accuracy and success was verified"
        result = mgr.enforce(text)

        # If storage is enabled (default), orchestrator.save should have been called
        self.assertGreater(len(orch.saved_calls), 0,
                          "Orchestrator should have saved at least one outcome when text is significant")


if __name__ == "__main__":
    unittest.main()
