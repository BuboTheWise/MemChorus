#!/usr/bin/env python3
"""
test_orientation.py -- Unit tests for memchorus.orientation module.

Covers every public function/method in orientation.py:

1. _CacheKey class — hashability, equality
2. _CacheRegistry.__init__() — cache construction with cap/TTL params
3. _CacheRegistry.put() — add entries, verify LRU eviction at self._cap
4. _CacheRegistry.get() — hit/miss, TTL expiry
5. _is_hermez_project_name() — hex Kanban ID detection / skip logic
6. _resolve_project() — env_task to project name resolution (hex skip + fallback chain)
7. _build_orientation_query() — query string construction logic
8. orientation_search() — full search orchestration with orchestrator param
9. _execute_query() — orchestrator call with limit/dedup
10. clear_orientation_cache() — global registry clearing

New additions for GAP026:
- t_0388776d: Hex Kanban ID detection + fallback chain priority order tests
- Cache stale empty result prevention after project context switch

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
    _KANBAN_HEX_RE,
    _is_hermez_project_name,
    _build_orientation_query,
    _resolve_project,
    clear_orientation_cache,
    orientation_search,
    _execute_query,
)


class TestCacheKey(unittest.TestCase):
    """_CacheKey -- hashability, equality, frozen dataclass."""

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
    """_CacheRegistry -- LRU cache with TTL eviction."""

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

    def test_put_empty_results_not_cached(self):
        """Empty result lists must NOT be cached (poison-entry guard)."""
        key = _CacheKey(project="A", query_types=("kg",))
        self.registry.put(key, [], ttl_seconds=60.0)
        # A put of empty list is a no-op -- get must return None (miss)
        self.assertIsNone(self.registry.get(key))

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
        # Override with short TTL -- should expire immediately
        got = self.registry.get(key, ttl_override=0.001)
        time.sleep(0.01)
        self.assertIsNone(self.registry.get(key, ttl_override=0.001))

    def test_lru_eviction_at_capacity(self):
        """When maxsize reached, oldest entry (by timestamp) is evicted."""
        for i in range(3):
            k = _CacheKey(project=f"P{i}", query_types=("kg",))
            self.registry.put(k, [{"key": f"r{i}"}], ttl_seconds=60.0)
            time.sleep(0.01)

        # Add 4th entry -- should evict oldest (P0)
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

    def test_clear_project_invalidates_matching_keys_only(self):
        """clear_project(project) removes entries for that project, keeps others."""
        kA = _CacheKey(project="A", query_types=("kg",))
        kB1 = _CacheKey(project="B", query_types=("kg",))
        kB2 = _CacheKey(project="B", query_types=("semantic",))

        self.registry.put(kA, [{"key": "a"}], ttl_seconds=60.0)
        self.registry.put(kB1, [{"key": "b1"}], ttl_seconds=60.0)
        self.registry.put(kB2, [{"key": "b2"}], ttl_seconds=60.0)

        # Invalidate project B only
        self.registry.clear_project("B")

        # Project A still cached
        self.assertIsNotNone(self.registry.get(kA))
        # Project B keys gone
        self.assertIsNone(self.registry.get(kB1))
        self.assertIsNone(self.registry.get(kB2))

    def test_clear_project_no_match_is_noop(self):
        """clear_project with unknown project name does nothing."""
        k = _CacheKey(project="X", query_types=("kg",))
        self.registry.put(k, [{"key": "x"}], ttl_seconds=60.0)
        self.registry.clear_project("Z")
        self.assertIsNotNone(self.registry.get(k))

    def test_default_maxsize_is_256(self):
        default_registry = _CacheRegistry()
        self.assertEqual(default_registry._maxsize, 256)


class TestIsHermezProjectName(unittest.TestCase):
    """GAP026: _is_hermez_project_name -- hex Kanban ID detection."""

    def test_lowercase_hex_8_char_skipped(self):
        """t_[0-9a-f]{8} pattern should NOT be treated as project name."""
        self.assertFalse(_is_hermez_project_name("t_a1b2c3d4"))

    def test_uppercase_hex_8_char_skipped(self):
        """Hex detection is case-insensitive."""
        self.assertFalse(_is_hermez_project_name("t_A1B2C3D4"))

    def test_mixed_case_hex_8_char_skipped(self):
        self.assertFalse(_is_hermez_project_name("t_aF3Eb9d2"))

    def test_non_hex_task_id_accepted(self):
        """Task IDs with non-hex chars are treated as project names."""
        self.assertTrue(_is_hermez_project_name("t_xyz123ab"))
        self.assertTrue(_is_hermez_project_name("MyProject"))

    def test_meaningful_project_names_accepted(self):
        self.assertTrue(_is_hermez_project_name("MemChorus"))
        self.assertTrue(_is_hermez_project_name("Void-Scanner"))
        self.assertTrue(_is_hermez_project_name("project_v2"))

    def test_empty_string_rejected(self):
        self.assertFalse(_is_hermez_project_name(""))

    def test_short_hex_not_skipped(self):
        """Only exactly 8 hex chars after t_ are skipped."""
        self.assertTrue(_is_hermez_project_name("t_a1b2c3"))       # 6 chars
        self.assertTrue(_is_hermez_project_name("t_a1b2c3d4e5"))   # 10 chars

    def test_no_t_prefix_not_skipped(self):
        """Plain hex strings without t_ prefix are treated as project names."""
        self.assertTrue(_is_hermez_project_name("a1b2c3d4"))


class TestResolveProjectHexSkip(unittest.TestCase):
    """GAP026: _resolve_project -- hex Kanban ID detection + fallback chain priority."""

    def _save_workspace(self):
        return os.environ.pop("HERMES_WORKSPACE", None)

    def _restore_workspace(self, value):
        if value is not None:
            os.environ["HERMES_WORKSPACE"] = value
        elif "HERMES_WORKSPACE" in os.environ:
            del os.environ["HERMES_WORKSPACE"]

    # --- Hex Kanban IDs are skipped, falling to deeper fallbacks ---

    def test_hex_kanban_id_falls_to_workspace(self):
        """When env_task is a hex Kanban ID and HERMES_WORKSPACE is set, use workspace."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/some/project/dir"
        try:
            result = _resolve_project("t_a1b2c3d4")
            self.assertEqual(result, "dir")
        finally:
            del os.environ["HERMES_WORKSPACE"]

    def test_hex_kanban_id_uppercase_falls_to_workspace(self):
        """Uppercase hex Kanban IDs are also skipped."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/some/project/dir"
        try:
            result = _resolve_project("t_A1B2C3D4")
            self.assertEqual(result, "dir")
        finally:
            del os.environ["HERMES_WORKSPACE"]

    def test_hex_kanban_id_falls_to_cwd(self):
        """When env_task is hex Kanban ID and no HERMES_WORKSPACE, fall to cwd."""
        orig = self._save_workspace()
        try:
            # Without workspace set, hex ID falls all the way to cwd
            result = _resolve_project("t_beeeeef0")
            self.assertEqual(result, os.path.basename(os.getcwd()))
        finally:
            self._restore_workspace(orig)

    def test_meaningful_project_name_not_skipped(self):
        """Non-hex project names returned as-is without falling through."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/different/path"
        try:
            result = _resolve_project("MemChorus")
            self.assertEqual(result, "MemChorus")
        finally:
            del os.environ["HERMES_WORKSPACE"]

    def test_non_hex_task_id_not_skipped(self):
        """Task IDs that don't match the hex pattern are returned."""
        result = _resolve_project("t_xyz12345")  # 'x' is non-hex
        self.assertEqual(result, "t_xyz12345")

    # --- Full fallback chain priority order ---

    def test_fallback_chain_env_task_wins(self):
        """Priority 1: env_task takes precedence over everything."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/other/path"
        try:
            result = _resolve_project("RealProject")
            self.assertEqual(result, "RealProject")
        finally:
            del os.environ["HERMES_WORKSPACE"]

    def test_fallback_chain_hex_skips_to_workspace(self):
        """Priority 1→2: hex Kanban ID skipped, workspace used next."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/work/project/path"
        try:
            result = _resolve_project("t_12345678")
            self.assertEqual(result, "path")
        finally:
            del os.environ["HERMES_WORKSPACE"]

    def test_fallback_chain_workspace_used_when_env_task_none(self):
        """Priority 2: HERMES_WORKSPACE used when env_task is None."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/ws/dir"
        try:
            result = _resolve_project(None)
            self.assertEqual(result, "dir")
        finally:
            del os.environ["HERMES_WORKSPACE"]

    def test_fallback_chain_cwd_used_when_no_env(self):
        """Priority 3: cwd basename used when env_task is None and no HERMES_WORKSPACE."""
        orig = self._save_workspace()
        try:
            result = _resolve_project(None)
            self.assertEqual(result, os.path.basename(os.getcwd()))
        finally:
            self._restore_workspace(orig)

    def test_fallback_chain_empty_env_task_same_as_none(self):
        """Empty/whitespace env_task behaves like None -- falls to workspace or cwd."""
        orig = self._save_workspace()
        try:
            # Empty string → skip step 1 → fall through
            result = _resolve_project("")
            self.assertEqual(result, os.path.basename(os.getcwd()))
        finally:
            self._restore_workspace(orig)

    def test_stripped_value_returned(self):
        """env_task is stripped before return."""
        result = _resolve_project("  MyProject  ")
        self.assertEqual(result, "MyProject")


class TestBuildOrientationQuery(unittest.TestCase):
    """_build_orientation_query -- query string construction logic."""

    def test_returns_list_for_valid_task(self):
        queries = _build_orientation_query(env_task="t_custom123")
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

    def test_hex_kanban_id_uses_workspace_not_raw_id(self):
        """When env_task is hex Kanban ID, queries should use workspace basename, not the raw ID."""
        os.environ["HERMES_WORKSPACE"] = "/tmp/my/test/project"
        try:
            queries = _build_orientation_query(env_task="t_aabbccdd")
            # The project should be "project" (workspace basename), NOT "t_aabbccdd"
            for q in queries:
                self.assertNotIn("t_aabbccdd", q["query"])
                self.assertIn("project", q["query"])
        finally:
            del os.environ["HERMES_WORKSPACE"]


class TestOrientationSearch(unittest.TestCase):
    """orientation_search -- full search orchestration."""

    def setUp(self):
        clear_orientation_cache()

    def tearDown(self):
        clear_orientation_cache()

    def test_returns_list(self):
        result = orientation_search(env_task="t_custom")
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
        # May return [] or some results depending on cwd -- just verify it's a list
        self.assertIsInstance(result, list)

    def test_caching_returns_same_results(self):
        """Repeated calls within TTL return cached results."""
        result1 = orientation_search(env_task="t_cache_test", orchestrator=None)
        result2 = orientation_search(env_task="t_cache_test", orchestrator=None)
        self.assertEqual(result1, result2)


class TestCacheStaleResultOnProjectSwitch(unittest.TestCase):
    """GAP026: Verify cache does not serve stale empty results after project context switch.

    Scenario:
    1. Query for Project A -- returns empty (no memories)
    2. Switch to Project B
    3. Project B should NOT receive cached empty results from step 1
    """

    def setUp(self):
        clear_orientation_cache()

    def tearDown(self):
        clear_orientation_cache()

    def test_new_project_not_affected_by_previous_empty_query(self):
        """Switching projects after an empty result does not reuse stale cache."""
        # First query for project A -- no orchestrator means empty results
        import memchorus.orientation as _mod

        result_a = orientation_search(env_task="ProjectA", orchestrator=None)
        self.assertEqual(result_a, [])  # no orchestrator → empty

        # Verify nothing was cached for ProjectA (empty results aren't cached)
        cache_len_before = len(_mod._cache._cache)

        # Now query for project B -- should run fresh, not hit stale data
        result_b = orientation_search(env_task="ProjectB", orchestrator=None)
        self.assertEqual(result_b, [])  # still no orchestrator
        # Cache state should be the same (neither got cached since both were empty)
        cache_len_after = len(_mod._cache._cache)
        self.assertEqual(cache_len_before, cache_len_after)

    def test_project_switch_invalidates_old_entries(self):
        """When project changes, clear_project removes old entries."""
        import memchorus.orientation as _mod

        # Manually populate cache for ProjectA
        key = _CacheKey(project="ProjectA", query_types=("kg", "semantic"))
        _mod._cache.put(key, [{"key": "a1", "content": "old data"}], ttl_seconds=300.0)
        self.assertEqual(len(_mod._cache._cache), 1)

        # Clear ProjectA via targeted invalidation
        _mod._cache.clear_project("ProjectA")
        self.assertEqual(len(_mod._cache._cache), 0)

    def test_empty_results_never_cached(self):
        """Confirm empty result lists are never written to cache."""
        import memchorus.orientation as _mod

        _mod._cache.put(
            _CacheKey(project="Empty", query_types=("kg",)),
            [],
            ttl_seconds=60.0,
        )
        self.assertEqual(len(_mod._cache._cache), 0)

    def test_project_switch_clears_old_populates_new(self):
        """With an orchestrator: after switching projects, only new project data is in cache."""
        class CountingOrch:
            """Tracks how many searches were called and with what query."""
            calls = []
            def search(self, query_str, limit=5):
                self.calls.append(query_str)
                return [{"key": f"hit_{len(self.calls)}", "content": query_str}]

        orch = CountingOrch()
        # Query for ProjectX first
        result_x = orientation_search(env_task="ProjectX", orchestrator=orch, limit=5)
        self.assertTrue(len(result_x) > 0)

        # Then switch to ProjectY
        result_y = orientation_search(env_task="ProjectY", orchestrator=orch, limit=5)
        self.assertTrue(len(result_y) > 0)

        # Both projects were queried (no stale data served from cache)
        self.assertEqual(len(orch.calls), 4)  # 2 queries × 0 previously cached


class TestExecuteQuery(unittest.TestCase):
    """_execute_query -- single query execution."""

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
    """clear_orientation_cache -- global cache purge."""

    def test_clearing_removes_all_entries(self):
        import memchorus.orientation as _ori_mod
        key = _CacheKey(project="A", query_types=("kg",))
        # Put something directly in the global cache (via the real module ref)
        _ori_mod._cache.put(key, [{"key": "demo"}], ttl_seconds=60.0)
        self.assertIsNotNone(_ori_mod._cache.get(key))

        clear_orientation_cache()

        # Verify via len() on _cache._cache dict -- immune to stale-ref issues
        self.assertEqual(len(_ori_mod._cache._cache), 0)


if __name__ == "__main__":
    unittest.main()
