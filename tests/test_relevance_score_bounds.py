"""Score bound regression tests — G3 fix (2026-07-12).

Ensures RelevanceScorer outputs stay in reasonable range [0, MAX] where MAX <= 5.
Higher quality input should still produce higher final scores (monotonicity preserved).
Specifically guards against the earlier bug where raw source-level word-count integers
(0..6+) leaked through meta and inflated the final score to 15+.
"""

import sys
import os

# Ensure src/ is importable (matches existing test convention)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from memchorus.relevance_engine import RelevanceScorer, ContextWeight


@pytest.fixture
def scorer():
    return RelevanceScorer()


class TestScoreBounds:
    """Final scores should be bounded in [0, ~3] range after G3 fix."""

    def test_empty_result_is_near_zero(self, scorer):
        result = {"key": "empty", "content": "", "source": "hermes_default"}
        s = scorer.score(result, "some query here")
        assert 0.0 <= s <= 1.0

    def test_perfect_match_still_bounded(self, scorer):
        # Even a perfect semantic match should not exceed ~3.0
        query = "architecture decision project planning"
        result = {
            "key": "perfect",
            "content": (
                "We decided to use architecture-based planning with modular design."
                " This was the primary project decision for the planning phase."
            ),
            "source": "hermes_default",
        }
        s = scorer.score(result, query)
        assert 0.1 < s <= 3.0, f"Score {s} exceeds expected max ~3.0"

    def test_low_match_stays_low(self, scorer):
        result = {"key": "unrelated", "content": "The weather today was sunny", "source": "mempalace"}
        s = scorer.score(result, "implement database schema migration fix")
        assert 0.0 <= s < 1.0

    def test_raw_source_score_not_leaked(self, scorer):
        # The source result dict may carry a raw integer score from hermes_default.
        # After G3 fix it should NOT inflate the final output.
        result = {
            "key": "k1",
            "content": "test content about project structure",
            "source": "hermes_default",
            "score": 6,  # raw word-count integer (pre-G3 leak)
        }
        s = scorer.score(result, "project")
        assert 0.0 <= s <= 3.0, f"Raw score 6 leaked through, final={s}"

    def test_monotonic_quality(self, scorer):
        """Better content → higher score."""
        query = "search relevance scoring algorithm"
        worse = {"key": "w", "content": "database connection timeout handling", "source": "hermes_default"}
        better = {"key": "b", "content": (
            "The search relevance engine uses a multi-dimensional scoring algorithm."
            " Relevance scoring combines semantic quality, recency decay, and source bias."
        ), "source": "hermes_default"}
        s_worse = scorer.score(worse, query)
        s_better = scorer.score(better, query)
        assert s_better >= s_worse, (
            f"Monotonicity failed: better={s_better:.4f} < worse={s_worse:.4f}"
        )

    def test_f1_quality_component(self, scorer):
        """_score_quality should return [0, 1]."""
        query = "implement search scoring fix"
        related = "I need to implement the search scoring fixes in relevancy engine"
        unrelated = "The weather was absolutely beautiful today"
        q1 = RelevanceScorer._score_quality(query, related)
        q2 = RelevanceScorer._score_quality(query, unrelated)
        assert 0.0 <= q1 <= 1.0
        assert 0.0 <= q2 <= 1.0
        assert q1 > q2

    def test_score_and_rank_preserves_bounds(self, scorer):
        """score_and_rank results should all be bounded."""
        results = [
            {"key": f"k{i}", "content": f"test result number {i} with some text", "source": "hermes_default", "score": i * 2}
            for i in range(1, 6)
        ]
        ranked = scorer.score_and_rank(results, query="test results")
        for r in ranked:
            assert 0.0 <= r.score <= 3.0, (
                f"RankedResult {r.key} score={r.score} out of bounds"
            )

