#!/usr/bin/env python3
"""
test_gap008_cache_eviction.py - GAP008: LRU cache eviction performance and correctness.

Verifies:
1. OrderedDict is used (not plain dict) for O(1) eviction
2. Eviction runs in constant time regardless of cache size
3. Cache does not exceed _cache_max_size under load
4. Oldest/least-recently-used entries evict correctly
5. TTL expiry still works alongside LRU ordering
6. 500+ key insertions exercise the eviction path without regression
"""

import os
import sys
import time
from collections import OrderedDict
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from memchorus.orchestrator import MemoryOrchestrator


@pytest.fixture
def orch():
    source = Mock()
    source.is_available = True
    o = MemoryOrchestrator(config={
        'cache_ttl_seconds': 0.1,
        'cache_max_size': 5,
        'enforcement_on_read': False,
    })
    o.memory_sources['test'] = source
    return o


@pytest.fixture
def orch_long_ttl():
    """Orchestrator with a long TTL so expiry does not interfere."""
    source = Mock()
    source.is_available = True
    o = MemoryOrchestrator(config={
        'cache_ttl_seconds': 3600.0,
        'cache_max_size': 5,
        'enforcement_on_read': False,
    })
    o.memory_sources['test'] = source
    return o


class TestGap008Structural:
    """GAP008 structural checks - data type and implementation pattern."""

    def test_cache_is_ordered_dict(self, orch_long_ttl):
        assert isinstance(orch_long_ttl._retrieve_cache, OrderedDict)

    def test_no_min_scan_in_eviction(self):
        import inspect
        source_code = inspect.getsource(MemoryOrchestrator._evict_oldest_if_needed)
        assert 'min(' not in source_code, "Eviction still uses min() - O(n) scan remains"
        assert 'popitem' in source_code, "Should use popitem for O(1) eviction"


class TestGap008BoundedCache:
    """GAP008 acceptance criterion 2: cache does not exceed _cache_max_size."""

    def test_cache_respects_after_500_inserts(self, orch_long_ttl):
        orch_long_ttl._cache_max_size = 5
        for i in range(500):
            orch_long_ttl._retrieve_cache[f"key_{i}"] = (f"value_{i}", time.monotonic())
            orch_long_ttl._evict_oldest_if_needed()
        assert len(orch_long_ttl._retrieve_cache) <= 5

    def test_eviction_via_retrieve_path(self, orch_long_ttl):
        """Eviction fires through the normal retrieve path, not just direct calls."""
        orch_long_ttl._cache_max_size = 3
        for i in range(20):
            orch_long_ttl.source_for_key = None
            orch_long_ttl.memory_sources['test'].retrieve.side_effect = lambda k: f"v_{k}"
            orch_long_ttl.retrieve(f"k_{i}")
        assert len(orch_long_ttl._retrieve_cache) <= 3


class TestGap008EvictionOrder:
    """GAP008 acceptance criterion 4: oldest inserts evict correctly."""

    def test_oldest_insert_evicts_first(self, orch_long_ttl):
        orch_long_ttl._cache_max_size = 2
        orch_long_ttl.memory_sources['test'].retrieve.side_effect = lambda k: f"data_{k}"

        orch_long_ttl.retrieve("alpha")
        orch_long_ttl.retrieve("beta")
        orch_long_ttl.retrieve("gamma")  # should evict 'alpha'

        assert "alpha" not in orch_long_ttl._retrieve_cache
        assert "beta" in orch_long_ttl._retrieve_cache
        assert "gamma" in orch_long_ttl._retrieve_cache

    def test_recent_access_keeps_entry(self, orch_long_ttl):
        """Accessing an item moves it to the end so it survives eviction."""
        orch_long_ttl._cache_max_size = 2
        orch_long_ttl.memory_sources['test'].retrieve.side_effect = lambda k: f"data_{k}"

        orch_long_ttl.retrieve("x")
        orch_long_ttl.retrieve("y")
        time.sleep(0.01)
        orch_long_ttl.retrieve("x")  # re-access x - should become most-recently-used
        orch_long_ttl.retrieve("z")  # should evict y (now oldest), not x

        assert "y" not in orch_long_ttl._retrieve_cache, "y should have been evicted"
        assert "x" in orch_long_ttl._retrieve_cache, "x was accessed recently - should survive"
        assert "z" in orch_long_ttl._retrieve_cache


class TestGap008TTLInteraction:
    """GAP008 acceptance criterion 3: TTL expiry still works with OrderedDict."""

    def test_expired_entry_removed_on_access(self, orch):
        orch.memory_sources['test'].retrieve.side_effect = lambda k: f"data_{k}"
        orch.retrieve("temp_key")
        time.sleep(0.15)  # exceed 0.1s TTL
        orch.memory_sources['test'].reset_mock()
        result = orch.retrieve("temp_key")
        orch.memory_sources['test'].retrieve.assert_called()


class TestGap008ClearCache:
    """Existing clear_cache API continues to work."""

    def test_clear_empty(self, orch_long_ttl):
        orch_long_ttl.memory_sources['test'].retrieve.side_effect = lambda k: "data"
        orch_long_ttl.retrieve("a")
        assert len(orch_long_ttl._retrieve_cache) > 0
        orch_long_ttl.clear_cache()
        assert len(orch_long_ttl._retrieve_cache) == 0
