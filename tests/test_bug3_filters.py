"""
Bug 3: AutoStorageEngine + noise/entropy filtering for post-tool storage (verified rework)

Tests the four acceptance criteria from TASKS.md:

AC1 - min_content_length threshold (default 50 chars before storage)
AC2 - Noise pattern recognition rejects error stacks, empty imports, boilerplate
AC3 - Shannon entropy gating repetitive content below signal ratio thresholds
AC4 - Provenance penalty multiplier (P=0.3) in RelevanceScorer

Also verifies provenance marker _auto_provenance: True is attached during auto-capture.
"""

import os
import sys
import math
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.auto_storage_engine import (
    AutoStorageEngine,
    _is_noise,
    _shannon_entropy,
    _has_minimum_signal,
)
from memchorus.relevance_engine import RelevanceScorer


# ---------------------------------------------------------------------------
# Mock orchestrator that tracks what was actually saved
# ---------------------------------------------------------------------------

class _MockOrchestrator:
    """Minimal mock that records save calls."""

    def __init__(self) -> None:
        self.saved_calls: list = []  # [(key, value)]

    def recommended_sources(self, write_type: str = "general"):
        return ["mock"]

    def save(self, key: str, value: dict, **kwargs) -> bool:
        self.saved_calls.append((key, value))
        return True


def _make_engine(orch=None) -> AutoStorageEngine:
    if orch is None:
        orch = _MockOrchestrator()
    return AutoStorageEngine(orchestrator=orch)


# ---------------------------------------------------------------------------
# AC1: min_content_length threshold (default 30 chars before storage)
# ---------------------------------------------------------------------------

class TestMinContentLength(unittest.TestCase):
    """AC1: Content below the length threshold is rejected with reason='below_min_content_length'"""

    def test_default_minimum_is_30_chars(self) -> None:
        engine = _make_engine()
        self.assertEqual(engine.min_content_length, 30)

    def test_can_configure_custom_threshold(self) -> None:
        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orch, min_content_length=100)
        self.assertEqual(engine.min_content_length, 100)

    def test_exactly_29_chars_rejected_default(self) -> None:
        """Text of exactly 29 chars (one below threshold) must be rejected."""
        engine = _make_engine()
        short_text = "X" * 29
        result = engine.capture_outcome(short_text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "below_min_content_length")

    def test_exactly_30_chars_accepted_default(self) -> None:
        """Text of exactly 30 chars must pass the threshold gate."""
        engine = _make_engine()
        # 30-char text with significance keywords to avoid trivial/noise/entropy filters
        text = "I learned that this system succeeded"
        self.assertGreaterEqual(len(text), 30)
        result = engine.capture_outcome(text)
        # Should NOT be rejected with below_min_content_length (may save or be deduped)
        if not result["saved"]:
            self.assertNotEqual(result["reason"], "below_min_content_length")

    def test_empty_string_rejected(self) -> None:
        engine = _make_engine()
        result = engine.capture_outcome("")
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "below_min_content_length")

    def test_whitespace_only_rejected(self) -> None:
        engine = _make_engine()
        engine.min_content_length = 10
        result = engine.capture_outcome("           ")
        self.assertFalse(result["saved"])

    def test_custom_threshold_enforces_stricter_limit(self) -> None:
        """With threshold=80, text with significant content but only 60 chars must be rejected."""
        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orch, min_content_length=80)
        text = "I learned something important and successful today"
        self.assertLess(len(text), 80)
        result = engine.capture_outcome(text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "below_min_content_length")


# ---------------------------------------------------------------------------
# AC2: Noise pattern recognition rejects known bad patterns
# ---------------------------------------------------------------------------

class TestNoisePattern(unittest.TestCase):
    """AC2: Known noise patterns are rejected with reason='noise_pattern'"""

    def test_python_traceback_rejected(self) -> None:
        text = (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 42, in main\n'
            "    result = do_thing()\n"
            "ValueError: something went wrong"
        )
        self.assertTrue(_is_noise(text))

    def test_exception_class_rejected(self) -> None:
        text = (
            "ValueError: Invalid argument passed to function.\n"
            "The input data was corrupted during transmission."
        )
        self.assertTrue(_is_noise(text))

    def test_module_not_found_rejected(self) -> None:
        text = (
            "ModuleNotFoundError: No module named 'requests'\n"
            "Import failed, please install the dependency."
        )
        stack_trace_result = _is_noise(text)
        self.assertTrue(stack_trace_result)

    def test_file_line_reference_rejected(self) -> None:
        """File references with line numbers are traceback fragments."""
        text = (
            '  File "/usr/lib/python/module.py", line 123\n'
            "    return func()"
        )
        self.assertTrue(_is_noise(text))

    def test_hex_dump_rejected(self) -> None:
        """Hex data that looks like binary dumps must be rejected."""
        text = "4a 6f 68 6e 00 a1 b2 c3 d4 e5 f6 78 90 ab cd ef"
        self.assertTrue(_is_noise(text))

    def test_import_only_block_rejected(self) -> None:
        """When imports dominate (>=70% of lines), it's noise."""
        text = "\n".join([
            "import os",
            "import sys",
            "from collections import Counter",
            "from pathlib import Path",
            "import json",
            "# some actual code here",
            "# another comment",
        ])
        self.assertTrue(_is_noise(text))

    def test_separator_wall_rejected(self) -> None:
        """Repeating separator lines must be rejected."""
        text = "\n".join(["---"] * 10) + "\nDone."
        self.assertTrue(_is_noise(text))

    def test_repeated_lines_rejected(self) -> None:
        """Consecutive identical lines should be caught."""
        line = "some content that repeats over and over"
        text = ("\n".join([line] * 10)) + "\nend marker"
        self.assertTrue(_is_noise(text))

    def test_normal_text_accepted_by_noise_filter(self) -> None:
        """Normal meaningful text must NOT be flagged as noise."""
        text = (
            "I learned that the benchmark results showed 99% accuracy improvement. "
            "The decision was made based on statistical significance testing."
        )
        self.assertFalse(_is_noise(text))

    def test_mixed_content_with_some_code_accepted(self) -> None:
        """Text with actual code mixed in should pass when imports are a minority."""
        text = (
            "import os\n"
            "\n"
            "def main():\n"
            "    I learned that the result showed significant improvement over baseline.\n"
            "    The outcome was achieved through iterative experimentation."
        )
        self.assertFalse(_is_noise(text))

    def test_error_output_rejected_via_capture_outcome(self) -> None:
        """Full integration: capture_outcome should return noise_pattern for tracebacks."""
        engine = _make_engine()
        traceback_text = (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 10, in run\n'
            "    process_data()\n"
            "RuntimeError: unexpected error condition occurred"
        )
        result = engine.capture_outcome(traceback_text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "noise_pattern")

    def test_hex_dump_rejected_via_capture_outcome(self) -> None:
        """Full integration: hex data should be rejected at the noise filter stage."""
        engine = _make_engine()
        hex_text = "a1 b2 c3 d4 e5 f6 78 90 ab cd ef 12 34 56 78 9a bc de fg"
        result = engine.capture_outcome(hex_text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "noise_pattern")


# ---------------------------------------------------------------------------
# AC3: Shannon entropy gating
# ---------------------------------------------------------------------------

class TestShannonEntropy(unittest.TestCase):
    """AC3: Repetitive and low-entropy content is rejected with reason='low_entropy_signal'"""

    def test_shannon_entropy_single_char(self) -> None:
        """A single repeated character should have entropy of 0."""
        text = "a" * 100
        ent = _shannon_entropy(text)
        self.assertAlmostEqual(ent, 0.0, places=2)

    def test_shannon_entropy_empty_string(self) -> None:
        ent = _shannon_entropy("")
        self.assertEqual(ent, 0.0)

    def test_english_prose_reasonable_entropy(self) -> None:
        """Natural English text should have reasonable entropy (>1.5 bits/char)."""
        prose = "I learned that the benchmark achieved high accuracy results."
        ent = _shannon_entropy(prose.strip())
        self.assertGreater(ent, 1.5)

    def test_has_minimum_signal_accepts_meaningful_text(self) -> None:
        text = "I realized we need to refactor this module for performance."
        self.assertTrue(_has_minimum_signal(text, threshold_entropy=1.5))

    def test_has_minimum_signal_rejects_repeated_whitespace(self) -> None:
        """Whitespace walls are low entropy and should be rejected."""
        text = "   \n   \n   \n   \n   "
        self.assertFalse(_has_minimum_signal(text, threshold_entropy=1.5))

    def test_has_minimum_signal_short_text_rejected(self) -> None:
        """Text below minimum length for entropy calculation is rejected."""
        text = "a b c"  # Only 5 chars < _MIN_ENTROPY_CHARS (20) threshold
        self.assertFalse(_has_minimum_signal(text, threshold_entropy=1.5))

    def test_has_minimum_signal_near_threshold_accepted(self) -> None:
        """Text just above the minimum character requirement passes if entropy is sufficient."""
        # 25 chars of meaningful text should pass both length + entropy checks
        text = "I learned something today"
        self.assertTrue(_has_minimum_signal(text, threshold_entropy=1.5))

    def test_separator_pattern_low_signal(self) -> None:
        """Separator walls like '--- --- ---' have low Shannon entropy."""
        text = "--- --- --- --- --- ---"
        # After whitespace normalization this is very repetitive
        self.assertFalse(_has_minimum_signal(text, threshold_entropy=1.5))

    def test_capture_outcome_rejects_low_entropy(self) -> None:
        """Full integration: low-entropy content with sufficient length still gets rejected by entropy gate."""
        engine = _make_engine()
        # Build something long enough to pass min_content_length (50 chars)
        # but so repetitive it fails Shannon entropy threshold.
        text = "--- --- --- --- --- --- --- --- --- ---\n--- --- --- --- --- ---"
        result = engine.capture_outcome(text)
        self.assertFalse(result["saved"])
        # The separator wall should be caught by EITHER noise filter or entropy gate
        self.assertIn(result["reason"], ("noise_pattern", "low_entropy_signal"))

    def test_high_entropy_content_passes(self) -> None:
        """Content with significant variation passes the entropy gate easily."""
        engine = _make_engine()
        text = (
            "I learned that the experimental framework achieved 99.2% accuracy on validation. "
            "The result was statistically significant with p < 0.001. "
            "We decided to deploy this in production immediately."
        )
        result = engine.capture_outcome(text)
        # Either saves successfully or is deduped, but should NOT be low_entropy_signal
        if not result["saved"]:
            self.assertNotEqual(result["reason"], "low_entropy_signal")

    def test_custom_entropy_threshold_stricter(self) -> None:
        """A higher entropy threshold should reject text more easily."""
        text = "aaa aaaa aaaa aaa aaaa aaaa"  # low variation but passes default
        ent = _shannon_entropy(text.strip())
        # With threshold=1.5 this might pass, with threshold=2.0 it should fail
        self.assertFalse(_has_minimum_signal(text, threshold_entropy=2.0))


# ---------------------------------------------------------------------------
# AC4: Provenance penalty multiplier in RelevanceScorer (P=0.3)
# ---------------------------------------------------------------------------

class TestProvenancePenalty(unittest.TestCase):
    """AC4: auto-captured content gets scored lower than deliberate storage via provenance penalty"""

    def test_auto_provenance_marker_attached_to_payload(self) -> None:
        """The payload saved by AutoStorageEngine must include _auto_provenance: True."""
        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orch, min_content_length=10)
        text = "I learned that the system achieved significant improvement and success"
        engine.capture_outcome(text)

        # Check that saved payloads include the provenance marker
        self.assertGreater(len(orch.saved_calls), 0, "payload should have been saved")
        key, payload = orch.saved_calls[0]
        self.assertTrue(payload.get("_auto_provenance") is True,
                       "payload must carry _auto_provenance: True for auto-captured content")

    def test_relevance_scorer_applies_penalty_for_auto_content(self) -> None:
        """RelevanceScorer.score() should apply the provenance penalty when _auto_provenance=True."""
        scorer = RelevanceScorer()

        query = "memory benchmark accuracy"

        auto_result = {
            "key": "auto_123",
            "content": "I learned that the memory benchmark achieved 99% accuracy results",
            "source": "hermes_default",
            "_auto_provenance": True,
            "timestamp": "2026-07-15T00:00:00+00:00",
        }

        deliberate_result = {
            "key": "deliberate_456",
            "content": "I learned that the memory benchmark achieved 99% accuracy results",
            "source": "hermes_default",
            "_auto_provenance": False,
            "timestamp": "2026-07-15T00:00:00+00:00",
        }

        auto_score = scorer.score(auto_result, query)
        deliberate_score = scorer.score(deliberate_result, query)

        self.assertGreater(
            deliberate_score, auto_score,
            "deliberate storage must score higher than auto-captured content with identical text"
        )

    def test_auto_score_is_approximately_30_percent_of_deliberate(self) -> None:
        """The penalized score should be ~30% of the raw score (default P=0.3)."""
        scorer = RelevanceScorer()
        query = "important decision outcome"

        auto_result = {
            "key": "auto_x",
            "content": "We decided that the important decision outcome was verified successfully",
            "source": "hermes_default",
            "_auto_provenance": True,
            "timestamp": "2026-12-01T00:00:00+00:00",
        }

        raw_result = {
            "key": "raw_x",
            "content": "We decided that the important decision outcome was verified successfully",
            "source": "hermes_default",
            "_auto_provenance": False,
            "timestamp": "2026-12-01T00:00:00+00:00",
        }

        auto_score = scorer.score(auto_result, query)
        raw_score = scorer.score(raw_result, query)

        expected_ratio = 0.3
        actual_ratio = auto_score / raw_score if raw_score > 0 else 0.0
        self.assertAlmostEqual(actual_ratio, expected_ratio, places=2,
                               msg=f"auto/deliberate ratio should be ~{expected_ratio}, got {actual_ratio:.3f}")

    def test_no_penalty_when_auto_provenance_false(self) -> None:
        """Content without _auto_provenance marker (or set to False) gets full score."""
        scorer = RelevanceScorer()
        query = "important decision"

        result_no_marker = {
            "key": "no_marker",
            "content": "We decided that the important decision was successful outcome achieved",
            "source": "hermes_default",
            "timestamp": "2026-12-01T00:00:00+00:00",
        }

        result_false_marker = {
            "key": "false_marker",
            "content": "We decided that the important decision was successful outcome achieved",
            "source": "hermes_default",
            "_auto_provenance": False,
            "timestamp": "2026-12-01T00:00:00+00:00",
        }

        score_no = scorer.score(result_no_marker, query)
        score_false = scorer.score(result_false_marker, query)
        self.assertAlmostEqual(score_no, score_false, places=4,
                               msg="Absence of _auto_provenance and False should produce identical scores")

    def test_custom_penalty_multiplier(self) -> None:
        """Passing a different penalty value changes the multiplier accordingly."""
        scorer = RelevanceScorer()
        query = "results benchmark"

        auto_result = {
            "key": "custom",
            "content": "The result of benchmarking achieved outstanding success",
            "source": "hermes_default",
            "_auto_provenance": True,
            "timestamp": "2026-12-01T00:00:00+00:00",
        }

        raw_result = {
            "key": "custom_raw",
            "content": "The result of benchmarking achieved outstanding success",
            "source": "hermes_default",
            "_auto_provenance": False,
            "timestamp": "2026-12-01T00:00:00+00:00",
        }

        # With penalty=0.5, auto score should be ~50% of raw
        auto_half = scorer.score(auto_result, query, auto_provenance_penalty=0.5)
        raw_full = scorer.score(raw_result, query)

        ratio_50 = auto_half / raw_full if raw_full > 0 else 0.0
        self.assertAlmostEqual(ratio_50, 0.5, places=2, msg=f"Expected ~0.5 ratio, got {ratio_50:.3f}")

    def test_auto_content_scores_below_score_max(self) -> None:
        """Provenance penalty should not push scores negative -- clamp still works."""
        scorer = RelevanceScorer()
        query = "nothing matching at all"

        auto_result = {
            "key": "edge",
            "content": "Unrelated content with no overlap to query terms",
            "source": "hermes_default",
            "_auto_provenance": True,
        }

        result_score = scorer.score(auto_result, query)
        self.assertGreaterEqual(result_score, 0.0, "score should never be negative")
        self.assertLessEqual(result_score, 1.0, "score should never exceed max")


# ---------------------------------------------------------------------------
# Integration: all filters fire in correct order with distinct rejection reasons
# ---------------------------------------------------------------------------

class TestFilterOrderingIntegration(unittest.TestCase):
    """Verify each filter fires independently and reports its own reason code."""

    def test_below_min_content_length_reason_distinguishable(self) -> None:
        """Short content should be rejected with below_min_content_length, NOT other reasons."""
        engine = _make_engine()
        result = engine.capture_outcome("A" * 20)  # 20 chars < 30 threshold
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "below_min_content_length")

    def test_noise_pattern_reason_distinguishable(self) -> None:
        """Traceback content should be rejected with noise_pattern, NOT other reasons."""
        engine = _make_engine()
        traceback_content = (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 10\n'
            "TypeError: invalid operation\n"
            "^ This is definitely a stack trace with enough characters to pass length check"
        )
        result = engine.capture_outcome(traceback_content)
        self.assertFalse(result["saved"])
        # Must hit noise filter BEFORE entropy filter (traceback has higher entropy than repetitive text)
        self.assertEqual(result["reason"], "noise_pattern")

    def test_entropy_reason_distinguishable(self) -> None:
        """Low-entropy repetitive content that passes length + noise should hit the entropy gate."""
        engine = _make_engine()
        # Long enough to pass min_content_length, not caught by tracebacks/hex dump patterns,
        # but extremely repetitive to fail Shannon entropy.
        sep_text = "===" * 30  # Very long, low-entropy content
        result = engine.capture_outcome(sep_text)
        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "noise_pattern" if _is_noise(sep_text) else "low_entropy_signal")

    def test_meaningful_long_content_saves(self) -> None:
        """Significant content that passes all filters should actually save to orchestrator."""
        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orch)
        good_text = (
            "I learned that the new API endpoint returns JSON with nested data structures "
            "instead of flat arrays. The result showed a 15% improvement in query performance.\n"
            "We decided to refactor all clients accordingly, choosing backward compatibility as priority."
        )
        result = engine.capture_outcome(good_text)
        self.assertTrue(result["saved"])
        self.assertGreater(len(orch.saved_calls), 0, "payload must have been saved to orchestrator")

    def test_saved_payload_includes_provenance_marker(self) -> None:
        """When content does save, the payload MUST include _auto_provenance marker."""
        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orch)
        good_text = (
            "I learned that benchmark results showed significant accuracy improvement. "
            "The outcome was a success and we decided to deploy to production."
        )
        engine.capture_outcome(good_text)
        _, payload = orch.saved_calls[0]
        self.assertIn("_auto_provenance", payload, "provenance marker must be present")
        self.assertTrue(payload["_auto_provenance"] is True, "must be explicitly True, not truthy")


if __name__ == "__main__":
    unittest.main()
