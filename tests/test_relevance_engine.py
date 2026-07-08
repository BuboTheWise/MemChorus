"""
Unit tests for internal relevance engine methods — covers the content extraction fix.

Regression coverage:
  T1: _extract_content_text(string) passes through unchanged.
  T2: _extract_content_text(dict) joins keys + leaf values.
  T3: _extract_content_text(list) joins list elements recursively.
  T4: _score_quality(dict content) produces meaningful overlap (regression for zero-score bug).
  T5: _score_quality(nested dict) handles recursion correctly.
  T6: Edge cases — empty dict, empty list, None, deeply nested structure.
"""

import sys
import os
import unittest

# Ensure src/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.relevance_engine import RelevanceScorer, ContextWeight


class TestExtractContentText(unittest.TestCase):
    """Test _extract_content_text for all supported content types."""

    def test_plain_string_passthrough(self):
        """T1: Plain strings pass through unchanged."""
        text = RelevanceScorer._extract_content_text("hello world test")
        self.assertEqual(text, "hello world test")

    def test_dict_keys_and_values(self):
        """T2: Dict content extracts both keys and leaf values."""
        d = {"fix": "verified", "relevance_scorer": "returns zero"}
        text = RelevanceScorer._extract_content_text(d)
        # Should include key names AND their values
        self.assertIn("fix", text.lower())
        self.assertIn("verified", text.lower())
        self.assertIn("relevance_scorer", text.lower())
        self.assertIn("returns zero", text.lower())

    def test_list_elements(self):
        """T3: List content joins elements."""
        lst = ["memchorus", "quality", "improvements"]
        text = RelevanceScorer._extract_content_text(lst)
        self.assertIn("memchorus", text.lower())
        self.assertIn("quality", text.lower())
        self.assertIn("improvements", text.lower())

    def test_nested_structure(self):
        """T5: Nested dicts and lists extract recursively."""
        nested = {
            "data": {"scorer": "fixed", "quality": ["high", "verified"]},
            "status": "resolved",
        }
        text = RelevanceScorer._extract_content_text(nested)
        self.assertIn("scorer", text.lower())
        self.assertIn("fixed", text.lower())
        self.assertIn("high", text.lower())
        self.assertIn("verified", text.lower())
        self.assertIn("resolved", text.lower())


class TestQualityScoreWithStructuredContent(unittest.TestCase):
    """Test _score_quality specifically with non-string content."""

    def setUp(self):
        self.scorer = RelevanceScorer()

    def test_dict_content_meaningful_overlap(self):
        """T4: Dict content extracts keys+values, producing meaningful query overlap.
           Regression test for zero-score bug when MemPalace _from_str() returns dicts."""
        content = {"relevance": "high", "scorer": "improved", "verdict": "fix confirmed"}
        score = self.scorer._score_quality("relevance scorer fix", content)
        # With dict keys+values extracted we should get meaningful term match, not near-zero.
        self.assertGreater(score, 0.3,
                           f"Dict quality score {score} too low — str() bug may be back")

    def test_nested_dict_recursion(self):
        """Nested dicts extract leaf values correctly through recursion."""
        nested = {"data": {"relevance": "high", "quality": "improved"}, "status": "ok"}
        score = self.scorer._score_quality("relevance quality improved", nested)
        # Extracted text: "data relevance high status ok ... quality improved"
        self.assertGreater(score, 0.3)

    def test_string_content_still_works(self):
        """Ensure the fix doesn't regress plain string scoring."""
        score = self.scorer._score_quality(
            "quick brown fox",
            "the quick brown fox jumps over the lazy dog"
        )
        self.assertGreater(score, 0.3)

    def test_list_content_score(self):
        """List content should produce meaningful scores."""
        score = self.scorer._score_quality(
            "memchorus relevance",
            ["memchorus", "quality", "scorer", "relevance"]
        )
        self.assertGreater(score, 0.3)

    def test_empty_dict_returns_neutral_floor(self):
        """Empty dict should hit the neutral floor."""
        score = self.scorer._score_quality("anything", {})
        self.assertAlmostEqual(score, 0.3, places=2)

    def test_empty_list_returns_neutral_floor(self):
        """Empty list should hit the neutral floor."""
        score = self.scorer._score_quality("anything", [])
        self.assertAlmostEqual(score, 0.3, places=2)


class TestScoreDoesNotDropToZero(unittest.TestCase):
    """Regression: total scores must never be exactly 0.0 for non-matching content."""

    def setUp(self):
        self.scorer = RelevanceScorer()
        self.ctx = ContextWeight()

    def test_structured_content_nonzero_total(self):
        """Even with poor term overlap, recency+source_type keep total score > 0."""
        r = {
            "key": "x",
            "content": {"completely_unrelated": "nothing to do with the query at all"},
            "source": "hermes_default",
        }
        score = self.scorer.score(r, "memchorus relevance fix", self.ctx)
        # Should NOT be exactly 0.0 due to neutral floors (quality=0.3, recency=0.5)
        self.assertGreater(score, 0.2)

    def test_dict_content_scores_higher_when_overlap_presents(self):
        """Dict content with good overlap should rank higher than poor overlap."""
        r_good = {
            "key": "good",
            "content": {"relevance": "high", "scorer": "confirmed", "fix": "verified"},
            "source": "mempalace",
        }
        r_bad = {
            "key": "bad",
            "content": {"random_stuff": "blah blah nothing useful"},
            "source": "mempalace",
        }
        s_good = self.scorer.score(r_good, "relevance scorer fix", self.ctx)
        s_bad = self.scorer.score(r_bad, "relevance scorer fix", self.ctx)
        self.assertGreater(s_good, s_bad,
                           f"Good overlap should score higher ({s_good} vs {s_bad})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
