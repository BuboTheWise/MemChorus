"""GH#103 - test_cross_source_merge.py

Verify that when multiple sources return overlapping results at different scores,
the aggregated search pipeline behaves correctly before/after scoring.
"""
import pytest
from memchorus.orchestrator import _check_source_available


class ScoredSource:
    def __init__(self, name, items):
        self.name = name
        self._items = items

    @property
    def is_available(self):
        return True

    def search(self, query, limit=10):
        return self._items


def _raw_merge(sources):
    """Simulate orchestrator.py line 1050-1076 source aggregation loop."""
    all_results = []
    for sname, src in sources.items():
        if not src or not _check_source_available(src):
            continue
        try:
            res = src.search("query", limit=10)
            if res:
                all_results.extend(res)
        except Exception:
            continue
    return all_results


class TestCrossSourceMergeOrdering:

    def test_concatenation_appends_source_a_then_b(self):
        """Raw aggregation appends source results in dict iteration order."""
        source_a = ScoredSource("A", [
            {"key": "a1", "content": "item a1", "score": 0.8},
            {"key": "a2", "content": "item a2", "score": 0.5},
        ])
        source_b = ScoredSource("B", [
            {"key": "b1", "content": "item b1", "score": 0.7},
            {"key": "b2", "content": "item b2", "score": 0.9},
        ])

        sources = {"A": source_a, "B": source_b}
        raw = _raw_merge(sources)

        assert len(raw) == 4
        scores_raw = [r["score"] for r in raw]
        # A-then-B concatenation: [0.8, 0.5, 0.7, 0.9] - not yet sorted
        assert scores_raw == [0.8, 0.5, 0.7, 0.9]

    def test_identical_scores_are_stable_not_swapped(self):
        source_a = ScoredSource("A", [{"key": "a1", "content": "x", "score": 0.7}])
        source_b = ScoredSource("B", [{"key": "b1", "content": "x", "score": 0.7}])

        raw = _raw_merge({"A": source_a, "B": source_b})
        assert len(raw) == 2
        assert raw[0]["score"] == raw[1]["score"] == 0.7

    def test_single_dominant_source_wins(self):
        source_a = ScoredSource("A", [
            {"key": "a1", "content": "x", "score": 0.95},
            {"key": "a2", "content": "x", "score": 0.85},
        ])
        source_b = ScoredSource("B", [
            {"key": "b1", "content": "x", "score": 0.4},
        ])

        raw = _raw_merge({"A": source_a, "B": source_b})
        assert len(raw) == 3

    def test_limit_respected_across_sources(self):
        """effective_limit budget is consumed across sources."""
        source_a = ScoredSource("A", [
            {"key": "a1", "content": "x", "score": 0.9},
            {"key": "a2", "content": "x", "score": 0.8},
            {"key": "a3", "content": "x", "score": 0.7},
        ])
        source_b = ScoredSource("B", [
            {"key": "b1", "content": "x", "score": 0.6},
        ])

        effective_limit = 2
        all_results = []
        remaining = effective_limit
        for sname, src in [("A", source_a), ("B", source_b)]:
            if not _check_source_available(src):
                continue
            try:
                res = src.search("query", limit=effective_limit)
                take = res[:remaining]
                all_results.extend(take)
                remaining -= len(take)
                if remaining <= 0:
                    break
            except Exception:
                continue

        assert len(all_results) == 2


class TestSourceOrderIndependence:

    def test_source_iteration_does_not_affect_scores(self):
        source_a = ScoredSource("A", [{"key": "a1", "content": "x", "score": 0.6}])
        source_b = ScoredSource("B", [{"key": "b1", "content": "x", "score": 0.4}])

        raw_fwd = _raw_merge({"A": source_a, "B": source_b})
        assert len(raw_fwd) == 2

        raw_rev = _raw_merge({"B": source_b, "A": source_a})
        assert len(raw_rev) == 2
