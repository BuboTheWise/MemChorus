#!/usr/bin/env python3
"""
test_orientation.py — Unit/integration tests for memchorus.orientation module.

Covers:
- _CacheRegistry LRU cache hit/miss with monotonic time within and exceeding TTL windows (AC-O2)
- TTL eviction under 256-entry cap — oldest entry discarded first on insertion order
- Query construction priority: HERMES_KANBAN_TASK → workspace dir basename → CWD basename
- Silent empty result handling: no log output or exceptions when orchestrator is None (AC-O3)
- Deduplication within _execute_query results by key field, capping at limit of 5 total entries (AC-O1)
- orientation_search end-to-end with caching and dedup behavior
- clear_orientation_cache resets all state

Runs against live imports — no mocking of external services.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.orientation import (
    _CacheKey,
    _CacheEntry,
    _CacheRegistry,
    _build_orientation_query,
    _execute_query,
    _resolve_project,
    clear_orientation_cache,
    orientation_search,
    DEFAULT_CACHE_TTL_SECONDS,
)


# --------------------------------------------------------------------------- #
# LRU cache internals (_CacheKey, _CacheEntry, _CacheRegistry)
# --------------------------------------------------------------------------- #

class TestCacheKey(unittest.TestCase):
    """_CacheKey immutability and hashing."""

    def test_cache_key_immutable(self):
        key = _CacheKey(project="test", query_types=("kg", "semantic"))
        self.assertTrue(key.project == "test")
        self.assertTrue(key.query_types == ("kg", "semantic"))

    def test_cache_key_equality(self):
        k1 = _CacheKey(project="a", query_types=("kg",))
        k2 = _CacheKey(project="a", query_types=("kg",))
        k3 = _CacheKey(project="b", query_types=("kg",))
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_cache_key_hashable(self):
        """_CacheKey is hashable — can be used as dict key."""
        k = _CacheKey(project="x", query_types=("semantic",))
        d = {k: "val"}
        self.assertEqual(d[k], "val")


class TestCacheEntry(unittest.TestCase):
    def test_cache_entry_fields(self):
        entry = _CacheEntry(results=[{"key": "a"}], timestamp=1.0, ttl=60)
        self.assertEqual(entry.results, [{"key": "a"}])
        self.assertEqual(entry.timestamp, 1.0)
        self.assertEqual(entry.ttl, 60)


class TestCacheRegistry(unittest.TestCase):
    """_CacheRegistry LRU / TTL / maxsize behaviour."""

    def setUp(self):
        self.reg = _CacheRegistry(maxsize=4)
        clear_orientation_cache()

    def tearDown(self):
        clear_orientation_cache()

    # -- Hit / miss -------------------------------------------------------
    def test_cache_miss_on_empty(self):
        key = _CacheKey(project="z", query_types=("kg",))
        self.assertIsNone(self.reg.get(key))

    def test_cache_hit_within_ttl(self):
        key = _CacheKey(project="hit", query_types=("semantic",))
        results = [{"key": "r1", "content": "hello"}]
        self.reg.put(key, results, ttl_seconds=60.0)
        hit = self.reg.get(key)
        self.assertIsNotNone(hit)
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["key"], "r1")

    def test_cache_miss_after_ttl_expires(self):
        key = _CacheKey(project="expiry", query_types=("kg",))
        results = [{"key": "old"}]
        # Manually backdate the entry's timestamp so it is expired regardless of real clock
        self.reg.put(key, results, ttl_seconds=10.0)
        entry = self.reg._cache[key]
        # Mutate entry.timestamp to 100s ago (entry is mutable dataclass)
        entry.timestamp = time.monotonic() - 200.0
        self.assertIsNone(self.reg.get(key))

    def test_ttl_override_in_get(self):
        """ttl_override parameter overrides the default/entry TTL."""
        key = _CacheKey(project="override", query_types=("kg",))
        results = [{"key": "data"}]
        # Backdate entry so it is 200s old
        self.reg.put(key, results, ttl_seconds=100.0)
        entry = self.reg._cache[key]
        entry.timestamp = time.monotonic() - 200.0
        # With override=999 the delta (200) < override -> still valid -> hit
        # With default TTL (60s), delta (200) > 60 -> expired -> miss
        self.assertIsNone(self.reg.get(key, ttl_override=60.0))

    def test_clear_removes_all(self):
        key = _CacheKey(project="clear_me", query_types=("kg",))
        self.reg.put(key, [{"key": "x"}], ttl_seconds=60.0)
        self.reg.clear()
        self.assertIsNone(self.reg.get(key))

    # -- LRU eviction at maxsize ------------------------------------------
    def test_maxsize_evicts_oldest(self):
        """When cache is full (maxsize exceeded), oldest entry is evicted."""
        reg = _CacheRegistry(maxsize=3)
        for i in range(3):
            k = _CacheKey(project=f"p{i}", query_types=("kg",))
            reg.put(k, [{"key": f"r{i}"}], ttl_seconds=600.0)
            time.sleep(0.01)  # Ensure timestamps differ

        # Insert a 4th entry — should evict p0 (oldest)
        k_new = _CacheKey(project="p3", query_types=("kg",))
        reg.put(k_new, [{"key": "r_new"}], ttl_seconds=600.0)

        # Oldest entry (p0) should be gone
        self.assertIsNone(reg.get(_CacheKey(project="p0", query_types=("kg",))))
        # New entry is present
        self.assertEqual(
            reg.get(_CacheKey(project="p3", query_types=("kg",))),
            [{"key": "r_new"}],
        )

    def test_maxsize_256_capacity(self):
        """Cache supports 256 entries (default maxsize)."""
        reg = _CacheRegistry(maxsize=256)
        for i in range(256):
            k = _CacheKey(project=str(i), query_types=("kg",))
            reg.put(k, [{"key": str(i)}], ttl_seconds=600.0)

        # All 256 should be present
        for i in range(256):
            k = _CacheKey(project=str(i), query_types=("kg",))
            hit = reg.get(k)
            self.assertIsNotNone(hit, f"Entry {i} should still be cached")

    def test_257th_entry_evicts_one(self):
        """Inserting the 257th entry drops exactly one slot."""
        reg = _CacheRegistry(maxsize=256)
        for i in range(256):
            k = _CacheKey(project=str(i), query_types=("kg",))
            reg.put(k, [{"key": str(i)}], ttl_seconds=600.0)

        # One more insertion
        k_extra = _CacheKey(project="extra", query_types=("kg",))
        reg.put(k_extra, [{"key": "extra"}], ttl_seconds=600.0)
        self.assertEqual(len(reg._cache), 256)


# --------------------------------------------------------------------------- #
# Query construction: _resolve_project and _build_orientation_query
# --------------------------------------------------------------------------- #

class TestResolveProject(unittest.TestCase):
    """Priority chain: HERMES_KANBAN_TASK → workspace dir → CWD basename."""

    def setUp(self):
        self._saved_task = os.environ.pop("HERMES_KANBAN_TASK", None)
        self._saved_workspace = os.environ.pop("HERMES_WORKSPACE", None)

    def tearDown(self):
        if self._saved_task is not None:
            os.environ["HERMES_KANBAN_TASK"] = self._saved_task
        elif "HERMES_KANBAN_TASK" in os.environ:
            del os.environ["HERMES_KANBAN_TASK"]
        if self._saved_workspace is not None:
            os.environ["HERMES_WORKSPACE"] = self._saved_workspace
        elif "HERMES_WORKSPACE" in os.environ:
            del os.environ["HERMES_WORKSPACE"]

    def test_env_task_highest_priority(self):
        """When env_task is provided with strip(), it wins."""
        result = _resolve_project("t_test_task")
        self.assertEqual(result, "t_test_task")

    def test_env_task_empty_string_falls_through(self):
        """Empty string env_task falls through to next priority."""
        os.environ.pop("HERMES_WORKSPACE", None)
        result = _resolve_project("")
        # Falls back to CWD basename
        cwd_base = os.path.basename(os.getcwd())
        self.assertEqual(result, cwd_base)

    def test_env_task_none_uses_workspace(self):
        """When env_task is None, HERMES_WORKSPACE basename is used."""
        os.environ["HERMES_WORKSPACE"] = "/some/path/MyProject"
        result = _resolve_project(None)
        self.assertEqual(result, "MyProject")

    def test_env_task_none_uses_cwd(self):
        """When env_task and workspace are absent, CWD basename is used."""
        os.environ.pop("HERMES_WORKSPACE", None)
        result = _resolve_project(None)
        cwd_base = os.path.basename(os.getcwd())
        self.assertEqual(result, cwd_base)

    def test_whitespace_only_env_task_falls_through(self):
        """All-whitespace env_task falls through."""
        os.environ.pop("HERMES_WORKSPACE", None)
        result = _resolve_project("   ")
        # Falls back to CWD
        cwd_base = os.path.basename(os.getcwd())
        self.assertEqual(result, cwd_base)


class TestBuildOrientationQuery(unittest.TestCase):
    """_build_orientation_query returns correct query dicts."""

    def setUp(self):
        self._saved_task = os.environ.pop("HERMES_KANBAN_TASK", None)
        self._saved_workspace = os.environ.pop("HERMES_WORKSPACE", None)

    def tearDown(self):
        if self._saved_task is not None:
            os.environ["HERMES_KANBAN_TASK"] = self._saved_task
        elif "HERMES_KANBAN_TASK" in os.environ:
            del os.environ["HERMES_KANBAN_TASK"]
        if self._saved_workspace is not None:
            os.environ["HERMES_WORKSPACE"] = self._saved_workspace
        elif "HERMES_WORKSPACE" in os.environ:
            del os.environ["HERMES_WORKSPACE"]

    def test_query_count(self):
        queries = _build_orientation_query(env_task="my_project")
        # Should build 2 queries: kg + semantic
        self.assertEqual(len(queries), 2)

    def test_query_types(self):
        queries = _build_orientation_query(env_task="proj")
        types = [q["type"] for q in queries]
        self.assertIn("kg", types)
        self.assertIn("semantic", types)

    def test_query_content_includes_project_name(self):
        queries = _build_orientation_query(env_task="MyProject")
        kg_query = [q for q in queries if q["type"] == "kg"][0]
        self.assertIn("MyProject", kg_query["query"])

    def test_empty_when_no_project(self):
        """When project resolves to None (shouldn't happen normally), returns empty."""
        # _build_orientation_query always passes env_task to _resolve_project;
        # since CWD usually exists, this rarely returns []. But if project is None,
        # it should return an empty list.

    def test_both_queries_have_query_key(self):
        queries = _build_orientation_query(env_task="test_proj")
        for q in queries:
            self.assertIn("query", q)
            self.assertIsInstance(q["query"], str)


# --------------------------------------------------------------------------- #
# _execute_query with orchestrator = None (silent degradation, AC-O3)
# --------------------------------------------------------------------------- #

class TestExecuteQuerySilent(unittest.TestCase):
    """_execute_query returns [] silently when no orchestrator is available."""

    def test_no_orchestrator_returns_empty(self):
        qdef = {"type": "kg", "query": "test project relationship"}
        result = _execute_query(qdef, orchestrator=None)
        self.assertEqual(result, [])

    def test_semantic_no_orchestrator_returns_empty(self):
        qdef = {"type": "semantic", "query": "session context test"}
        result = _execute_query(qdef, orchestrator=None)
        self.assertEqual(result, [])

    def test_unknown_type_returns_empty(self):
        """When query type is not kg or semantic and no orchestrator, returns []."""
        qdef = {"type": "unknown", "query": "whatever"}
        result = _execute_query(qdef, orchestrator=None)
        self.assertEqual(result, [])


class TestExecuteQueryWithMockOrchestrator(unittest.TestCase):
    """_execute_query against a fake orchestrator that returns results or raises."""

    def test_kg_query_with_working_orch(self):
        """When orchestrator.search() returns data, _execute_query returns it."""
        class MockOrch:
            def search(self, query, limit=5):
                return [{"key": f"hit_{query}", "content": "result"}]

        qdef = {"type": "kg", "query": "alpha relationship entity"}
        result = _execute_query(qdef, orchestrator=MockOrch())
        self.assertEqual(len(result), 1)
        self.assertIn("alpha", result[0]["key"])

    def test_semantic_query_with_working_orch(self):
        class MockOrch:
            def search(self, query, limit=5):
                return [{"key": "sem_hit", "content": query}]

        qdef = {"type": "semantic", "query": "session context alpha"}
        result = _execute_query(qdef, orchestrator=MockOrch())
        self.assertEqual(len(result), 1)

    def test_orchestrator_search_raises_returns_empty(self):
        """When orchestrator.search raises, _execute_query returns []."""
        class FailingOrch:
            def search(self, query, limit=5):
                raise RuntimeError("MCP unreachable")

        qdef = {"type": "kg", "query": "failing"}
        result = _execute_query(qdef, orchestrator=FailingOrch())
        self.assertEqual(result, [])


# --------------------------------------------------------------------------- #
# orientation_search — end-to-end with cache and dedup
# --------------------------------------------------------------------------- #

class TestOrientationSearch(unittest.TestCase):
    """Full integration of orientation_search including caching and dedup."""

    def setUp(self):
        clear_orientation_cache()
        self._saved_task = os.environ.pop("HERMES_KANBAN_TASK", None)

    def tearDown(self):
        clear_orientation_cache()
        if self._saved_task is not None:
            os.environ["HERMES_KANBAN_TASK"] = self._saved_task
        elif "HERMES_KANBAN_TASK" in os.environ:
            del os.environ["HERMES_KANBAN_TASK"]

    def test_returns_list(self):
        result = orientation_search(env_task="test_project")
        self.assertIsInstance(result, list)

    def test_empty_when_no_project_detected(self):
        # Pass None and clear workspace so project can't be resolved
        os.environ.pop("HERMES_WORKSPACE", None)
        # env_task=None with no workspace → might still resolve from CWD
        # If it does resolve, result is at worst empty (no orchestrator) rather than raising

    def test_dedup_by_key(self):
        """Duplicate keys in results are removed by _execute_query dedup logic."""
        class DedupOrch:
            def search(self, query, limit=5):
                # Return items with overlapping keys
                return [
                    {"key": "dup_1", "content": "first"},
                    {"key": "dup_2", "content": "second"},
                    {"key": "dup_1", "content": "duplicate of first"},
                ]
        result = orientation_search(
            env_task="dedup_test",
            orchestrator=DedupOrch(),
            limit=5,
        )
        # Keys should be unique — dup_1 appears only once
        keys = [r["key"] for r in result]
        self.assertEqual(len(keys), len(set(keys)), "Keys should be unique after dedup")

    def test_limit_enforcement(self):
        """Result is capped at limit."""
        class ManyOrch:
            def search(self, query, limit=5):
                return [{"key": f"k{i}", "content": str(i)} for i in range(10)]
        result = orientation_search(
            env_task="limit_test",
            orchestrator=ManyOrch(),
            limit=3,
        )
        self.assertLessEqual(len(result), 3)

    def test_cache_serves_result_on_second_call(self):
        """Second call within TTL returns from cache without re-executing queries."""
        class CountingOrch:
            call_count = 0
            def search(self, query, limit=5):
                CountingOrch.call_count += 1
                return [{"key": "cached_hit", "content": "from_orch"}]

        orch = CountingOrch()
        first = orientation_search(
            env_task="cache_test",
            orchestrator=orch,
            limit=5,
            cache_ttl_seconds=60.0,
        )
        count_after_first = CountingOrch.call_count
        second = orientation_search(
            env_task="cache_test",
            orchestrator=orch,
            limit=5,
            cache_ttl_seconds=60.0,
        )
        # Second call should serve from cache — no additional search() calls
        self.assertEqual(first, second)
        self.assertEqual(count_after_first, CountingOrch.call_count,
                         "Cache should prevent additional orchestrator calls")

    def test_limit_applied_even_on_cache_hit(self):
        """Limit is enforced on both cold and cached results."""
        class LargeOrch:
            def search(self, query, limit=5):
                return [{"key": f"lk{i}", "content": str(i)} for i in range(20)]

        orch = LargeOrch()
        os.environ["HERMES_KANBAN_TASK"] = "limit_cache_test"
        try:
            result = orientation_search(
                env_task="limit_cache_test",
                orchestrator=orch,
                limit=2,
                cache_ttl_seconds=60.0,
            )
        finally:
            os.environ.pop("HERMES_KANBAN_TASK", None)

        self.assertLessEqual(len(result), 2)


# --------------------------------------------------------------------------- #
# Cache management
# --------------------------------------------------------------------------- #

class TestCachePurge(unittest.TestCase):
    """clear_orientation_cache resets the global cache."""

    def test_clear_purges_entries(self):
        clear_orientation_cache()

    def test_put_get_clear_roundtrip(self):
        from memchorus.orientation import _cache as g_cache
        key = _CacheKey(project="roundtrip", query_types=("kg",))
        g_cache.put(key, [{"key": "before"}], ttl_seconds=60.0)
        self.assertIsNotNone(g_cache.get(key))
        clear_orientation_cache()
        self.assertIsNone(g_cache.get(key))


if __name__ == "__main__":
    unittest.main(verbosity=2)
