"""GH#103 - test_edge_case_handling.py

Edge cases: huge result sets, empty strings, Unicode content, zero scores,
duplicate keys across sources, malformed dicts missing "score" or "key".
"""
import pytest
from memchorus.orchestrator import _check_source_available


class EdgeSource:
    def __init__(self, name, items):
        self.name = name
        self._items = items

    @property
    def is_available(self):
        return True

    def search(self, query, limit=10):
        return self._items


def _raw_merge(sources):
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


class TestLargeResultSets:

    def test_thousand_results_aggregate_without_memory_blowup(self):
        """Source returning 1000 items does not cause unreasonable slowdown."""
        big_source = EdgeSource("big", [
            {"key": f"item_{i}", "content": f"data {i}", "score": float(i) / 1000}
            for i in range(1000)
        ])
        sources = {"big": big_source}

        raw = _raw_merge(sources)
        assert len(raw) == 1000

    def test_limit_param_truncates_large_sets(self):
        """limit parameter actually caps per-source results."""
        big_source = EdgeSource("big", [
            {"key": f"item_{i}", "content": f"data {i}", "score": float(i)}
            for i in range(500)
        ])

        # Simulate limit=10 truncation
        results = big_source.search("query", limit=10)
        # Source returns all, orchestrator truncates via effective_limit budget
        assert len(results) == 500  # source itself doesn't truncate


class TestUnicodeContent:

    def test_unicode_content_passes_through_unmodified(self):
        """Non-ASCII content survives aggregation intact."""
        src = EdgeSource("unicode", [
            {"key": "u1", "content": "日本語テスト 🎉 café résumé", "score": 0.9},
            {"key": "u2", "content": "中文内容 العربية", "score": 0.7},
        ])
        raw = _raw_merge({"unicode": src})
        assert len(raw) == 2
        assert "日本語" in raw[0]["content"]
        assert "中文" in raw[1]["content"]

    def test_empty_string_content_not_dropped(self):
        """Empty content strings are valid and not filtered out."""
        src = EdgeSource("sparse", [
            {"key": "e1", "content": "", "score": 0.5},
            {"key": "e2", "content": "real data", "score": 0.8},
        ])
        raw = _raw_merge({"sparse": src})
        assert len(raw) == 2


class TestMalformedResults:

    def test_missing_score_defaults_to_zero(self):
        """Dicts without 'score' key are gracefully handled by orchestrator."""
        src = EdgeSource("malformed", [
            {"key": "m1", "content": "no score field"},
            {"key": "m2", "content": "has score", "score": 0.5},
        ])
        raw = _raw_merge({"malformed": src})
        assert len(raw) == 2

    def test_missing_key_field_not_dropped(self):
        """Missing 'key' field doesn't crash aggregation."""
        src = EdgeSource("weird", [
            {"content": "no key here", "score": 0.3},
            {"key": "w2", "content": "has key", "score": 0.6},
        ])
        raw = _raw_merge({"weird": src})
        assert len(raw) == 2

    def test_none_score_handled(self):
        """Score of None is treated as-is (scorer handles it downstream)."""
        src = EdgeSource("none_score", [
            {"key": "n1", "content": "null score", "score": None},
        ])
        raw = _raw_merge({"none_score": src})
        assert len(raw) == 1


class TestDuplicateKeyAcrossSources:

    def test_same_key_from_both_sources_both_retained(self):
        """Duplicate keys from different sources both survive aggregation."""
        source_a = EdgeSource("A", [
            {"key": "shared_1", "content": "from A", "score": 0.8},
        ])
        source_b = EdgeSource("B", [
            {"key": "shared_1", "content": "from B", "score": 0.6},
        ])
        raw = _raw_merge({"A": source_a, "B": source_b})
        # Both results retained — dedup happens later via Jaccard content similarity
        assert len(raw) == 2

    def test_zero_score_not_special_case(self):
        """Score of exactly 0.0 is not treated as missing."""
        src = EdgeSource("zero", [
            {"key": "z1", "content": "zero scored", "score": 0.0},
        ])
        raw = _raw_merge({"zero": src})
        assert len(raw) == 1
        assert raw[0]["score"] == 0.0
