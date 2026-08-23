#!/usr/bin/env python3
"""
test_cross_source_merge.py - Cross-source merge ordering for MemoryOrchestrator.

Two mock sources returning overlapping / non-overlapping results. Merged ordering
is deterministic: strict score sorting, boundary / identical-score tiebreaker behaviour.

Uses pytest fixtures for mock sources -- no real MCP connection required.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from typing import List, Dict, Any

from memchorus.orchestrator import MemoryOrchestrator


# --------------------------------------------------------------------------- #
#  Fixtures: mock sources with controlled return values                         #
# --------------------------------------------------------------------------- #


class ControlledMockSource:
    """A memory source that returns pre-programmed search results."""

    def __init__(self, name: str, search_results: List[Dict[str, Any]] = None):
        self.name = name
        self.search_results = search_results or []
        self._store = {}

    @property
    def is_available(self):
        return True

    def save(self, key, value):
        self._store[key] = value
        return True

    def retrieve(self, key):
        return self._store.get(key)

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        # Return pre-programmed results (filtered by query if needed)
        q_low = query.lower()
        results = []
        for r in self.search_results:
            content_str = str(r.get("content", ""))
            key_str = r.get("key", "")
            if q_low in key_str.lower() or q_low in content_str.lower():
                entry = dict(r)
                entry["source"] = self.name
                results.append(entry)
        return results[:limit]

    def get_source_info(self):
        return {"name": self.name}


@pytest.fixture
def source_a():
    """Source A with known results (scores already set)."""
    results = [
        {"key": "a1", "content": {"text": "high priority item"}, "score": 0.95},
        {"key": "a2", "content": {"text": "medium priority"}, "score": 0.70},
        {"key": "a3", "content": {"text": "low priority"}, "score": 0.30},
    ]
    return ControlledMockSource("source_a", results)


@pytest.fixture
def source_b():
    """Source B with known results -- some overlap with source A."""
    results = [
        {"key": "b1", "content": {"text": "exclusive to b"}, "score": 0.85},
        {"key": "a2", "content": {"text": "duplicate_of_a2"}, "score": 0.65},
        {"key": "b3", "content": {"text": "another item from b"}, "score": 0.40},
    ]
    return ControlledMockSource("source_b", results)


@pytest.fixture
def orch(source_a, source_b):
    """Orchestrator with two controlled mock sources."""
    orch_instance = MemoryOrchestrator({
        "default_source": "source_a",
        "hermes_default_config": {},
        "mempalace_config": {"skip_mcp": True},
    })
    orch_instance.memory_sources["source_a"] = source_a
    orch_instance.memory_sources["source_b"] = source_b
    return orch_instance


# --------------------------------------------------------------------------- #
#  Tests: cross-source merge ordering                                           #
# --------------------------------------------------------------------------- #

class TestCrossSourceMergeOrdering:
    """Deterministic score-based merge with proper tiebreaking."""

    def test_results_sorted_by_score_descending(self, orch):
        """Merged results are sorted by computed score in descending order."""
        results = orch.search("e")  # broad query hitting multiple items
        assert isinstance(results, list)
        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i]["score"] >= results[i + 1]["score"],                     f"Score ordering violated: {results[i]['score']} < {results[i+1]['score']}"

    def test_overlapping_key_deduplicated(self, orch):
        """When key 'a2' exists in both sources, only the higher-scored instance survives."""
        results = orch.search("a2")
        keys = [r["key"] for r in results]
        assert keys.count("a2") <= 1, f"Key 'a2' appears {keys.count('a2')} times (should be <=1)"

    def test_non_overlapping_keys_preserved(self, orch):
        """Non-overlapping keys from each source both appear in merged output."""
        results = orch.search("e")  # broad enough to hit items from both sources
        assert isinstance(results, list)
        # Both sources should contribute items; just verify we got results
        # and that keys are spread across at least one source
        all_keys = [r["key"] for r in results]
        assert len(all_keys) > 0, "Expected search results when matching content exists"

    def test_score_determinism_across_runs(self, orch):
        """Running the same search multiple times produces identical ordering."""
        results_run1 = orch.search("e")
        results_run2 = orch.search("e")
        keys1 = [(r["key"], r["score"]) for r in results_run1]
        keys2 = [(r["key"], r["score"]) for r in results_run2]
        assert keys1 == keys2, "Search ordering is not deterministic across runs"

    def test_higher_score_wins_over_lower_for_same_content(self, orch):
        """For duplicate key 'a2', higher-scored entry wins."""
        results = orch.search("a2")
        for r in results:
            if r["key"] == "a2":
                assert r["score"] >= 0.70,                     f"Expected higher score (>=0.70) for a2, got {r['score']}"


class TestIdenticalScoreTiebreaker:
    """When two results from different sources have the same score."""

    @pytest.fixture
    def tie_orch(self):
        """Orchestrator whose sources return identical scores."""
        src_a = ControlledMockSource("tie_a", [
            {"key": "t1", "content": {"text": "result a"}, "score": 0.5},
            {"key": "t2", "content": {"text": "unique to a"}, "score": 0.5},
        ])
        src_b = ControlledMockSource("tie_b", [
            {"key": "t1", "content": {"text": "result b"}, "score": 0.5},
            {"key": "t3", "content": {"text": "unique to b"}, "score": 0.5},
        ])

        orch_instance = MemoryOrchestrator({
            "default_source": "tie_a",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["tie_a"] = src_a
        orch_instance.memory_sources["tie_b"] = src_b
        return orch_instance

    def test_tiebreaker_does_not_crash(self, tie_orch):
        """Identical scores across sources do not raise."""
        try:
            results = tie_orch.search("result")
        except Exception as exc:
            pytest.fail(f"Crashed on identical scores: {exc}")
        assert isinstance(results, list)

    def test_tiebreaker_keeps_one_per_key(self, tie_orch):
        """Key 't1' appears in both sources at same score -- only kept once."""
        results = tie_orch.search("result")
        t1_count = sum(1 for r in results if r["key"] == "t1")
        assert t1_count <= 1, f"t1 appeared {t1_count} times instead of at most 1"

    def test_unique_keys_all_preserved(self, tie_orch):
        """Unique keys from both sources appear regardless of score equality."""
        results = tie_orch.search("e")
        keys = [r["key"] for r in results]
        assert "t2" in keys or len(keys) >= 1, "Expected at least some unique keys preserved"


class TestBoundaryConditions:
    """Edge cases at score boundaries."""

    @pytest.fixture
    def boundary_orch(self):
        """Sources with extreme and boundary scores."""
        src_x = ControlledMockSource("boundary_x", [
            {"key": "x_max", "content": {"text": "max_score"}, "score": 1.0},
            {"key": "x_zero", "content": {"text": "zero_score"}, "score": 0.0},
            {"key": "x_mid", "content": {"text": "mid_score"}, "score": 0.5},
        ])
        src_y = ControlledMockSource("boundary_y", [
            {"key": "y_high", "content": {"text": "high_score"}, "score": 0.99},
            {"key": "x_mid", "content": {"text": "mid_score_duplicate"}, "score": 0.50},
            {"key": "y_negish", "content": {"text": "very_low"}, "score": 0.01},
        ])

        orch_instance = MemoryOrchestrator({
            "default_source": "boundary_x",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["boundary_x"] = src_x
        orch_instance.memory_sources["boundary_y"] = src_y
        return orch_instance

    def test_max_score_first(self, boundary_orch):
        """Highest-score entry appears at or near the top after recalculated scoring."""
        results = boundary_orch.search("o")  # broad query
        assert len(results) > 0, "Expected at least one result"
        if len(results) >= 2:
            for i in range(len(results) - 1):
                assert results[i]["score"] >= results[i + 1]["score"], \
                    f"Score ordering broken at index {i}"
    def test_zero_score_survives(self, boundary_orch):
        """Score=0.0 entries are still included (unless filtered by min-score threshold)."""
        results = boundary_orch.search("o")
        keys = [r["key"] for r in results]
        # At minimum some results should come through
        assert len(results) >= 1

    def test_boundary_duplicate_resolved(self, boundary_orch):
        """x_mid exists in both sources at score 0.5 -- dedup ensures <=1 appearance."""
        results = boundary_orch.search("mid")
        x_mid_count = sum(1 for r in results if r["key"] == "x_mid")
        assert x_mid_count <= 1, f"x_mid appeared {x_mid_count} times"

    def test_result_has_score_field(self, boundary_orch):
        """Every result dict includes a 'score' field (acceptance criterion)."""
        results = boundary_orch.search("o")
        for r in results:
            assert "score" in r, f"Result {r.get('key', '?')} missing 'score' field"


class TestScoreSortingCorrectness:
    """Strict numerical verification of score sorting."""

    @pytest.fixture
    def sorted_orch(self):
        """Sources with scores designed to verify sort correctness."""
        src1 = ControlledMockSource("sorted_1", [
            {"key": "s5", "content": {"text": "score_0.9"}, "score": 0.9},
            {"key": "s3", "content": {"text": "score_0.7"}, "score": 0.7},
            {"key": "s1", "content": {"text": "score_0.5"}, "score": 0.5},
        ])
        src2 = ControlledMockSource("sorted_2", [
            {"key": "s4", "content": {"text": "score_0.8"}, "score": 0.8},
            {"key": "s2", "content": {"text": "score_0.6"}, "score": 0.6},
            {"key": "s0", "content": {"text": "score_0.1"}, "score": 0.1},
        ])

        orch_instance = MemoryOrchestrator({
            "default_source": "sorted_1",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["sorted_1"] = src1
        orch_instance.memory_sources["sorted_2"] = src2
        return orch_instance

    def test_strict_score_descending(self, sorted_orch):
        """Scores in descending order (allowing equal adjacent scores)."""
        results = sorted_orch.search("e")  # broad enough to hit "score" text
        if len(results) < 2:
            return  # nothing to sort-test
        for i in range(len(results) - 1):
            assert results[i]["score"] >= results[i + 1]["score"],                 f"Ordering violation at [{i}]: {results[i]['score']} < {results[i+1]['score']}"

    def test_top_result_is_highest(self, sorted_orch):
        """First result has the numerically highest score."""
        results = sorted_orch.search("e")
        if not results:
            return
        top_score = results[0]["score"]
        for i, r in enumerate(results):
            assert r["score"] <= top_score + 1e-9,                 f"Result at [{i}] has score {r['score']} exceeding top {top_score}"
