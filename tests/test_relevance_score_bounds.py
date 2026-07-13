"""Regression tests for RelevanceScorer score bounds.

Proves ``score()`` always returns values in [0, 1] regardless of:
- Custom ContextWeight that don't sum to 1.0
- Edge-case content (empty, dict, list)
- Source types with missing priors

These tests were added after diagnostic output showed scores reaching
15.0 due to unnormalized weights and raw quality inputs hitting the
additive score formula.  The fix L1-normalises weights and clamps output.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone, timedelta

from memchorus.relevance_engine import RelevanceScorer, ContextWeight


class TestScoreBoundsDefaultWeights(unittest.TestCase):
    """score() stays in [0, 1] with default weights."""

    def setUp(self):
        self.scorer = RelevanceScorer()
        self.ctx = ContextWeight()
        self.now_iso = datetime.now(timezone.utc).isoformat()

    def test_perfect_quality_recent(self):
        """Maximum quality + fresh timestamp -> score <= 1.0."""
        result = {
            "key": "k1",
            "content": "alpha beta gamma delta epsilon",
            "source": "hermes_default",
            "timestamp": self.now_iso,
        }
        s = self.scorer.score(result, "alpha beta gamma delta epsilon", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_no_overlap(self):
        """Zero term overlap -> score >= 0 (neutral floor from quality + priors)."""
        result = {
            "key": "k2",
            "content": "completely unrelated text xyzzy",
            "source": "hermes_default",
        }
        s = self.scorer.score(result, "alpha beta gamma delta epsilon", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_empty_content(self):
        result = {
            "key": "k3",
            "content": "",
            "source": "hermes_default",
        }
        s = self.scorer.score(result, "any query at all", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_empty_query(self):
        result = {
            "key": "k4",
            "content": "some content that matters",
            "source": "hermes_default",
        }
        s = self.scorer.score(result, "", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_dict_content(self):
        result = {
            "key": "k5",
            "content": {"title": "relevant item", "body": "details about the topic"},
            "source": "mempalace",
        }
        s = self.scorer.score(result, "relevant details topic", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_list_content(self):
        result = {
            "key": "k6",
            "content": ["item one about scoring", "item two about ranking"],
            "source": "hermes_default",
        }
        s = self.scorer.score(result, "scoring ranking items", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_unknown_source(self):
        result = {
            "key": "k7",
            "content": "some relevant content here",
            "source": "totally_unknown_source_xyz",
        }
        s = self.scorer.score(result, "relevant content", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_future_timestamp(self):
        """Future date should produce valid score (recency -> 1.0 with delta=0)."""
        future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        result = {
            "key": "k8",
            "content": "relevant content matching the query",
            "source": "hermes_default",
            "timestamp": future,
        }
        s = self.scorer.score(result, "relevant content query", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_ancient_timestamp(self):
        result = {
            "key": "k9",
            "content": "relevant content matching the query",
            "source": "hermes_default",
            "timestamp": "2000-01-01T00:00:00+00:00",
        }
        s = self.scorer.score(result, "relevant content query", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_domain_hint(self):
        result = {
            "key": "k10",
            "content": "knowledge graph storage retrieval",
            "source": "mempalace",
            "_domain": "graph",
        }
        s = self.scorer.score(result, "knowledge graph", self.ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)


class TestScoreBoundsCustomWeights(unittest.TestCase):
    """score() stays in [0, 1] even when weights sum != 1.0 (L1-norm fix)."""

    def setUp(self):
        self.scorer = RelevanceScorer()

    def test_large_weights(self):
        """Quality/Recency/Source weights all set to large values."""
        ctx = ContextWeight(quality_weight=2.0, recency_weight=2.0, source_type_weight=2.0)
        result = {
            "key": "lw1",
            "content": "alpha beta gamma scoring relevance memory normalization",
            "source": "hermes_default",
        }
        s = self.scorer.score(result, "alpha beta gamma scoring relevance", ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_extreme_weights(self):
        """One weight massively dominant."""
        ctx = ContextWeight(quality_weight=100.0, recency_weight=0.1, source_type_weight=0.1)
        result = {
            "key": "ew1",
            "content": "perfect match alpha beta gamma delta epsilon zeta iota kappa",
            "source": "hermes_default",
        }
        s = self.scorer.score(
            result, "perfect match alpha beta gamma delta", ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_unbalanced_weights_sum_over_five(self):
        """Weights that sum to >5 (the case previously producing scores of 15+)."""
        ctx = ContextWeight(quality_weight=10.0, recency_weight=5.0, source_type_weight=3.0)
        result = {
            "key": "ub1",
            "content": "alpha beta gamma delta scoring engine memory relevance normalisation",
            "source": "hermes_default",
        }
        s = self.scorer.score(
            result, "scoring engine memory relevance", ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_zero_weight_fallback(self):
        """All weights zero -> equal default contribution."""
        ctx = ContextWeight(quality_weight=0.0, recency_weight=0.0, source_type_weight=0.0)
        result = {
            "key": "zw1",
            "content": "some relevant content here for testing",
            "source": "hermes_default",
        }
        s = self.scorer.score(result, "relevant content testing", ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_negative_weight_clamp(self):
        """Negative weights should still produce valid bounded output."""
        ctx = ContextWeight(quality_weight=-1.0, recency_weight=2.0, source_type_weight=1.0)
        result = {
            "key": "nw1",
            "content": "alpha beta gamma delta epsilon zeta",
            "source": "hermes_default",
        }
        s = self.scorer.score(result, "alpha beta gamma", ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)

    def test_mixed_sign_weights(self):
        """Mixed positive and negative weights still bounded."""
        ctx = ContextWeight(quality_weight=5.0, recency_weight=-2.0, source_type_weight=3.0)
        result = {
            "key": "ms1",
            "content": "scoring relevance engine memory normalisation bounds",
            "source": "hermes_default",
        }
        s = self.scorer.score(result, "scoring relevance engine", ctx)
        self.assertGreaterEqual(s, 0.0)
        self.assertLessEqual(s, 1.0)


class TestScoreMonotonicity(unittest.TestCase):
    """Higher matching should produce higher scores (monotonicity check)."""

    def setUp(self):
        self.scorer = RelevanceScorer()
        self.ctx = ContextWeight()

    def test_more_overlap_higher_quality(self):
        """_score_quality is monotonic with overlap."""
        base_content = "the quick brown fox jumps over the lazy dog"
        q_low = "xylophone zirconium"       # zero overlap
        q_high = "quick brown fox lazy dog"  # high overlap

        s_low = self.scorer._score_quality(q_low, base_content)
        s_high = self.scorer._score_quality(q_high, base_content)
        self.assertLess(s_low, s_high)

    def test_same_score_ordering_preserved(self):
        """Results with higher overlap rank above others in score_and_rank."""
        results = [
            {"key": "poor", "content": "totally unrelated information about cats"},
            {"key": "good", "content": "memory scoring relevance engine normalisation bounds tests"},
        ]
        ranked = self.scorer.score_and_rank(
            results, "scoring relevance memory", self.ctx)
        self.assertEqual(ranked[0].key, "good")


if __name__ == "__main__":
    unittest.main()
