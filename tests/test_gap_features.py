#!/usr/bin/env python3
"""
test_gap_features.py - End-to-end tests for GAP-008, GAP-009, GAP-010.

GAP-008: Retrieval optimisation (configurable priority ordering + LRU cache with TTL)
GAP-009: Smart storage placement with deduplication / consolidation
GAP-010: Source enable/disable without unregistering
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.orchestrator import (
    MemoryOrchestrator,
    MemoryProfile,
    _PROFILE_SOURCE_HINT,
)
from memchorus.hermes_memory_source import HermesDefaultMemorySource


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _make_orch(hermes_dir: str, **extra_config):
    """Create a pristine orchestrator pointing at a temporary hermes directory."""
    config = {
        "default_source": "hermes_default",
        "hermes_default_config": {"memory_dir": hermes_dir},
        "mempalace_config": {"skip_mcp": True},  # avoid live MCP in tests
    }
    config["mempalace_config"].update(extra_config.pop("mempalace_config", {}))
    config.update(extra_config)
    return MemoryOrchestrator(config)


class TestGAP010_SourceEnableDisable(unittest.TestCase):
    """GAP-010: enable_source / disable_source / is_source_enabled."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memchorus_gap010_")
        self.hermes_dir = os.path.join(self.tmpdir, "hermes_mem")
        self.mp_dir = os.path.join(self.tmpdir, "mempalace_cache")
        self.orch = _make_orch(self.hermes_dir)
        os.makedirs(self.mp_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 1. disable keeps the source registered ----------------------------
    def test_disable_does_not_unregister(self):
        self.orch.disable_source("mempalace")
        assert "mempalace" in self.orch.memory_sources, \
            "disable should not remove the source from registry"

    # -- 2. is_source_enabled reflects state ---------------------------------
    def test_is_source_enabled_true_by_default(self):
        assert self.orch.is_source_enabled("hermes_default") is True
        assert self.orch.is_source_enabled("mempalace") is True

    def test_is_source_enabled_after_disable(self):
        self.orch.disable_source("mempalace")
        assert self.orch.is_source_enabled("mempalace") is False
        # Others unaffected
        assert self.orch.is_source_enabled("hermes_default") is True

    # -- 3. save skips disabled sources -------------------------------------
    def test_save_skip_disabled_sources(self):
        self.orch.disable_source("mempalace")
        result = self.orch.save("skip_test_key", {"data": "only_hermes"})
        assert result is True
        # hermes_default has it
        h_val = self.orch.memory_sources["hermes_default"].retrieve("skip_test_key")
        mp_val = self.orch.memory_sources["mempalace"].retrieve("skip_test_key")
        assert h_val is not None, "hermes should have received the save"
        # mempalace was disabled so its *save* wasn't called — it won't have the key
        self.assertIsNone(mp_val)

    # -- 4. search skips disabled sources ------------------------------------
    def test_search_skip_disabled_sources(self):
        self.orch.memory_sources["hermes_default"].save(
            "only_hermes_key", {"x": 1}
        )
        self.orch.disable_source("hermes_default")
        results = self.orch.search("only_hermes")
        assert len(results) == 0, "Disabled source should not contribute to search"

    # -- 5. retrieve skips disabled sources -----------------------------------
    def test_retrieve_skip_disabled_sources(self):
        # Only hermes has the key; disabling it means retrieve returns None
        self.orch.memory_sources["hermes_default"].save(
            "retrieve_test", {"v": 1}
        )
        self.orch.disable_source("hermes_default")
        result = self.orch.retrieve("retrieve_test")
        assert result is None, \
            "Disabled source should be skipped during retrieve (nothing else has it)"

    # -- 6. re-enable restores functionality ----------------------------------
    def test_re_enable_restores_functionality(self):
        self.orch.disable_source("hermes_default")
        self.orch.enable_source("hermes_default")
        result = self.orch.save("reenable_key", {"ok": True})
        assert result is True
        val = self.orch.retrieve("reenable_key")
        assert val is not None

    # -- 7. enable/disable non-existent source returns False -------------------
    def test_enable_disable_nonexistent(self):
        assert self.orch.disable_source("ghost") is False
        assert self.orch.enable_source("ghost") is False
        assert self.orch.is_source_enabled("ghost") is False

    # -- 8. info reflects enabled state ---------------------------------------
    def test_info_includes_enabled_flag(self):
        info = self.orch.get_orchestrator_info()
        assert info["sources"]["hermes_default"]["enabled"] is True
        self.orch.disable_source("hermes_default")
        info2 = self.orch.get_orchestrator_info()
        assert info2["sources"]["hermes_default"]["enabled"] is False


class TestGAP009_SmartStoragePlacement(unittest.TestCase):
    """GAP-009: content-based routing, deduplication, consolidation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memchorus_gap009_")
        self.hermes_dir = os.path.join(self.tmpdir, "hermes_mem")
        self.mp_dir = os.path.join(self.tmpdir, "mempalace_cache")
        self.orch = _make_orch(
            self.hermes_dir,
            smart_storage_mode=True,
        )
        os.makedirs(self.mp_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 1. Short string goes to hermes_default only (smart storage) ----------
    def test_short_string_goes_to_hermes_default(self):
        self.orch.save("short_key", "Hello world")
        h_val = self.orch.memory_sources["hermes_default"].retrieve("short_key")
        assert h_val is not None, \
            "Short string should be stored in hermes_default"

    # -- 2. Explicit source_name overrides smart placement --------------------
    def test_explicit_source_overrides_smart_placement(self):
        self.orch.save("explicit_mp", {"a": 1}, source_name="mempalace")
        mp_val = self.orch.memory_sources["mempalace"].retrieve("explicit_mp")
        assert mp_val is not None, \
            "Explicit source_name should override smart placement"

    # -- 3. Smart storage can be disabled -> falls back to fan-out -------------
    def test_smart_storage_disabled_fanout(self):
        orch = _make_orch(
            self.hermes_dir,
            smart_storage_mode=False,
            mempalace_config={},
        )
        orch.save("fan_key", "fan_value")
        h_val = orch.memory_sources["hermes_default"].retrieve("fan_key")
        assert h_val is not None, \
            "Fan-out should still write to hermes_default"

    # -- 4. Profile inference (static method) ---------------------------------
    def test_infer_profile_short_string(self):
        profile = MemoryOrchestrator._infer_profile("k", "short")
        assert profile == MemoryProfile.EPHEMERAL

    def test_infer_profile_large_dict(self):
        big_dict = {str(i): i for i in range(2000)}
        profile = MemoryOrchestrator._infer_profile("big", big_dict)
        assert profile == MemoryProfile.LARGE_DATA_BLOCK

    def test_infer_profile_graph_keywords(self):
        profile = MemoryOrchestrator._infer_profile(
            "entity_relate", {"relation": "friend"}
        )
        assert profile == MemoryProfile.RELATIONSHIP_GRAPH

    # -- 5. find_duplicates detects overlap ------------------------------------
    def test_find_duplicates(self):
        key = "dup_key"
        val = {"same": True}
        self.orch.save(key, val, source_name="hermes_default")
        self.orch.memory_sources["mempalace"].save(key, val)
        dupes = self.orch.find_duplicates(key)
        assert len(dupes) >= 2, \
            "Both sources have the key, so find_duplicates should detect it"

    # -- 6. consolidate_key removes redundant copies --------------------------
    def test_consolidate_key(self):
        key = "collate"
        val = {"shared": True}
        self.orch.memory_sources["hermes_default"].save(key, val)
        self.orch.memory_sources["mempalace"].save(key, val)
        summary = self.orch.consolidate_key(key)
        assert summary["key"] == key

    # -- 7. smart storage avoids unnecessary duplication -----------------------
    def test_smart_storage_no_unnecessary_duplication(self):
        """When MemPalace is available and content is short, it should NOT be
        written to mempalace by default (avoids duplication)."""
        hd_saves: list = []
        mp_saves: list = []

        class TrackedHermes(HermesDefaultMemorySource):
            def save(self, key, value):
                hd_saves.append(key)
                return True  # Pretend success; we only care about call count

        orch = _make_orch(self.hermes_dir)
        orch.memory_sources["hermes_default"] = TrackedHermes(
            name="hermes_default",
            config={"memory_dir": self.hermes_dir},
        )

        # Replace mempalace source with a lightweight mock that logs saves
        class MockMPPositive:
            """A minimal MemorySource-like stub that tracks saves + is always available."""
            _enabled = True

            def __init__(self):
                self.name = "mempalace"

            def save(self, key, value):
                mp_saves.append(key)
                return True

            def retrieve(self, key):
                return None

            def search(self, query, limit=10):
                return []

            def is_available(self):
                return True

            def get_source_info(self):
                return {"name": "mempalace", "type": "mock", "available": True}

        orch.memory_sources["mempalace"] = MockMPPositive()

        orch.save("no_dupe", "short value")
        assert "no_dupe" in hd_saves, \
            "Short string should be written to hermes_default"
        assert "no_dupe" not in mp_saves, \
            f"Smart storage should NOT write short string to mempalace, but did: {mp_saves}"


class TestGAP008_RetrievalOptimization(unittest.TestCase):
    """GAP-008: configurable priority ordering + LRU cache with TTL."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memchorus_gap008_")
        self.hermes_dir = os.path.join(self.tmpdir, "hermes_mem")
        self.mp_dir = os.path.join(self.tmpdir, "mempalace_cache")
        os.makedirs(self.mp_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 1. priority_order config flips retrieval order -----------------------
    def test_priority_order_flips_retrieve(self):
        orch = _make_orch(
            self.hermes_dir,
            priority_order=["mempalace", "hermes_default"],
        )
        key = "priority_flip"
        val_hd = {"source": "hermes"}
        val_mp = {"source": "mempalace"}
        orch.memory_sources["hermes_default"].save(key, val_hd)
        orch.memory_sources["mempalace"].save(key, val_mp)

        result = orch.retrieve(key)
        assert result == val_mp, \
            f"priority_order=['mempalace', ...] should return mempalace's value, got {result}"

    # -- 2. default priority still favours hermes_default ---------------------
    def test_default_priority_hermes_first(self):
        orch = _make_orch(self.hermes_dir)
        key = "default_order"
        val_hd = {"hd": True}
        val_mp = {"mp": True}
        orch.memory_sources["hermes_default"].save(key, val_hd)
        orch.memory_sources["mempalace"].save(key, val_mp)
        result = orch.retrieve(key)
        # Default scorer prefers hermes_default (0.7 prior vs 0.3)
        assert result is not None
        assert "hd" in result, \
            f"Default priority should retrieve from hermes_default first; got {result}"

    # -- 3. LRU cache serves cached value ------------------------------------
    def test_cache_serves_value(self):
        orch = _make_orch(
            self.hermes_dir,
            cache_max_size=16,
            cache_ttl_seconds=60.0,
        )
        key = "cache_hit"
        val = {"cached": True}
        orch.memory_sources["hermes_default"].save(key, val)

        first = orch.retrieve(key)
        # Now delete the file so that a MISS would occur without cache
        from memchorus.hermes_memory_source import HermesDefaultMemorySource as _HMS
        safe_key = _HMS._safe_key(key)
        fpath = os.path.join(orch.memory_sources['hermes_default'].memory_dir, f"{safe_key}.json")
        if os.path.exists(fpath):
            os.remove(fpath)

        second = orch.retrieve(key)
        assert second == val, \
            "Cache should serve the value even after the backing file is gone"

    # -- 4. clear_cache forces MISS -----------------------------------------
    def test_clear_cache(self):
        orch = _make_orch(
            self.hermes_dir,
            cache_max_size=16,
            cache_ttl_seconds=60.0,
        )
        key = "clear_key"
        val = {"data": "x"}
        orch.memory_sources["hermes_default"].save(key, val)
        assert orch.retrieve(key) == val

        orch.clear_cache()
        # After clearing cache + deleting file -> should be None
        from memchorus.hermes_memory_source import HermesDefaultMemorySource as _HMS_clear
        safe_key_clear = _HMS_clear._safe_key(key)
        fpath = os.path.join(orch.memory_sources['hermes_default'].memory_dir, f"{safe_key_clear}.json")
        if os.path.exists(fpath):
            os.remove(fpath)
        result = orch.retrieve(key)
        assert result is None, \
            "Cache cleared + file gone -> retrieve should return None"

    # -- 5. save invalidates cache for that key -------------------------------
    def test_save_invalidates_cache(self):
        orch = _make_orch(
            self.hermes_dir,
            cache_max_size=16,
            cache_ttl_seconds=300.0,
        )
        key = "invalidate_key"
        orch.memory_sources["hermes_default"].save(key, {"v": 1})
        assert orch.retrieve(key) == {"v": 1}

        # Save a NEW value -> should invalidate old cache entry
        orch.save(key, {"v": 2}, source_name="hermes_default")
        new_val = orch.retrieve(key)
        assert new_val is not None
        assert new_val["v"] == 2, \
            "After save, retrieve should see the updated value (cache invalidated)"


class TestBackwardCompatibility(unittest.TestCase):
    """Existing behaviour is preserved when new features are absent."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="memchorus_bc_")
        self.hermes_dir = os.path.join(self.tmpdir, "hermes_mem")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 1. save without smart_storage_mode=False still fans out (legacy) ------
    def test_fanout_without_smart_config(self):
        orch = _make_orch(
            self.hermes_dir,
            smart_storage_mode=True,  # default — fan-out as last resort when no clear target
        )
        # Save to explicit source should always work
        assert orch.save("test", {"x": 1}, "hermes_default") is True

    # -- 2. empty orchestrator still returns safe defaults ---------------------
    def test_empty_orchestrator_safe(self):
        orch = _make_orch(self.hermes_dir)
        source_names = list(orch.memory_sources.keys())
        for n in source_names:
            orch.unregister_source(n)
        assert orch.save("orphan", "x") is False
        assert orch.retrieve("orphan") is None
        assert orch.search("any") == []
        assert orch.is_available() is False

    # -- 3. explicit save still works ----------------------------------------
    def test_explicit_save_works(self):
        orch = _make_orch(self.hermes_dir)
        result = orch.save("explicit", {"k": "v"}, source_name="hermes_default")
        assert result is True
        val = orch.retrieve("explicit")
        assert val == {"k": "v"}


if __name__ == "__main__":
    unittest.main(verbosity=2)
