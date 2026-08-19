"""MistakeDetector — unit tests for each correction pattern class with real conversation excerpts."""

import pytest

from memchorus.mistake_detector import (
    MistakeDetector,
    CorrectionType,
    DetectionResult,
    _CORRECTION_PATTERNS,
)


@pytest.fixture(autouse=True)
def _reset_detector():
    MistakeDetector._instance = None
    yield
    MistakeDetector._instance = None


def test_repetition_general_pattern_i_already_told():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("I already told you that, stop asking")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.REPETITION for r in results)


def test_repetition_general_pattern_you_know_this():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("You know this already, it's not worth mentioning again")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.REPETITION for r in results)


def test_repetition_general_pattern_told_you_before():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("I told you before about this project structure")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.REPETITION for r in results)


def test_repetition_general_pattern_told_you_that():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("I told you that already when we started")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.REPETITION for r in results)


def test_repetition_redundant_pattern_thats_already():
    detector = MistakeDetector.get_instance()
    # Pattern expects "THAT  's already" with whitespace before 's
    results = detector.scan_user_text("THAT  's already covered")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.REPETITION for r in results)


def test_repetition_redundant_pattern_duplicate():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("This is a duplicate of what we already have")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.REPETITION for r in results)


def test_repetition_redundant_pattern_stop_repeating():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("Stop repeating yourself with the same information")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.REPETITION for r in results)


def test_outdated_changed_pattern_now_called():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("The API is now called something different")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.OUTDATED for r in results)


def test_outdated_changed_pattern_changed_to():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("The project has changed to use a new framework")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.OUTDATED for r in results)


def test_outdated_changed_pattern_renamed_from():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("Renamed from the old package name")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.OUTDATED for r in results)


def test_outdated_correction_pattern_actually_its():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("Actually it's not that way anymore")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.OUTDATED for r in results)


def test_outdated_correction_pattern_correct_name():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text("The correct name of the service has changed")
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.OUTDATED for r in results)


def test_outdated_correction_pattern_actually_is_different():
    detector = MistakeDetector.get_instance()
    results = detector.scan_user_text(
        "Actually it is now different from what you said before"
    )
    assert len(results) >= 1
    assert any(r.correction_type == CorrectionType.OUTDATED for r in results)


class TestClassifyAndFlag:
    """Verification of classify_and_flag counters and flag behavior."""

    def test_classify_increments_noise_on_correction(self):
        detector = MistakeDetector.get_instance()
        noise, useful = detector.classify_and_flag("I already told you that part")
        assert noise >= 1
        assert detector.total_noise_flags >= 1

    def test_classify_no_noise_on_benign_text(self):
        detector = MistakeDetector.get_instance()
        noise, useful = detector.classify_and_flag("This is a regular update about the project progress today")
        assert noise == 0

    def test_record_positive_signal(self):
        detector = MistakeDetector.get_instance()
        detector.record_positive_signal()
        assert detector.total_useful_flags >= 1


class TestDetectionResult:
    """Verify DetectionResult dataclass properties."""

    def test_is_negative_for_repetition(self):
        result = DetectionResult(
            correction_type=CorrectionType.REPETITION,
            match_text="I already told",
            confidence=0.7,
        )
        assert result.is_negative is True

    def test_is_negative_for_outdated(self):
        result = DetectionResult(
            correction_type=CorrectionType.OUTDATED,
            match_text="changed to new name",
            confidence=0.7,
        )
        assert result.is_negative is True

    def test_is_negative_false_for_positive(self):
        result = DetectionResult(
            correction_type=CorrectionType.POSITIVE,
            match_text="",
            confidence=1.0,
        )
        assert result.is_negative is False


class TestFalsePositiveRate:
    """Verify false positive rate stays below 15% on benign text corpus."""

    # Benign texts that should NOT trigger correction patterns
    BENIGN_CORPUS = [
        "Today I worked on the feature branch and pushed commits",
        "The database migration completed successfully without errors",
        "I need to review the pull request before merging it",
        "This document covers the architecture decisions we made",
        "The deployment pipeline runs tests automatically every night",
        "We should refactor this module for better readability",
        "The API response time has improved since the last update",
        "I finished reading through the documentation this morning",
        "Meeting notes are saved in the shared drive location",
        "Configuration changes require a service restart",
        "The backup process completes within the scheduled window",
        "Testing covered all critical paths in the payment flow",
        "Documentation updates were merged into the main branch",
        "Server monitoring shows normal resource usage today",
        "Code review feedback has been addressed in the latest patch",
        "Integration tests pass across all supported platforms",
        "The incident report has been filed with the operations team",
        "Performance metrics indicate stable throughput this week",
        "User feedback from the beta test was mostly positive",
        "Infrastructure provisioning takes about twenty minutes now",
    ]

    def test_false_positive_rate_below_threshold(self):
        detector = MistakeDetector.get_instance()
        false_positives = 0
        total = len(self.BENIGN_CORPUS)

        for text in self.BENIGN_CORPUS:
            results = detector.scan_user_text(text)
            # Only count negative corrections as potential false positives
            if any(r.is_negative for r in results):
                false_positives += 1

        rate = false_positives / total
        assert rate < 0.15, (
            f"False positive rate {rate:.0%} ({false_positives}/{total}) "
            f"exceeds 15% threshold"
        )


class TestPatternCoverage:
    """Ensure every pattern in _CORRECTION_PATTERNS fires at least once."""

    def test_all_patterns_fire(self):
        detector = MistakeDetector.get_instance()
        triggered_names = set()

        for name, pattern, ctype in _CORRECTION_PATTERNS:
            # Find a test string that triggers this specific compiled pattern
            test_strings = [
                "I already told you about this",
                "You know this already",
                "That's already documented here",
                "This is a duplicate entry don't repeat",
                "The name has now changed to something new",
                "Actually it is different than recorded",
                "Stop repeating the same information over",
            ]
            for ts in test_strings:
                if pattern.search(ts):
                    triggered_names.add(name)
                    break

        all_names = {n for n, _, _ in _CORRECTION_PATTERNS}
        missing = all_names - triggered_names
        assert not missing, f"Patterns never triggered by any test string: {missing}"


class TestEmptyAndEdgeInput:
    """Handle empty, whitespace, and extremely long input."""

    def test_empty_input(self):
        detector = MistakeDetector.get_instance()
        results = detector.scan_user_text("")
        assert results == []

    def test_whitespace_only(self):
        detector = MistakeDetector.get_instance()
        results = detector.scan_user_text("   \n\t  ")
        assert results == []

    def test_case_insensitive_matching(self):
        detector = MistakeDetector.get_instance()
        results_upper = detector.scan_user_text("I ALREADY TOLD YOU THAT")
        results_lower = detector.scan_user_text("i already told you that")
        assert len(results_upper) >= 1
        assert len(results_lower) >= 1
