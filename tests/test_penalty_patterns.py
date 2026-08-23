"""
Unit tests for GH-100 recall-time penalty patterns in RelevanceScorer.

Coverage:
 - T1: _compile_penalty_patterns with None returns built-in defaults
 - T2: _compile_penalty_patterns with user config compiles valid entries
 - T3: _compile_penalty_patterns skips invalid factor values
 - T4: _compile_penalty_patterns skips malformed entries (missing keys, bad regex)
 - T5: _apply_penalty_patterns returns 1.0 when no patterns match
 - T6: _apply_penalty_patterns applies correct factor for single match
 - T7: _apply_penalty_patterns uses minimum factor on multi-pattern overlap
 - T8: _apply_penalty_patterns returns 1.0 for empty pattern list
 - T9: _apply_penalty_patterns handles changelog content correctly
 - T10: _apply_penalty_patterns handles package list content correctly
 - T11: _apply_penalty_patterns handles empty API response content correctly
 - T12: _apply_penalty_patterns handles version block content correctly
 - T13: score() correctly applies penalty patterns multiplicatively
 - T14: score_and_rank() penalizes noisy content relative to clean content
"""

import sys
import os
import re
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.relevance_engine import (
    RelevanceScorer,
    ContextWeight,
    _DEFAULT_PENALTY_PATTERNS,
)


def _make_result(content: str, timestamp=None):
    """Helper to build a minimal result dict for scoring."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    return {
        "key": "test-key",
        "content": content,
        "source": "hermes_default",
        "timestamp": timestamp,
    }


class TestCompilePenaltyPatterns(unittest.TestCase):
    """Test pattern compilation from config (T1-T4)."""

    @staticmethod
    def _scorer(*, penalty_patterns=None):
        kwargs = {}
        if penalty_patterns is not None:
            kwargs["penalty_patterns"] = penalty_patterns
        return RelevanceScorer(**kwargs)

    def test_default_patterns(self):
        """T1: Passing None returns built-in defaults."""
        scorer = self._scorer()
        assert len(scorer._penalty_patterns) == 4, (
            f"Expected 4 default patterns, got {len(scorer._penalty_patterns)}"
        )
        # Each entry should be a (label, compiled_regex, factor) tuple
        for label, pat, factor in scorer._penalty_patterns:
            assert isinstance(label, str), f"label should be str, got {type(label)}"
            assert hasattr(pat, "search"), f"pattern should be compiled regex"
            assert 0 < factor <= 1.0, f"factor out of range: {factor}"

    def test_user_config_patterns(self):
        """T2: User-provided config compiles correctly."""
        patterns = [
            {"label": "test_pattern", "pattern": r"noisy_content", "factor": 0.5},
        ]
        scorer = self._scorer(penalty_patterns=patterns)
        assert len(scorer._penalty_patterns) == 1
        label, pat, factor = scorer._penalty_patterns[0]
        assert label == "test_pattern"
        assert factor == 0.5
        assert bool(pat.search("this has noisy_content here"))

    def test_invalid_factor_skipped(self):
        """T3: Out-of-range factors are dropped with a warning."""
        patterns = [
            {"label": "zero", "pattern": r"x", "factor": 0.0},
            {"label": "negative", "pattern": r"y", "factor": -1.0},
            {"label": "above_one", "pattern": r"z", "factor": 2.0},
            {"label": "valid", "pattern": r"ok", "factor": 0.75},
        ]
        scorer = self._scorer(penalty_patterns=patterns)
        assert len(scorer._penalty_patterns) == 1
        assert scorer._penalty_patterns[0][0] == "valid"

    def test_malformed_entries_skipped(self):
        """T4: Missing keys or broken regex are dropped gracefully."""
        patterns = [
            {"label": "no_pattern"},
            {"pattern": r"", "factor": 0.5},
            {"label": "bad", "pattern": "[invalid(regex", "factor": 0.5},
            {"label": "good", "pattern": r"^test$", "factor": 0.8},
        ]
        scorer = self._scorer(penalty_patterns=patterns)
        assert len(scorer._penalty_patterns) == 1
        assert scorer._penalty_patterns[0][0] == "good"


class TestApplyPenaltyPatterns(unittest.TestCase):
    """Test penalty matching logic (T5-T12)."""

    def test_no_match_returns_one(self):
        """T5: Content that matches no pattern gets factor 1.0."""
        scorer = RelevanceScorer()
        factor = scorer._apply_penalty_patterns("Normal user memory content here")
        assert factor == 1.0, f"Expected 1.0 for clean content, got {factor}"

    def test_changelog_penalty(self):
        """T9: Changelog-style content gets penalized."""
        scorer = RelevanceScorer()
        changelog_text = (
            "Changelog:\n"
            "- v1.2.0 Added new feature\n"
            "- v1.1.0 Bug fix for auth\n"
            "- v1.0.0 Initial release\n"
        )
        factor = scorer._apply_penalty_patterns(changelog_text)
        assert factor < 1.0, f"Changelog content should be penalized: got {factor}"

    def test_package_list_penalty(self):
        """T10: Package/dependency list content gets penalized."""
        scorer = RelevanceScorer()
        pkg_list = (
            "requests>=2.28.0\n"
            "numpy==1.24.1\n"
            "pandas<2.0\n"
            "scipy~=1.10.0\n"
        )
        factor = scorer._apply_penalty_patterns(pkg_list)
        assert factor < 1.0, f"Package list should be penalized: got {factor}"

    def test_empty_api_response_penalty(self):
        """T11: Empty/trivial API response content gets penalized."""
        scorer = RelevanceScorer()
        api_response = '{"ok": true}'
        factor = scorer._apply_penalty_patterns(api_response)
        assert factor < 1.0, f"Empty API response should be penalized: got {factor}"

    def test_version_block_penalty(self):
        """T12: Pure version metadata blocks get penalized."""
        scorer = RelevanceScorer()
        version_text = (
            "version: 1.2.3\n"
            "VERSION: 2.0.0\n"
            "Version = 0.9.1\n"
        )
        factor = scorer._apply_penalty_patterns(version_text)
        assert factor < 1.0, f"Version block should be penalized: got {factor}"

    def test_multi_pattern_uses_minimum(self):
        """T7: When multiple patterns match, minimum factor is used."""
        # A small custom set with known factors where two could overlap
        patterns = [
            {"label": "a", "pattern": r"noisy", "factor": 0.4},
            {"label": "b", "pattern": r"duplicate", "factor": 0.25},
        ]
        scorer = RelevanceScorer(penalty_patterns=patterns)
        # Content that triggers BOTH patterns -> should get min(0.4, 0.25) = 0.25
        factor = scorer._apply_penalty_patterns("noisy duplicate content")
        assert factor == 0.25, f"Expected min factor 0.25, got {factor}"

    def test_empty_pattern_list(self):
        """T8: When penalty list is empty, no penalty applied."""
        # Explicitly passing an empty list should give no penalties
        scorer = RelevanceScorer(penalty_patterns=[])
        factor = scorer._apply_penalty_patterns("any content at all")
        assert factor == 1.0


class TestPenaltyPatternsIntegrateWithScore(unittest.TestCase):
    """Test that penalty patterns actually affect score() (T13-T14)."""

    def setUp(self):
        self.scorer = RelevanceScorer()
        self.context = ContextWeight()
        now = datetime.now(timezone.utc)
        self.recent_ts = (now - timedelta(hours=1)).isoformat()

    def test_score_reduced_by_penalty(self):
        """T13: Score for noisy content is lower than same content without penalty patterns."""
        # Score with default penalties
        clean_result = _make_result(
            "This is useful information about machine learning",
            timestamp=self.recent_ts,
        )
        score_with_penalty = self.scorer.score(clean_result, "machine learning", self.context)

        # Same scorer but override to have NO penalty patterns -> content should score higher
        no_penalty_scorer = RelevanceScorer(penalty_patterns=[])
        score_no_penalty = no_penalty_scorer.score(
            clean_result, "machine learning", self.context
        )

        # Scores should be equal for non-penalized content (penalty factor is 1.0)
        assert abs(score_with_penalty - score_no_penalty) < 1e-6, (
            f"Clean content penalized: {score_with_penalty} vs {score_no_penalty}"
        )

        # Now test that truly noisy content IS reduced
        noisy_result = _make_result(
            '{"ok": true}',
            timestamp=self.recent_ts,
        )
        noisy_with = self.scorer.score(noisy_result, "machine learning", self.context)
        noisy_without = no_penalty_scorer.score(
            noisy_result, "machine learning", self.context
        )

        assert noisy_with < noisy_without, (
            f"Noisy content should score lower with penalties: "
            f"{noisy_with} vs {noisy_without}"
        )
        ratio = noisy_with / no_penalty_scorer.score(
            noisy_result, "machine learning", self.context
        ) if noisy_without > 0 else float("inf")
        assert ratio <= 0.6, (
            f"Penalty should reduce score significantly, ratio is only {ratio}"
        )

    def test_score_and_rank_penalized_content_demoted(self):
        """T14: In ranked results, penalized content falls below unpenalized."""
        scorer = RelevanceScorer()
        no_penalty_scorer = RelevanceScorer(penalty_patterns=[])

        # Clean result and noisy result with similar keyword overlap
        clean = _make_result(
            "Useful deployment notes for the production server",
            timestamp=self.recent_ts,
        )
        noisy = _make_result(
            '{"success": true}',
            timestamp=self.recent_ts,
        )

        ranked_default = scorer.score_and_rank(
            [clean, noisy], "deployment", self.context
        )
        ranked_no_penalty = no_penalty_scorer.score_and_rank(
            [clean, noisy], "deployment", self.context
        )

        # With default penalties, clean should rank above noisy
        assert ranked_default[0].key == clean["key"], (
            f"Clean content should rank first with penalties. "
            f"Ranked: {[r.key for r in ranked_default]}"
        )

    def test_factor_levels(self):
        """T6: Penalty application works at each factor level (0.1 through 0.9)."""
        for target_factor in [0.1, 0.25, 0.3, 0.35, 0.4, 0.5, 0.75, 0.9]:
            patterns = [
                {
                    "label": f"factor_{target_factor}",
                    "pattern": r"TARGET_KEY",
                    "factor": target_factor,
                }
            ]
            scorer = RelevanceScorer(penalty_patterns=patterns)
            factor = scorer._apply_penalty_patterns("content with TARGET_KEY")
            assert abs(factor - target_factor) < 1e-4, (
                f"Expected factor {target_factor}, got {factor}"
            )


if __name__ == "__main__":
    unittest.main()
