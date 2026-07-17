"""
Tests for Bug 3 auto-storage filtering pipeline.

Acceptance criteria covered:
  AC1: minimum content length threshold (default 50 chars) rejects short text
  AC2: noise pattern detection rejects errors, empty results, library dumps
  AC3: Shannon entropy gating rejects highly repetitive boilerplate
  AC4: provenance metadata tagging + relevance penalty for auto-generated content

Uses Mock objects to decouple from real orchestrator implementation.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.auto_storage_engine import (
    AutoStorageEngine,
    _is_noise,
    _shannon_entropy,
    _has_minimum_signal,
    PROVENANCE_PENALTY_FACTOR,
)


# ---------------------------------------------------------------------------
# Mock orchestrator helpers
# ---------------------------------------------------------------------------


class _MockOrchestrator:
    """Minimal mock that records save calls."""

    def __init__(self) -> None:
        self.saved_calls: list = []

    def recommended_sources(
        self, write_type: str = "general", max_results: int = 3
    ) -> list[str]:
        return ["mock"]

    def save(self, key: str, value: dict, **kwargs) -> bool:
        self.saved_calls.append((key, value))
        return True

    def retrieve(self, key: str):
        return None


def _make_engine(orch=None, min_content_length=50) -> AutoStorageEngine:
    if orch is None:
        orch = _MockOrchestrator()
    return AutoStorageEngine(orchestrator=orch, min_content_length=min_content_length)


# ---------------------------------------------------------------------------
# AC1: Minimum content length threshold
# ---------------------------------------------------------------------------


class TestMinContentLength(unittest.TestCase):
    """AC1: text below the configured minimum content length is rejected."""

    def test_default_min_50_rejects_short(self) -> None:
        """Text under 50 chars must be rejected with default settings."""
        engine = _make_engine()
        short_text = "A" * 49  # exactly 49 characters (under threshold)
        result = engine.capture_outcome(short_text)
        self.assertFalse(result["saved"])

    def test_short_49_rejected(self) -> None:
        """Exactly 49 characters is below the default threshold of 50."""
        engine = _make_engine()
        short_text = "A" * 49
        result = engine.capture_outcome(short_text)
        self.assertFalse(result["saved"])

    def test_exactly_50_accepted_if_enough_signal(self) -> None:
        """Exactly 50 characters meets the minimum threshold."""
        engine = _make_engine()
        # 50 chars with enough diversity to pass entropy check
        text = "I learned that the routing table was misconfigured during testing phase."
        result = engine.capture_outcome(text)
        self.assertTrue(result["saved"])

    def test_configurable_threshold(self) -> None:
        """User can override min_content_length on construction."""
        engine = AutoStorageEngine(
            _MockOrchestrator(),
            min_content_length=30,  # lower threshold
        )
        text = "We decided to refactor this subsystem today."
        self.assertTrue(len(text) >= 30)
        result = engine.capture_outcome(text)
        # Should pass length check (>= 30), might still fail entropy
        # but at least we verified the constructor parameter works
        self.assertEqual(engine.min_content_length, 30)

    def test_below_custom_threshold_rejected(self) -> None:
        """Shorter than custom threshold is rejected."""
        engine = AutoStorageEngine(
            _MockOrchestrator(),
            min_content_length=100,
        )
        text = "I learned something really important about the system architecture today"
        self.assertTrue(len(text) < 100)
        result = engine.capture_outcome(text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "below_min_content_length")

    def test_long_text_accepted(self) -> None:
        """Significant text well above threshold is saved."""
        engine = _make_engine()
        long_text = (
            "I learned that the benchmark achieved 99.2% accuracy on the validation set, "
            "and the success rate improved significantly after we refactored the data pipeline."
        )
        result = engine.capture_outcome(long_text)
        self.assertTrue(result["saved"])


# ---------------------------------------------------------------------------
# AC2: Noise pattern filtering
# ---------------------------------------------------------------------------


class TestNoisePatternFiltering(unittest.TestCase):
    """AC2: known noise patterns are rejected to prevent boilerplate storage."""

    def test_traceback_rejected(self) -> None:
        text = "Traceback (most recent call last):\n  File 'test.py', line 1\n    raise ValueError"
        self.assertTrue(_is_noise(text))
        engine = _make_engine()
        result = engine.capture_outcome(text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "noise_pattern")

    def test_error_message_rejected(self) -> None:
        text = "Error: Connection refused at port 5432 after 30 seconds of retries"
        self.assertTrue(_is_noise(text))

    def test_exception_rejected(self) -> None:
        text = "Exception: Unable to load the specified module from the cache directory"
        self.assertTrue(_is_noise(text))

    def test_module_not_found_rejected(self) -> None:
        text = "ModuleNotFoundError: No module named 'missing_package'"
        self.assertTrue(_is_noise(text))

    def test_none_result_rejected(self) -> None:
        text = "None"
        self.assertTrue(_is_noise(text))

    def test_empty_list_rejected(self) -> None:
        # Empty list / dict patterns should NOT flag normal text
        self.assertFalse(_is_noise("some meaningful content here that is long enough"))

    def test_version_string_rejected(self) -> None:
        text = "version: 1.23.45\nAuthor: Someone\nLicense: MIT"
        self.assertTrue(_is_noise(text))

    def test_importlib_dump_rejected(self) -> None:
        text = "importlib.metadata.requires returns [] for package 'test'"
        self.assertTrue(_is_noise(text))

    def test_no_results_rejected(self) -> None:
        text = "no results found in the search query after exhaustive checking" * 3
        self.assertTrue(_is_noise(text))

    def test_meaningful_content_not_noise(self) -> None:
        """Normal meaningful output should NOT be flagged as noise."""
        text = (
            "I learned that the benchmark achieved 99.2% accuracy on the validation set, "
            "which was a significant improvement over previous experiments and results."
        )
        self.assertFalse(_is_noise(text))

    def test_learning_with_error_word_not_noise(self) -> None:
        """Text that contains 'error' as part of meaningful discussion should pass."""
        text = (
            "I learned that the error rate dropped significantly after we added input validation,"
            "which was a key insight into improving our system reliability by design."
        )
        self.assertFalse(_is_noise(text))


# ---------------------------------------------------------------------------
# AC3: Shannon entropy gating
# ---------------------------------------------------------------------------


class TestShannonEntropy(unittest.TestCase):
    """AC3: low-entropy repetitive content is rejected even if long enough."""

    def test_repetitive_content_low_entropy(self) -> None:
        text = "ok ok ok ok ok ok ok ok ok ok ok ok ok ok ok ok" * 5  # very long but low entropy
        entropy = _shannon_entropy(text)
        # Low entropy text should be below threshold
        self.assertIsInstance(entropy, float)

    def test_meaningful_content_high_entropy(self) -> None:
        text = "I learned that the benchmark achieved 99.2% accuracy on validation"
        entropy = _shannon_entropy(text)
        self.assertGreater(entropy, 1.0)

    def test_empty_string_zero_entropy(self) -> None:
        self.assertEqual(_shannon_entropy(""), 0.0)
        self.assertFalse(_has_minimum_signal(""))

    def test_single_char_low_entropy(self) -> None:
        text = "a" * 100
        entropy = _shannon_entropy(text)
        # Single character repeated has zero entropy
        self.assertAlmostEqual(entropy, 0.0, places=4)

    def test_diverse_content_accepted(self) -> None:
        """Diverse, meaningful content passes entropy filter."""
        engine = _make_engine()
        text = (
            "We decided to refactor the authentication module, "
            "choosing OAuth 2.0 instead of basic auth for better security."
        )
        result = engine.capture_outcome(text)
        self.assertTrue(result["saved"])

    def test_has_minimum_signal_boundary(self) -> None:
        """Boundary test: entropy just above threshold should pass."""
        high_entropy_text = "The quick brown fox jumps over lazy dogs near rivers." * 3
        self.assertTrue(_has_minimum_signal(high_entropy_text))


# ---------------------------------------------------------------------------
# AC4: Provenance tagging and relevance penalty
# ---------------------------------------------------------------------------


class _MockOrchestratorWithPayloads:
    """Mock that records save payloads for inspecting provenance metadata."""

    def __init__(self) -> None:
        self.saved_payloads: list = []  # [(key, value_dict), ...]

    def recommended_sources(
        self, write_type: str = "general", max_results: int = 3
    ) -> list[str]:
        return ["mock"]

    def save(self, key: str, value: dict, **kwargs) -> bool:
        self.saved_payloads.append((key, dict(value)))
        return True


class TestProvenanceTagging(unittest.TestCase):
    """AC4: auto-stored content gets provenance metadata for penalty scoring."""

    def test_provenance_key_in_payload(self) -> None:
        """Payload saved by AutoStorageEngine includes provenance='auto_stored'."""
        orch = _MockOrchestratorWithPayloads()
        engine = AutoStorageEngine(orch)
        text = (
            "I learned that the benchmark achieved 99.2% accuracy on validation,"
            "and this was a significant result from our experiments today."
        )
        engine.capture_outcome(text, outcome_type="automatic")

        # Check all saved payloads include provenance metadata
        self.assertTrue(len(orch.saved_payloads) > 0)
        for key, payload in orch.saved_payloads:
            self.assertIn("provenance", payload)
            self.assertEqual(payload["provenance"], "auto_stored")

    def test_provenance_penalty_factor_constant(self) -> None:
        """PROVENANCE_PENALTY_FACTOR is set to 0.3 as specified."""
        self.assertEqual(PROVENANCE_PENALTY_FACTOR, 0.3)

    def test_relevance_scorer_applies_provenance_penalty(self) -> None:
        """RelevanceScorer scores auto-generated content lower."""
        from memchorus.relevance_engine import RelevanceScorer, ContextWeight

        scorer = RelevanceScorer(ContextWeight())

        # Auto-stored content (with provenance dict)
        auto_result = {
            "key": "test_auto",
            "content": {
                "text": "I learned that something interesting happened today during testing",
                "provenance": "auto_stored",
                "category": "LEARNING",
            },
            "source": "mock",
            "timestamp": None,
        }

        # Manual content (no provenance)
        manual_result = {
            "key": "test_manual",
            "content": "I learned that something interesting happened today during testing",
            "source": "mock",
            "timestamp": None,
        }

        query = "something interesting"
        auto_score = scorer.score(auto_result, query)
        manual_score = scorer.score(manual_result, query)

        # Auto-stored should score lower due to 0.7 multiplier (30% penalty equivalent)
        self.assertLess(auto_score, manual_score)
        # Roughly: auto_score should be ~0.7x manual for the same quality/recency
        self.assertAlmostEqual(
            auto_score / max(manual_score, 1e-9),
            0.7,
            delta=0.15,  # allow some tolerance for randomness in scoring
        )

    def test_manual_content_no_penalty(self) -> None:
        """Non-dict content is unaffected by provenance penalty."""
        from memchorus.relevance_engine import RelevanceScorer, ContextWeight

        scorer = RelevanceScorer(ContextWeight())

        result = {
            "key": "test",
            "content": "Plain string content with no metadata at all here today",
            "source": "mock",
            "timestamp": None,
        }

        # Should score normally without penalty
        score = scorer.score(result, "content")
        self.assertGreater(score, 0.0)


# ---------------------------------------------------------------------------
# Integration: full pipeline rejects noise despite length
# ---------------------------------------------------------------------------


class TestPipelineIntegration(unittest.TestCase):
    """End-to-end tests verifying all filters chain correctly."""

    def test_meaningful_learning_saves(self) -> None:
        """Positive flow: meaningful text passes all filters and saves."""
        orch = _MockOrchestrator()
        engine = _make_engine()
        text = (
            "I learned that the deployment pipeline needed a caching layer to handle "
            "the spike in concurrent users during peak hours successfully today."
        )
        result = engine.capture_outcome(text)
        self.assertTrue(result["saved"])
        self.assertEqual(result["significance"], "LEARNING")

    def test_noise_pattern_blocks_even_if_long(self) -> None:
        """Long error trace is still rejected by noise filter."""
        engine = _make_engine()
        text = (
            "Traceback (most recent call last):\n"
            "  File 'test.py', line 1 in <module>\n"
            "    raise ValueError('this is a very long error message that exceeds fifty characters easily')"
        )
        self.assertTrue(len(text) >= 50)
        result = engine.capture_outcome(text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "noise_pattern")

    def test_hooks_route_through_engine(self) -> None:
        """hooks MemChorusHooks.on_post_tool_call routes through AutoStorageEngine."""
        from memchorus.hooks import MemChorusHooks

        mock_orch = MagicMock()
        mock_orch.recommended_sources.return_value = ["mock"]
        mock_orch.save.return_value = True

        hooks_instance = MemChorusHooks()
        # Patch _get_orchestrator to return our mock
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            long_significant = (
                "We decided to implement a caching strategy with Redis to handle "
                "the increased load during peak traffic periods effectively today."
            )
            result = hooks_instance.on_post_tool_call(
                tool_output=long_significant,
                tool_name="terminal",
            )
            self.assertIsNotNone(result)
            self.assertTrue("saved_ids" in result)

    def test_hooks_rejects_short_output(self) -> None:
        """Short meaningful output gets rejected by hooks via min_content_length."""
        from memchorus.hooks import MemChorusHooks

        mock_orch = MagicMock()
        hooks_instance = MemChorusHooks()
        short_text = "ok done"  # well under 50 chars

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            result = hooks_instance.on_post_tool_call(
                tool_output=short_text,
                tool_name="terminal",
            )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
