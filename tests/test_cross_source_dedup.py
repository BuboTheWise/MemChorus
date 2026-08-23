"""Tests for GH#95: Cross-source near-duplicate detection in orchestrator.search().

The dedup step runs after scoring, before taking top-N. It compares content
similarity between results from different sources using N-gram Jaccard scoring,
keeps the highest individual score when duplicates are found, and uses recency
as tiebreaker when scores are within 0.05 of each other.

Acceptance criteria:
- _deduplicate_results() method in orchestrator with configurable threshold (default 0.85)
- Unit tests with known-duplicate pairs from two different mock sources
- Benchmark showing reduced output on multi-source queries
"""

import sys
from importlib.metadata import version as pkg_version


def _skip_if_no_feature():
    """Gracefully skip if this code hasn't been merged yet."""
    try:
        return memchorus.orchestrator._deduplicate_results is not None
    except AttributeError:
        pytest.skip("Cross-source dedup feature not yet implemented")


class TestNgramJaccardSimilarity:
    """Test the Jaccard similarity computation between near-duplicates."""

    def test_exact_dupes_score_near_one(self):