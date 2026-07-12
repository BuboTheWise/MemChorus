"""
Tests for AutoStorageEngine automatic post-action outcome capture.

Acceptance criteria covered:
  AC-1: AutoStorageEngine class exists in src/memchorus/auto_storage_engine.py
  AC-2: Constructor takes a MemoryOrchestrator (or orchestrator-like) instance
  AC-3: capture_outcome(text, outcome_type="automatic") returns dict with saved/key/significance
  AC-4: Significance detection classifies into LEARNING, MISTAKE, DECISION, RESULT
  AC-5: Trivial content filtering rejects short/meaningless text (returns below_significance_threshold)
  AC-6: Deduplication merges within 30-second window (>60% similarity)
  AC-7: Empty/None orchestrator handles gracefully without crash

Uses Mock objects to decouple from real orchestrator implementation.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.auto_storage_engine import (
    AutoStorageEngine,
    CaptureResult,
    SignificanceCategory,
)


# ---------------------------------------------------------------------------
# Mock orchestrator helpers
# ---------------------------------------------------------------------------


class _MockOrchestrator:
    """Minimal mock that records save calls."""

    def __init__(self) -> None:
        self.saved_calls: list = []  # [(key, value)]

    def recommended_sources(
        self, write_type: str = "general", max_results: int = 3
    ) -> list[str]:
        """Stubs the new B-2 method so existing tests continue to work."""
        return ["mock"]

    def save(self, key: str, value: dict, **kwargs) -> bool:
        self.saved_calls.append((key, value))
        return True

    def retrieve(self, key: str):
        return None


def _make_engine(orch=None) -> AutoStorageEngine:
    if orch is None:
        orch = _MockOrchestrator()
    return AutoStorageEngine(orchestrator=orch)


# ---------------------------------------------------------------------------
# AC-1 & AC-2: Class existence and constructor
# ---------------------------------------------------------------------------


class TestAutoStorageEngineInit(unittest.TestCase):
    """AC-1/AC-2: class exists, constructor accepts orchestrator."""

    def test_class_exists(self) -> None:
        self.assertIsNotNone(AutoStorageEngine)

    def test_constructor_with_mock_orchestrator(self) -> None:
        engine = _make_engine()
        self.assertIsNotNone(engine.orchestrator)
        self.assertEqual(engine.dedup_window_seconds, 30.0)
        self.assertEqual(engine.dedup_similarity_threshold, 0.6)

    def test_constructor_custom_dedup_params(self) -> None:
        mock_orch = _MockOrchestrator()
        engine = AutoStorageEngine(
            orchestrator=mock_orch,
            dedup_window_seconds=60.0,
            dedup_similarity_threshold=0.8,
        )
        self.assertEqual(engine.dedup_window_seconds, 60.0)
        self.assertEqual(engine.dedup_similarity_threshold, 0.8)


# ---------------------------------------------------------------------------
# AC-4: Significance detection for all four categories
# ---------------------------------------------------------------------------


class TestSignificanceDetection(unittest.TestCase):
    """AC-4: each category detected on its correct keywords."""

    def _assert_category(self, engine: AutoStorageEngine, text: str, expected: SignificanceCategory) -> None:
        result = engine.capture_outcome(text)
        self.assertTrue(result["saved"])
        self.assertEqual(SignificanceCategory[result["significance"].upper()], expected)

    def test_learning_detected_on_learned(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "I learned that the API returns 404 now", SignificanceCategory.LEARNING)

    def test_learning_detected_on_realized(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "I realized we need to handle edge cases", SignificanceCategory.LEARNING)

    def test_learning_detected_on_understood(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "The user understood the issue was authentication", SignificanceCategory.LEARNING)

    def test_learning_detected_on_found_that(self) -> None:
        engine = _make_engine()
        self._assert_category(
            engine,
            "The script found that the file wasn't readable",
            SignificanceCategory.LEARNING,
        )

    def test_mistake_detected_on_went_wrong(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "Something went wrong with the deployment pipeline", SignificanceCategory.MISTAKE)

    def test_mistake_detected_on_wrong_approach(self) -> None:
        engine = _make_engine()
        self._assert_category(
            engine,
            "The regex approach was the wrong approach for this data format",
            SignificanceCategory.MISTAKE,
        )

    def test_mistake_detected_on_should_have(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "I should have validated the input first", SignificanceCategory.MISTAKE)

    def test_mistake_detected_on_incorrectly(self) -> None:
        engine = _make_engine()
        self._assert_category(
            engine,
            "The model was incorrectly configured for production",
            SignificanceCategory.MISTAKE,
        )

    def test_decision_detected_on_decided(self) -> None:
        agent = _make_engine()
        self._assert_category(agent, "We decided to use the new API endpoint", SignificanceCategory.DECISION)

    def test_decision_detected_on_chose(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "I chose to retry with exponential backoff", SignificanceCategory.DECISION)

    def test_decision_detected_on_got_with(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "The team decided to go with option B", SignificanceCategory.DECISION)

    def test_result_detected_on_result(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "The result of the benchmark was 99.2% accuracy", SignificanceCategory.RESULT)

    def test_result_detected_on_success(self) -> None:
        engine = _make_engine()
        self._assert_category(engine, "The deployment was a success with zero downtime", SignificanceCategory.RESULT)


# ---------------------------------------------------------------------------
# AC-5: Trivial content filtering
# ---------------------------------------------------------------------------


class TestTrivialFiltering(unittest.TestCase):
    """AC-5: short text and meaningless confirmations are skipped."""

    def _assert_trivial(self, engine: AutoStorageEngine, text: str) -> None:
        result = engine.capture_outcome(text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "below_significance_threshold")

    def test_too_short_text_is_trivial(self) -> None:
        engine = _make_engine()
        self._assert_trivial(engine, "ok")
        self._assert_trivial(engine, "1234567890123456789")  # exactly 19 chars

    def test_ok_is_trivial(self) -> None:
        engine = _make_engine()
        self._assert_trivial(engine, "ok it's done")

    def test_done_alone_at_end_is_trivial(self) -> None:
        engine = _make_engine()
        self._assert_trivial(engine, "Yep, we're done")

    def test_yep_is_trivial(self) -> None:
        engine = _make_engine()
        self._assert_trivial(engine, "yep")

    def test_single_word_is_trivial(self) -> None:
        engine = _make_engine()
        self._assert_trivial(engine, "hello world!")  # two words but trivial-ish
        # Actually verify the trivial path fires (it won't for 2+ word >6 chars each):
        
    def test_significant_text_saves(self) -> None:
        engine = _make_engine()
        result = engine.capture_outcome(
            "I learned that the new endpoint returns JSON instead of XML"
        )
        self.assertTrue(result["saved"])
        self.assertIsNotNone(result["key"])


# ---------------------------------------------------------------------------
# AC-6: Deduplication merging within time window
# ---------------------------------------------------------------------------


class TestDeduplication(unittest.TestCase):
    """AC-6: similar content within the dedup window merges into existing key."""

    def test_dedup_merges_similar_text(self) -> None:
        engine = _make_engine()
        
        # First capture
        result1 = engine.capture_outcome(
            "I learned that the API endpoint returns an array",
        )
        self.assertTrue(result1["saved"])

        # Similar text within 30-second window (default)
        time.sleep(0.5)  # brief but keeps same dedup window
        
        result2 = engine.capture_outcome(
            "I learned that the API endpoint returns an array with items",
        )
        
        # Should merge: check reason indicates it was merged
        self.assertTrue(result2["saved"])
        self.assertEqual(result2["reason"], "merged_into_existing")

    def test_long_gap_skips_dedup(self) -> None:
        engine = AutoStorageEngine(
            _MockOrchestrator(),
            dedup_window_seconds=0.1,  # very short window
        )
        
        result1 = engine.capture_outcome("I learned that the first test passed successfully and we can")
        time.sleep(0.2)  # exceeds 0.1s window
        
        result2 = engine.capture_outcome("I learned that the second test also passed successfully and we can")
        
        self.assertTrue(result2["saved"])
        self.assertNotEqual(result1["key"], result2["key"])


# ---------------------------------------------------------------------------
# AC-7: Empty/None orchestrator handles gracefully
# ---------------------------------------------------------------------------


class TestEmptyOrchestrator(unittest.TestCase):
    """AC-7: graceful degradation when orchestrator is None or broken."""

    def test_none_orchestrator_does_not_crash(self) -> None:
        # No valid save method, but should not raise
        engine = AutoStorageEngine(None)  # type: ignore[arg-type]
        result = engine.capture_outcome(
            "I realized that something significant happened during testing"
        )
        self.assertFalse(result["saved"])

    def test_orchestrator_with_no_save_method(self) -> None:
        class NoSaveOrchestrator:  # type: ignore[valid-type]
            pass
        
        engine = AutoStorageEngine(NoSaveOrchestrator())
        result = engine.capture_outcome("I learned that the system achieved high availability")
        self.assertFalse(result["saved"])

    def test_orchestrator_that_returns_false(self) -> None:
        mock = MagicMock()
        mock.save.return_value = False
        
        engine = AutoStorageEngine(mock)
        result = engine.capture_outcome(
            "The benchmark achieved 99.2% accuracy on the validation set and success"
        )
        self.assertFalse(result["saved"])
        self.assertIsNotNone(result["key"])  # key generated even if save failed


# ---------------------------------------------------------------------------
# AC-3: capture_outcome return structure
# ---------------------------------------------------------------------------


class TestCaptureResultStructure(unittest.TestCase):
    """AC-3: capture_outcome returns dict with expected keys."""

    def test_returns_dict_with_required_keys(self) -> None:
        engine = _make_engine()
        result = engine.capture_outcome(
            "I learned that the benchmark achieved 99% accuracy and the success was verified"
        )
        
        self.assertIsInstance(result, dict)
        for key in ("saved", "key", "significance", "outcome_type"):
            self.assertIn(key, result)

    def test_outcome_type_propagated(self) -> None:
        engine = _make_engine()
        result = engine.capture_outcome("I realized something important happened and we achieved the goal", outcome_type="manual")
        self.assertEqual(result["outcome_type"], "manual")

    def test_key_present_on_success(self) -> None:
        engine = _make_engine()
        result = engine.capture_outcome(
            "The system achieved 99.2% accuracy on the validation set and success was verified"
        )
        self.assertTrue(result["saved"])
        self.assertIsNotNone(result["key"])

    def test_key_format_contains_category(self) -> None:
        engine = _make_engine()

        # Learning-only text — no other category keywords that could compete
        r_learn = engine.capture_outcome("I learned a valuable insight today with the API")
        self.assertIsNotNone(r_learn["key"])
        self.assertIn("learning", r_learn["key"].lower())


# ---------------------------------------------------------------------------
# Dual-write (t_87b41d3a): LEARNING/MISTAKE/DECISION save to hermes_default AND mempalace
# ---------------------------------------------------------------------------


class _MockOrchestratorDualWrite:
    """Mock that tracks which source names received saves."""

    def __init__(self) -> None:
        self.save_by_source: list[list] = []  # [(source_name, key, payload), ...]

    def recommended_sources(
        self, write_type: str = "general", max_results: int = 3
    ) -> list[str]:
        return ["mempalace"]

    def save(self, key: str, value: dict, source_name: str = "") -> bool:
        self.save_by_source.append((source_name, key, value))
        return True


class TestDualWrite(unittest.TestCase):
    """AC-t_87b41d3a: dual-write to hermes_default + mempalace for key categories."""

    def _sources_saved(self, orch: _MockOrchestratorDualWrite) -> set[str]:
        return {entry[0] for entry in orch.save_by_source}

    def test_learning_saves_to_hermes_default_and_mempalace(self) -> None:
        orch = _MockOrchestratorDualWrite()
        engine = AutoStorageEngine(orch)
        result = engine.capture_outcome("I learned that the routing map was wrong")
        self.assertTrue(result["saved"])
        sources = self._sources_saved(orch)
        self.assertIn("mempalace", sources, "should save to mempalace via recommended_sources")
        self.assertIn("hermes_default", sources, "dual-write fallback should hit hermes_default too")

    def test_mistake_saves_to_both_sources(self) -> None:
        orch = _MockOrchestratorDualWrite()
        engine = AutoStorageEngine(orch)
        result = engine.capture_outcome("Something went wrong with the pipeline configuration")
        self.assertTrue(result["saved"])
        sources = self._sources_saved(orch)
        self.assertIn("hermes_default", sources)

    def test_decision_saves_to_both_sources(self) -> None:
        orch = _MockOrchestratorDualWrite()
        engine = AutoStorageEngine(orch)
        result = engine.capture_outcome("We decided to refactor the entire memory subsystem")
        self.assertTrue(result["saved"])
        sources = self._sources_saved(orch)
        self.assertIn("hermes_default", sources)

    def test_result_only_saves_to_one_source(self) -> None:
        orch = _MockOrchestratorDualWrite()
        engine = AutoStorageEngine(orch)
        result = engine.capture_outcome("The benchmark achieved 99.2% accuracy and was a success")
        self.assertTrue(result["saved"])
        sources = self._sources_saved(orch)
        # RESULT is NOT a dual-write category — should only hit recommended_sources (mempalace)
        self.assertEqual(sources, {"mempalace"})


if __name__ == "__main__":
    unittest.main()
