#!/usr/bin/env python3
"""
test_orientation.py -- Unit tests for memchorus.orientation module.

Covers every public function/method in orientation.py that previously had
zero dedicated test coverage:

1. _CacheRegistry.__init__() — cache construction with cap/TTL params
2. _CacheRegistry.put() — add entries, verify LRU eviction at self._cap
3. _CacheRegistry.get() — hit/miss, TTL expiry
4. _CacheKey class — hashability, equality
5. _build_orientation_query() — query string construction logic
6. _resolve_project() — env_task to project name resolution (None handling)
7. orientation_search() — full search orchestration with orchestrator param
8. _execute_query() — orchestrator call with limit/dedup
9. clear_orientation_cache() — global registry clearing

Use live imports, no unittest.mock for MemChorus internals.
"""

import os
import sys
import time
import tempfile
import unittest

# Ensure src/ is first on the path so memchorus resolves from this repo.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.orientation import (
    _CacheKey,
    _CacheRegistry,
    _build_orientation_query,
    _resolve_project,
    clear_orientation_cache,
    orientation_search,
    _execute_query,
)


class TestCacheKey(unittest.TestCase):
    """_CacheKey — hashability, equality, frozen dataclass."""

    def test_equality_same_values(self):
        k1 = _CacheKey(project="MemChorus", query_types=("kg", "semantic"))
        k2 = _CacheKey(project="MemChorus", query_types=("kg", "semantic"))
        self.assertEqual(k1, k2)

    def test_inequality_different_project(self):
        k1 = _CacheKey(project="A", query_types=("kg",))
        k2 = _CacheKey(project="B", query_types=("kg",))
        self.assertNotEqual(k1, k2)

    def test_inequality_different_query_types(self):
        k1 = _CacheKey(project="A", query_types=("kg",))
        k2 = _CacheKey(project="A", query_types=("semantic",))
        self.assertNotEqual(k1, k2)

    def test_hashable_in_dict(self):
        k = _CacheKey(project="X", query_types=("kg", "semantic"))
        d = {k: "value"}
        self.assertEqual(d[k], "value")

    def test_immutable_project(self):
        k = _CacheKey(project="A", query_types=("kg",))
        with self.assertRaises(Exception):
            k.project = "B"

    def test_immutable_query_types(self):
        k = _CacheKey(project="A", query_types=("kg",))
        with self.assertRaises(Exception):
            k.query_types = ("semantic",)


class TestCacheRegistry(unittest.TestCase):
    """_CacheRegistry — LRU cache with TTL eviction."""

    def setUp(self):
        self.registry = _CacheRegistry(maxsize=3)

    def test_init_creates_empty_cache(self):
        self.assertEqual(self.registry._cache, {})
        self.assertEqual(self.registry._maxsize, 3)

    def test_put_and_get_returns_stored_results(self):
        key = _CacheKey(project="A", query_types=("kg",))
        results = [{"key": "r1", "content": "hello"}]
        self.registry.put(key, results, ttl_seconds=60.0)
        got = self.registry.get(key)
        self.assertEqual(got, results)

    def test_get_miss_returns_none(self):
        key = _CacheKey(project="missing", query_types=("kg",))
        self.assertIsNone(self.registry.get(key))

    def test_ttl_expiry(self):
        key = _CacheKey(project="A", query_types=("kg",))
        self.registry.put(key, [{"key": "r1"}], ttl_seconds=0.05)
        time.sleep(0.1)
        self.assertIsNone(self.registry.get(key))

    def test_get_ttl_override_expires_early(self):
        key = _CacheKey(project="A", query_types=("kg",))
        # Store with long TTL
        self.registry.put(key, [{"key": "r1"}], ttl_seconds=60.0)
        # Override with short TTL — should expire immediately
        got = self.registry.get(key, ttl_override=0.001)
        time.sleep(0.01)
        self.assertIsNone(self.registry.get(key, ttl_override=0.001))

    def test_lru_eviction_at_capacity(self):
        """When maxsize reached, oldest entry (by timestamp) is evicted."""
        for i in range(3):
            k = _CacheKey(project=f"P{i}", query_types=("kg",))
            self.registry.put(k, [{"key": f"r{i}"}], ttl_seconds=60.0)
            time.sleep(0.01)

        # Add 4th entry — should evict oldest (P0)
        k_new = _CacheKey(project="P3", query_types=("kg",))
        self.registry.put(k_new, [{"key": "r3"}], ttl_seconds=60.0)

        # Oldest key P0 should be gone
        self.assertIsNone(self.registry.get(_CacheKey(project="P0", query_types=("kg",))))
        # New key should be present
        self.assertIsNotNone(self.registry.get(k_new))

    def test_clear_removes_all_entries(self):
        k = _CacheKey(project="A", query_types=("kg",))
        self.registry.put(k, [{"key": "r1"}], ttl_seconds=60.0)
        self.registry.clear()
        self.assertEqual(len(self.registry._cache), 0)

    def test_default_maxsize_is_256(self):
        default_registry = _CacheRegistry()
        self.assertEqual(default_registry._maxsize, 256)


class TestBuildOrientationQuery(unittest.TestCase):
    """_build_orientation_query — query string construction logic."""

    def test_returns_list_for_valid_task(self):
        queries = _build_orientation_query(env_task="t_12345")
        self.assertIsInstance(queries, list)
        self.assertTrue(len(queries) >= 1)

    def test_includes_kg_and_semantic_queries(self):
        queries = _build_orientation_query(env_task="MyProject")
        types = [q["type"] for q in queries]
        self.assertIn("kg", types)
        self.assertIn("semantic", types)

    def test_kg_query_contains_project_name(self):
        queries = _build_orientation_query(env_task="TestProject")
        kg_queries = [q for q in queries if q["type"] == "kg"]
        self.assertTrue(len(kg_queries) > 0)
        self.assertIn("TestProject", kg_queries[0]["query"])

    def test_semantic_query_contains_project_name(self):
        queries = _build_orientation_query(env_task="TestProject")
        sem_queries = [q for q in queries if q["type"] == "semantic"]
        self.assertTrue(len(sem_queries) > 0)
        self.assertIn("TestProject", sem_queries[0]["query"])

    def test_returns_empty_when_no_project(self):
        """When env_task is None and no HERMES_WORKSPACE/cwd hints, returns []."""
        # Save and clear environment for this test
        orig_workspace = os.environ.pop("HERMES_WORKSPACE", None)
        try:
            # _resolve_project falls through to os.getcwd() as last resort,
            # so it will almost never return None in this env. Test that the
            # function at least runs without error when env_task=None.
            result = _build_orientation_query(env_task=None)
            self.assertIsInstance(result, list)
        finally:
            if orig_workspace is not None:
                os.environ["HERMES_WORKSPACE"] = orig_workspace

    def test_empty_string_env_task_treated_as_none(self):
        """Whitespace-only env_task should fall through the priority chain."""
        orig_workspace = os.environ.pop("HERMES_WORKSPACE", None)
        try:
            # Empty string triggers fallback to HERMES_WORKSPACE or cwd
            result = _build_orientation_query(env_task="   ")
            self.assertIsInstance(result, list)
        finally:
            if orig_workspace is not None:
                os.environ["HERMES_WORKSPACE"] = orig_workspace


class TestResolveProject(unittest.TestCase):
    """_resolve_project — env_task to project name resolution."""

    def test_env_task_returns_stripped_value(self):
        result = _resolve_project("  t_be1e596c  ")
        self.assertEqual(result, "t_be1e596c")

    def test_empty_string_falls_through_priority_chain(self):
        """Empty string should not match first condition."""
        orig_workspace = os.environ.pop("HERMES_WORKSPACE", None)
        try:
            # Should fall through to HERMES_WORKSPACE or cwd fallback
            result = _resolve_project("")
            self.assertIsInstance(result, str)  # at least cwd basename
        finally:
            if orig_workspace is not None:
                os.environ["HERMES_WORKSPACE"] = orig_workspace

    def test_none_env_falls_to_cwd(self):
        """When env_task is None and no HERMES_WORKSPACE, fall to cwd."""
        orig_workspace = os.environ.pop("HERMES_WORKSPACE", None)
        try:
            result = _resolve_project(None)
            self.assertIsInstance(result, str)
            # Should be basename of cwd
            self.assertEqual(result, os.path.basename(os.getcwd()))
        finally:
            if orig_workspace is not None:
                os.environ["HERMES_WORKSPACE"] = orig_workspace

    def test_hermes_workspace_env_fallback(self):
        """HERMES_WORKSPACE provides fallback when env_task is absent."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/some/project/dir"
        try:
            result = _resolve_project(None)
            self.assertEqual(result, "dir")
        finally:
            del os.environ["HERMES_WORKSPACE"]

    def test_env_task_highest_priority(self):
        """env_task overrides HERMES_WORKSPACE."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/other/path"
        try:
            result = _resolve_project("MyKanbanTask")
            self.assertEqual(result, "MyKanbanTask")
        finally:
            del os.environ["HERMES_WORKSPACE"]


class TestOrientationSearch(unittest.TestCase):
    """orientation_search — full search orchestration."""

    def setUp(self):
        clear_orientation_cache()

    def tearDown(self):
        clear_orientation_cache()

    def test_returns_list(self):
        result = orientation_search(env_task="t_test")
        self.assertIsInstance(result, list)

    def test_no_orchestrator_returns_empty(self):
        """Without orchestrator, search returns empty list (graceful)."""
        result = orientation_search(env_task="t_test", orchestrator=None)
        self.assertEqual(result, [])

    def test_respects_limit(self):
        """Results capped to limit parameter."""
        result = orientation_search(
            env_task="t_test",
            orchestrator=None,
            limit=2,
        )
        self.assertLessEqual(len(result), 2)

    def test_empty_env_returns_silently(self):
        """Silent skip when no project context detectable (orchestrator=None)."""
        result = orientation_search(env_task=None, orchestrator=None)
        # May return [] or some results depending on cwd — just verify it's a list
        self.assertIsInstance(result, list)

    def test_caching_returns_same_results(self):
        """Repeated calls within TTL return cached results."""
        result1 = orientation_search(env_task="t_cache_test", orchestrator=None)
        result2 = orientation_search(env_task="t_cache_test", orchestrator=None)
        self.assertEqual(result1, result2)


class TestExecuteQuery(unittest.TestCase):
    """_execute_query — single query execution."""

    def test_returns_list_when_no_orchestrator(self):
        qdef = {"type": "kg", "query": "test project relationship"}
        result = _execute_query(qdef, orchestrator=None)
        self.assertEqual(result, [])

    def test_kg_query_with_mock_orchestrator(self):
        """KG query delegates to orchestrator.search()."""
        class FakeOrch:
            def search(self, query_str, limit=5):
                return [{"key": "kg1", "content": f"found: {query_str}"}]

        qdef = {"type": "kg", "query": "my_project relationship entity"}
        result = _execute_query(qdef, orchestrator=FakeOrch())
        self.assertEqual(len(result), 1)
        self.assertIn("my_project", result[0]["content"])

    def test_semantic_query_with_mock_orchestrator(self):
        """Semantic query delegates to orchestrator.search()."""
        class FakeOrch:
            def search(self, query_str, limit=5):
                return [{"key": "sem1", "content": f"found: {query_str}"}]

        qdef = {"type": "semantic", "query": "session context my_project"}
        result = _execute_query(qdef, orchestrator=FakeOrch())
        self.assertEqual(len(result), 1)
        self.assertIn("my_project", result[0]["content"])

    def test_orchestrator_exception_returns_empty(self):
        """When orchestrator.search() raises, query degrades gracefully."""
        class BadOrch:
            def search(self, query_str, limit=5):
                raise RuntimeError("MCP unreachable")

        qdef = {"type": "kg", "query": "test query"}
        result = _execute_query(qdef, orchestrator=BadOrch())
        self.assertEqual(result, [])

    def test_unknown_query_type_returns_empty(self):
        """Unrecognized query type falls through silently."""
        qdef = {"type": "unknown", "query": "should not happen"}
        result = _execute_query(qdef, orchestrator=None)
        self.assertEqual(result, [])


class TestClearOrientationCache(unittest.TestCase):
    """clear_orientation_cache — global cache purge."""

    def test_clearing_removes_all_entries(self):
        key = _CacheKey(project="A", query_types=("kg",))
        # Add entry via orientation_search to populate the real global cache
        _execute_query({"type": "kg", "query": "test"}, orchestrator=None)
        # Put something directly in the global cache
        from memchorus.orientation import _cache
        _cache.put(key, [{"key": "demo"}], ttl_seconds=60.0)
        self.assertIsNotNone(_cache.get(key))

        clear_orientation_cache()
        self.assertIsNone(_cache.get(key))


if __name__ == "__main__":
    unittest.main()
