"""
Tests for MistakeDetector — pattern-based correction signal classification.

Acceptance criteria (AUTOTUNING.md §7):
  AC-M1: Each pattern class fires on its correct keywords.
  AC-M2: False positive rate below 15% on benign text corpus.
  AC-M3: classify_and_flag() correctly partitions noise vs useful counters.
  AC-M4: record_positive_signal() increments useful_flags when no correction detected.
  AC-M5: scan_user_text() returns DetectionResult with expected fields.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.mistake_detector import (
    MistakeDetector,
    CorrectionType,
    DetectionResult,
    _CORRECTION_PATTERNS,
)


# ---------------------------------------------------------------------------
# Test fixtures: real conversation excerpts per pattern class
# ---------------------------------------------------------------------------

REPETITION_EXAMPLES = [
    "I already told you that my profile prefers concise responses",
    "You know this — I set it up in the config",
    "I told you before, use pytest not unittest",
    "Don't forget to add that again",
    "Duplicate — same thing as above",
    "Don't repeat what you just did",
    "Stop repeating yourself",
]

OUTDATED_EXAMPLES = [
    "It's now called Hermes Agent 3.0",
    "The path changed to ~/.hermes/config.yaml",
    "Renamed from memchorus.py to memory_orchestration.py",
    "Actually it is now different — they updated the default",
    "Actually it's at /home/bubo/.hermes instead of /usr/local",
    "The correct value is 42, not 37",
    "The real path changed last version",
]

BENIGN_EXAMPLES = [
    "Please run the tests and commit the changes",
    "Can you check if the build passes on CI?",
    "Looks good to me, merge it",
    "Let's add some logging here for debugging",
    "What's the status of task t_12345?",
    "I need you to refactor that function",
    "The documentation needs updating for the new API",
    "Can you write a skill for this workflow?",
    "How does the auto-tuning framework work?",
    "Run mypy and fix any type errors",
    "Deploy to staging when ready",
    "Let me know if you hit any blockers",
    "Great progress on this feature!",
    "I'd like a summary of what changed in v1.8.0",
    "Can you look into why the memory recall is slow?",
    "What models are currently available?",
    "Please update the README with installation instructions",
    "Set up CI pipelines for Python 3.12",
    "I want to understand how kanban tasks work",
    "Finish implementation and submit for review",
]


# ---------------------------------------------------------------------------
# AC-M1: Each pattern class fires on correct keywords
# ---------------------------------------------------------------------------


class TestCorrectionRepetitionPatterns(unittest.TestCase):
    """correction_repetition patterns detect redundancy signals."""

    def setUp(self) -> None:
        self.detector = MistakeDetector()

    def test_already_told_triggers_repetition(self) -> None:
        results = self.detector.scan_user_text("I already told you that")
        types = {r.correction_type for r in results}
        self.assertIn(CorrectionType.REPETITION, types)

    def test_you_know_this_triggers_repetition(self) -> None:
        results = self.detector.scan_user_text("You know this information")
        types = {r.correction_type for r in results}
        self.assertIn(CorrectionType.REPETITION, types)

    def test_told_before_triggers_repetition(self) -> None:
        results = self.detector.scan_user_text("I told you before not to do that")
        types = {r.correction_type for r in results}
        self.assertIn(CorrectionType.REPETITION, types)

    def test_duplicates_trigger_repetition(self) -> None:
        results = self.detector.scan_user_text("That's a duplicate of earlier content")
        types = {r.correction_type for r in results}
        self.assertIn(CorrectionType.REPETITION, types)

    def test_all_repetition_examples_match(self) -> None:
        """Every repetition example should trigger at least one REPETITION match."""
        for text in REPETITION_EXAMPLES:
            results = self.detector.scan_user_text(text)
            types = {r.correction_type for r in results}
            self.assertIn(
                CorrectionType.REPETITION, types,
                f"Expected repetition signal for: {text!r}"
            )


class TestCorrectionOutdatedPatterns(unittest.TestCase):
    """correction_outdated patterns detect factual corrections."""

    def setUp(self) -> None:
        self.detector = MistakeDetector()

    def test_now_called_triggers_outdated(self) -> None:
        results = self.detector.scan_user_text("It's now called something else")
        types = {r.correction_type for r in results}
        self.assertIn(CorrectionType.OUTDATED, types)

    def test_changed_to_triggers_outdated(self) -> None:
        results = self.detector.scan_user_text("The path changed to /new/location")
        types = {r.correction_type for r in results}
        self.assertIn(CorrectionType.OUTDATED, types)

    def test_actually_is_triggers_outdated(self) -> None:
        results = self.detector.scan_user_text("Actually it's v2.0 not v1.5")
        types = {r.correction_type for r in results}
        self.assertIn(CorrectionType.OUTDATED, types)

    def test_all_outdated_examples_match(self) -> None:
        """Every outdated example should trigger at least one OUTDATED match."""
        for text in OUTDATED_EXAMPLES:
            results = self.detector.scan_user_text(text)
            types = {r.correction_type for r in results}
            self.assertIn(
                CorrectionType.OUTDATED, types,
                f"Expected outdated signal for: {text!r}"
            )


# ---------------------------------------------------------------------------
# AC-M2: False positive rate below 15% on benign text
# ---------------------------------------------------------------------------


class TestFalsePositiveRate(unittest.TestCase):
    """MistakeDetector does not flag benign instructions as corrections."""

    def setUp(self) -> None:
        self.detector = MistakeDetector()

    def test_false_positive_rate_below_threshold(self) -> None:
        """False positive rate on benign text must stay below 15%."""
        flagged_count = 0
        for text in BENIGN_EXAMPLES:
            results = self.detector.scan_user_text(text)
            has_negative = any(r.is_negative for r in results)
            if has_negative:
                flagged_count += 1

        rate = flagged_count / len(BENIGN_EXAMPLES) if BENIGN_EXAMPLES else 0
        self.assertLess(
            rate, 0.15,
            f"False positive rate {rate:.2%} exceeds 15%% threshold "
            f"({flagged_count}/{len(BENIGN_EXAMPLES)} flagged)"
        )


# ---------------------------------------------------------------------------
# AC-M3: classify_and_flag() partitions noise vs useful correctly
# ---------------------------------------------------------------------------


class TestClassifyAndGetFlag(unittest.TestCase):
    """classify_and_flag updates global counters correctly."""

    def setUp(self) -> None:
        self.detector = MistakeDetector()

    def test_noise_counts_partitioned(self) -> None:
        noise, useful = self.detector.classify_and_flag("I already told you that")
        self.assertGreater(noise, 0)
        self.assertEqual(self.detector.total_noise_flags, noise)

    def test_no_signal_partitions_zero(self) -> None:
        """Benign text produces zero flags."""
        noise, useful = self.detector.classify_and_flag("Please run the tests")
        total_noise_plus_useful = noise + useful
        # Should be 0 or very low (no negative patterns should match)
        self.assertEqual(noise, 0)

    def test_counters_accumulate_across_calls(self) -> None:
        self.detector.classify_and_flag("I already told you that")
        self.detector.classify_and_flag("That's a duplicate entry")
        self.assertGreaterEqual(self.detector.total_noise_flags, 2)


# ---------------------------------------------------------------------------
# AC-M4: record_positive_signal() increments useful counter
# ---------------------------------------------------------------------------


class TestPositiveSignal(unittest.TestCase):

    def setUp(self) -> None:
        self.detector = MistakeDetector()

    def test_record_positive_increments_useful(self) -> None:
        initial = self.detector.total_useful_flags
        self.detector.record_positive_signal()
        self.assertEqual(self.detector.total_useful_flags, initial + 1)


# ---------------------------------------------------------------------------
# AC-M5: DetectionResult has expected fields
# ---------------------------------------------------------------------------


class TestDetectionResult(unittest.TestCase):

    def setUp(self) -> None:
        self.detector = MistakeDetector()

    def test_result_has_required_fields(self) -> None:
        results = self.detector.scan_user_text("I already told you that")
        self.assertTrue(len(results) > 0)
        r = results[0]
        self.assertIsInstance(r, DetectionResult)
        self.assertIn(r.correction_type, CorrectionType)
        self.assertIsInstance(r.match_text, str)
        self.assertTrue(0 <= r.confidence <= 1)

    def test_negative_isolation(self) -> None:
        """is_negative is True for REPETITION and OUTDATED, False for POSITIVE."""
        neg = DetectionResult(CorrectionType.REPETITION, "match", 0.7)
        self.assertTrue(neg.is_negative)
        neg2 = DetectionResult(CorrectionType.OUTDATED, "match", 0.7)
        self.assertTrue(neg2.is_negative)
        pos = DetectionResult(CorrectionType.POSITIVE, "match", 0.7)
        self.assertFalse(pos.is_negative)


# ---------------------------------------------------------------------------
# Pattern registry sanity
# ---------------------------------------------------------------------------


class TestPatternRegistry(unittest.TestCase):

    def test_no_duplicate_pattern_names(self) -> None:
        names = [name for name, _, _ in _CORRECTION_PATTERNS]
        self.assertEqual(len(names), len(set(names)))

    def test_patterns_all_have_types(self) -> None:
        for _name, pattern, ctype in _CORRECTION_PATTERNS:
            self.assertIsInstance(pattern, re.Pattern)
            self.assertIn(ctype, CorrectionType)


if __name__ == "__main__":
    unittest.main()
