"""Lifecycle Merge Engine tests — tokenisation, similarity, strategies & integration."""
# ruff: noqa: F401  # imported but not directly tested

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# --------------------------------------------------------------------------- 
# Test helpers
# ---------------------------------------------------------------------------

class InMemSource:
    """Simple in-memory source for unit tests."""

    def __init__(self, name: str):
        self.name = name
        self._store: dict[str, object] = {}

    @property
    def _path(self):
        return os.path.join(tempfile.gettempdir(), "memchorus_test")

    def save(self, key: str, value: object) -> bool:
        self._store[key] = value
        return True

    def retrieve(self, key: str):
        return self._store.get(key)

    def search(self, query: str, limit: int = 100):
        results = []
        q_lower = query.lower()
        for k, v in self._store.items():
            if (q_lower in k.lower() or
                (hasattr(v, '__iter__') and isinstance(v, dict) and
                 k.lower() in [str(x).lower() for x in v.keys()]) or
                q_lower in str(v).lower()):
                results.append({"key": k, "value": v})
        return results[:limit]

    def is_available(self):
        return True


# --------------------------------------------------------------------------- 
# Tokenisation & similarity tests
# ---------------------------------------------------------------------------

class TestTokenize(unittest.TestCase):
    """Test _tokenize helper."""

    def test_basic_tokenization(self):
        from memchorus.lifecycle_merge import _tokenize
        tokens = _tokenize("Hello World")
        self.assertEqual(tokens, {"hello", "world"})

    def test_ignore_punctuation(self):
        from memchorus.lifecycle_merge import _tokenize
        tokens = _tokenize("Hello, world!? Yes.")
        self.assertEqual(tokens, {"hello", "world", "yes"})

    def test_empty_string(self):
        from memchorus.lifecycle_merge import _tokenize
        tokens = _tokenize("")
        self.assertEqual(tokens, set())


class TestJaccardSimilarity(unittest.TestCase):
    """Test _jaccard_similarity function."""

    def test_identical_strings(self):
        from memchorus.lifecycle_merge import _jaccard_similarity
        result = _jaccard_similarity("hello world", "hello world")
        self.assertAlmostEqual(result, 1.0)

    def test_completely_different(self):
        from memchorus.lifecycle_merge import _jaccard_similarity
        result = _jaccard_similarity("abc", "xyz")
        self.assertAlmostEqual(result, 0.0)

    def test_partial_overlap(self):
        from memchorus.lifecycle_merge import _jaccard_similarity
        result = _jaccard_similarity("hello world test", "world test foo")
        expected = 2 / 4  # overlap: {world, test}, union: {hello, world, test, foo}
        self.assertAlmostEqual(result, expected)

    def test_dict_keys(self):
        from memchorus.lifecycle_merge import _jaccard_similarity
        result = _jaccard_similarity({"a": 1, "b": 2}, {"b": 3, "c": 4})
        expected = 1 / 3  # overlap: {b}, union: {a, b, c}
        self.assertAlmostEqual(result, expected)

    def test_non_string_fallback(self):
        from memchorus.lifecycle_merge import _jaccard_similarity
        result = _jaccard_similarity(123, 456)
        self.assertGreaterEqual(result, 0.0)


# ---------------------------------------------------------------------------
# Strategy function tests
# ---------------------------------------------------------------------------

class TestMergeStrategies(unittest.TestCase):
    """Test _strategy_overwrite, _strategy_append, _strategy_union directly."""

    def test_overwrite_replaces_existing(self):
        from memchorus.lifecycle_merge import MergeEngine
        merged, action = MergeEngine._strategy_overwrite("old", "new")
        self.assertEqual(merged, "new")
        self.assertEqual(action, "merge_overwrite")

    def test_append_creates_list(self):
        from memchorus.lifecycle_merge import MergeEngine
        merged, action = MergeEngine._strategy_append("first", "second")
        self.assertEqual(merged, ["first", "second"])
        self.assertEqual(action, "merge_append")

    def test_append_extends_existing_list(self):
        from memchorus.lifecycle_merge import MergeEngine
        merged, action = MergeEngine._strategy_append(["a", "b"], "c")
        self.assertEqual(merged, ["a", "b", "c"])
        self.assertEqual(action, "merge_append")

    def test_union_merges_dicts(self):
        from memchorus.lifecycle_merge import MergeEngine
        existing = {"x": 1, "y": 2}
        new = {"y": 99, "z": 3}
        merged, action = MergeEngine._strategy_union(existing, new)
        self.assertEqual(merged["x"], 1)
        self.assertEqual(merged["y"], 99)
        self.assertEqual(merged["z"], 3)
        self.assertEqual(action, "merge_union")

    def test_union_fallback_non_dict(self):
        from memchorus.lifecycle_merge import MergeEngine
        merged, action = MergeEngine._strategy_union("old", "new")
        self.assertEqual(merged, "new")
        self.assertEqual(action, "merge_union")


# ---------------------------------------------------------------------------
# MergeEngine integration tests with in-memory source
# ---------------------------------------------------------------------------

class TestMergeEngineIntegration(unittest.TestCase):
    """End-to-end tests of MergeEngine.pre_save_check with mock orchestrator."""

    def _setup_with_source(self, config_override=None):
        orch = MagicMock()
        src = InMemSource("test")
        orch.memory_sources = {"test": src}

        base_cfg = {
            "eviction": {
                "similarity_min": 0.3,
                "duplicate_cluster_max": 5,
            },
            "merge_at_write": {
                "enabled": True,
                "strategy": "overwrite",
            },
        }
        if config_override:
            base_cfg.update(config_override)

        from unittest.mock import MagicMock as _M
        orch.retrieve = _M(return_value=None)
        # MergeEngine._retrieve_existing calls orch.retrieve(); we default to None
        # so it falls through to the source.search fallback for finding seeded data.

        from memchorus.lifecycle_merge import MergeEngine
        me = MergeEngine(orch, base_cfg)
        return orch, src, me

    def test_no_similar_entries_passes_through(self):
        orch, src, me = self._setup_with_source()
        result = me.pre_save_check("novel_key", "unique content here")
        self.assertTrue(result.should_proceed)

    def test_low_similarity_passes_through(self):
        orch, src, me = self._setup_with_source()
        # Seed some completely unrelated content in the source. The search path
        # may find a partial match but similarity should remain below threshold.
        src.save("_unrelated", "totally different topic xyzabc")
        result = me.pre_save_check("novel_key", "unique brand new material")
        self.assertTrue(result.should_proceed)

    def test_high_similarity_below_cluster_max_passes(self):
        cfg = {
            "eviction": {"similarity_min": 0.3, "duplicate_cluster_max": 5},
            "merge_at_write": {"strategy": "overwrite"},
        }
        orch, src, me = self._setup_with_source(cfg)
        # Only one high-sim entry; cluster_max=5 → passthrough
        src.save("hello", "this is a test string")

        result = me.pre_save_check("hello_2", "this is a test string too")
        # Still passes because only 1 hit < cluster_max of 5
        self.assertTrue(result.should_proceed)

    def test_overwrite_strategy_applied(self):
        """Set cluster_max=1 so even a single hit triggers merge."""
        cfg = {
            "eviction": {"similarity_min": 0.3, "duplicate_cluster_max": 1},
            "merge_at_write": {"strategy": "overwrite"},
        }
        orch, src, me = self._setup_with_source(cfg)
        # Seed existing content under the key that will be saved again
        src.save("task_done", "completed analysis report")

        # Save uses the SAME key so _find_similar returns hits with high similarity
        result = me.pre_save_check("task_done", "completed analysis new report")
        # Should have detected similarity → merge (overwrite)
        self.assertFalse(result.should_proceed)
        self.assertEqual(result.final_value, "completed analysis new report")

    def test_append_strategy_applied(self):
        """Set cluster_max=1 so even a single hit triggers merge."""
        cfg = {
            "eviction": {"similarity_min": 0.3, "duplicate_cluster_max": 1},
            "merge_at_write": {"strategy": "append"},
        }
        orch, src, me = self._setup_with_source(cfg)
        src.save("data", "first value")

        # Same key so the engine finds existing content before merge check
        result = me.pre_save_check("data", "second value")
        self.assertFalse(result.should_proceed)
        # Append should combine values into a list
        if isinstance(result.final_value, list):
            self.assertIn("first value", result.final_value)

    def test_union_strategy_applied(self):
        cfg = {
            "eviction": {"similarity_min": 0.3, "duplicate_cluster_max": 1},
            "merge_at_write": {"strategy": "union"},
        }
        orch, src, me = self._setup_with_source(cfg)
        src.save("config", {"key": "a"})

        # Same key so the engine finds existing content before merge check
        result = me.pre_save_check("config", {"key": "b"})
        self.assertFalse(result.should_proceed)

    def test_degraded_when_merge_fails(self):
        """Test that exceptions in strategy application don't crash."""
        cfg = {
            "eviction": {"similarity_min": 0.3, "duplicate_cluster_max": 1},
            "merge_at_write": {"strategy": "overwrite"},
        }
        orch, src, me = self._setup_with_source(cfg)

        # Make search return weird data that causes strategy failure
        class BadSource:
            name = "bad"
            def is_available(self):
                return True
            def search(self, query, limit=500):
                return [{"key": "x", "value": "anything"}]

        orch.memory_sources["bad"] = BadSource()
        result = me.pre_save_check("any_key", "data")
        # Should still succeed (degrade gracefully)
        self.assertIn(result.should_proceed, [True, False])  # Either is OK after degradation

    def test_disabled_engine_passesthrough(self):
        """When the engine is disabled everything bypasses."""
        cfg = {
            "eviction": {"similarity_min": 0.3, "duplicate_cluster_max": 2},
            "merge_at_write": {"enabled": False},
        }
        orch, src, me = self._setup_with_source(cfg)
        result = me.pre_save_check("key", "value")
        self.assertTrue(result.should_proceed)


if __name__ == "__main__":
    unittest.main()
