"""
Tests for GH-121: _content_matches() scoring normalisation to [0, 1].

The old implementation returned additive scores (each matched term contributed
2.0 + bonus) while MIN_RECALL_SCORE = 0.3 assumed a ratio in [0, 1]. This made
the threshold effectively useless since any single-term match scored >= 2.0.

After the fix:
  - _content_matches() returns a proportion of matched terms boosted by frequency,
    clamped to [0, 1].
  - MIN_RECALL_SCORE = 0.5 so ~half the query terms must appear for a result
    to pass through.
"""
import pytest

from memchorus.hermes_memory_source import HermesDefaultMemorySource


class TestScoringNormalisation:
    """Verify _content_matches returns values in [0, 1]."""

    def setup_method(self):
        self.source = HermesDefaultMemorySource("test_normalised")

    def test_no_match_returns_zero(self):
        score = self.source._content_matches("zzz_nonexistent_abc", "some other completely unrelated text")
        assert score == 0.0

    def test_partial_match_below_one(self):
        """Only half the query terms appear -> ~0.5"""
        score = self.source._content_matches("alpha delta", "hello alpha world here")
        assert 0.3 <= score < 1.0, f"score out of expected range: {score}"

    def test_all_terms_match_approaches_one(self):
        """All query terms present -> near or at 1.0 (with freq bonus)."""
        score = self.source._content_matches("python version", "the python version is set here for python setup")
        assert 0.8 <= score <= 1.0, f"expected near-1: {score}"

    def test_score_capped_at_one(self):
        """Even massive frequency never exceeds 1.0."""
        content = "hello world hello world hello world hello world hello world"
        score = self.source._content_matches("hello world", content)
        assert score <= 1.0

    def test_self_match_penalised(self):
        """When query equals content, the self-match penalty halves the score."""
        score = self.source._content_matches("hello world", "hello world")
        assert score < 0.8, f"self-match should be penalised: {score}"

    def test_single_term_full_match(self):
        """A single-term query that matches yields 1.0 (ignoring self-match)."""
        content = "this project uses python extensively for testing"
        score = self.source._content_matches("python", content)
        assert 0.8 <= score <= 1.0, f"single term full match: {score}"

    def test_min_recall_score_is_half(self):
        """MIN_RECALL_SCORE threshold is 0.5 — half the terms should qualify."""
        assert HermesDefaultMemorySource.MIN_RECALL_SCORE == 0.5


class TestEffectiveMinScoreDefaults:
    def test_default_config_uses_class_constant(self):
        source = HermesDefaultMemorySource("test")
        eff = source._effective_min_score()
        assert eff == HermesDefaultMemorySource.MIN_RECALL_SCORE

    def test_config_override_allows_lower(self):
        source = HermesDefaultMemorySource("test", config={"min_recall_score": 0.2})
        assert source._effective_min_score() == 0.2


class TestSearchFiltersWithNewThreshold:
    """End-to-end: search() drops results below MIN_RECALL_SCORE."""

    def test_irrelevant_results_filtered(self, tmp_path):
        source = HermesDefaultMemorySource("test_filter", data_dir=str(tmp_path))
        # Save a memory about something completely unrelated to the query
        import json as _json
        fname = tmp_path / "completely-unrelated-topic.json"
        with open(fname, 'w') as f:
            _json.dump({"content": "this has nothing to do with search queries"}, f)

        results = source.search("quantum physics teleportation")
        # Score should fall far below 0.5 -> filtered out
        assert len(results) == 0

    def test_relevant_results_pass(self, tmp_path):
        source = HermesDefaultMemorySource("test_filter2", data_dir=str(tmp_path))
        import json as _json
        fname = tmp_path / "quantum-physics-lecture.json"
        with open(fname, 'w') as f:
            _json.dump({"content": "lectures on quantum physics and particle teleportation experiments"}, f)

        results = source.search("quantum physics")
        # Both query terms match -> score near 1.0 -> well above 0.5
        assert len(results) >= 1
