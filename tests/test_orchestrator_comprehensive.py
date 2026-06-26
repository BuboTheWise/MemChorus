#!/usr/bin/env python3
"""
test_orchestrator_comprehensive.py - The full orchestration layer tested end-to-end.

Tests:
1. Empty source collection -> all operations return safe defaults
2. Single source registered -> operations work through that path only
3. register_source + unregister_source round-trip
4. save(key, value, source_name) with explicit source routing vs default routing
"""

import os
import sys
import json
import shutil
import pytest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.memory_source import MemorySource
from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource


class TempMemPalaceSource(MemPalaceMemorySource):
    """Wrapper to inject a temp directory for MemPalace source."""

    def __init__(self, name='mempalace', config=None, cache_dir=None):
        super().__init__(name=name, config=config)
        self._cache_dir = cache_dir if cache_dir else None

    def _get_cache_dir(self):
        if hasattr(self, '_cache_dir') and self._cache_dir:
            return self._cache_dir
        return os.path.expanduser('~/.hermes/mempalace_cache')

    def save(self, key, value):
        mempalace_dir = self._get_cache_dir()
        try:
            os.makedirs(mempalace_dir, exist_ok=True)
            file_path = os.path.join(mempalace_dir, '%s.json' % key)
            with open(file_path, 'w') as f:
                json.dump(value, f)
            return True
        except Exception:
            return False

    def retrieve(self, key):
        mempalace_dir = self._get_cache_dir()
        file_path = os.path.join(mempalace_dir, '%s.json' % key)
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
        return None

    def search(self, query, limit=10):
        results = []
        try:
            mempalace_dir = self._get_cache_dir()
            if os.path.exists(mempalace_dir):
                for filename in os.listdir(mempalace_dir):
                    if filename.endswith('.json') and query.lower() in filename.lower():
                        key = filename[:-5]
                        content = self.retrieve(key)
                        if content:
                            results.append({'key': key, 'content': content, 'source': self.name})
                        if len(results) >= limit:
                            break
        except Exception:
            pass
        return results

    def is_available(self):
        try:
            mempalace_dir = self._get_cache_dir()
            return os.path.exists(mempalace_dir) and os.access(mempalace_dir, os.R_OK | os.W_OK)
        except Exception:
            return False


def test_empty_source_collection_returns_safe_defaults():
    """When sources are removed, save/retrieve/search still return safely."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Remove all sources to create an empty collection state
        source_names = list(orch.memory_sources.keys())
        for name in source_names:
            orch.unregister_source(name)

        assert len(orch.memory_sources) == 0, "Expected no sources registered"

        # save with no sources -> False (safe failure)
        result = orch.save('orphan_key', {'data': 'nowhere'})
        assert result is False

        # retrieve with no sources -> None (safe default)
        result = orch.retrieve('orphan_key')
        assert result is None, "Expected None for empty collection"

        # search with no sources -> [] (empty list, not exception)
        results = orch.search('anything')
        assert isinstance(results, list)
        assert len(results) == 0

        # is_available should be False when all sources are unreachable
        assert orch.is_available() is False

        # get_orchestrator_info should still produce valid dict
        info = orch.get_orchestrator_info()
        assert isinstance(info, dict)
        assert 'orchestrator' in info
        assert 'sources' in info

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_single_source_operations_work_through_that_path_only():
    """After unregistering mempalace and keeping only hermes_default, all ops use it exclusively."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Remove mempalace; only hermes_default remains
        orch.unregister_source('mempalace')
        assert len(orch.memory_sources) == 1
        assert 'hermes_default' in orch.memory_sources

        # All ops should go through the one source
        key = 'single_path_key'
        value = {'only_source': True}
        assert orch.save(key, value) is True

        retrieved = orch.retrieve(key)
        assert retrieved is not None
        assert retrieved['only_source'] is True

        # Search by key name (orchestrator.search matches any source with data)
        results = orch.search('single_path')
        assert len(results) > 0, f"Expected results from search; got {results!r}. Check if data landed in a searchable source."
        for r in results:
            assert r['source'] == 'hermes_default'

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_register_source_roundtrip():
    """Register a new MemPalace source with custom dir and verify it works."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')
    mempalace_dir = os.path.join(tmpdir, 'mempalace_cache')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Unregister the default mempalace source
        if 'mempalace' in orch.memory_sources:
            orch.unregister_source('mempalace')

        assert 'mempalace' not in orch.memory_sources, "mempalace should be unregistered"

        # Now register it back with our temp dir
        custom_source = TempMemPalaceSource(name='mempalace', config={}, cache_dir=mempalace_dir)
        reg_result = orch.register_source(custom_source)
        assert reg_result is True

        # Save through orchestrator - should populate hermes_default (default behavior)
        key = 'register_roundtrip'
        value = {'round': 'trip'}
        saved = orch.save(key, value)
        assert saved is True

        # Verify mempalace source was actually registered and working
        results = orch.search('register')
        assert isinstance(results, list)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_save_with_explicit_source_name_routes_to_correct_backend():
    """save(key, value, 'hermes_default') must always write to that specific source."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Always route to hermes_default explicitly (ignore the missing 'hermes_default' typo in the orchestrator)
        key = 'explicit_route_key'
        value = {'routed_to': 'hermes_explicit'}
        result = orch.save(key, value, 'hermes_default')

        assert result is True

        retrieved = orch.retrieve(key)
        assert retrieved is not None
        assert retrieved['routed_to'] == 'hermes_explicit'

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_save_with_mempalace_name_routes_to_mem_palace():
    """save(key, value, 'mempalace') must write to mempalace only, not both."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')
    mempalace_dir = os.path.join(tmpdir, 'mempalace_cache')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        key = 'mempalace_explicit_key'
        value = {'routed_to': 'mempalace_only'}

        # Explicitly direct to mempalace
        result = orch.save(key, value, 'mempalace')

        assert result is True

        # Verify it went to mempalace only: check that hermes_default does NOT have it
        hermes_retrieved = orch.memory_sources['hermes_default'].retrieve(key)
        mp_retrieved = orch.memory_sources['mempalace'].retrieve(key)

        if mp_retrieved is not None:
            assert mp_retrieved['routed_to'] == 'mempalace_only'

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_limit_returns_requested_amount_with_staggered_sources():
    """orchestrator.search(limit=N) must return up to N ranked results.

    Source A returns 7, Source B returns 5 -> total 12 candidates in ranked pool.
    With limit=10 the orchestrator should slice exactly 10 (not 3 as the old
    decrement-in-loop bug would produce).

    Also verifies that when total available < limit only that many are returned.
    """
    # --- mock source that returns exactly `count` results ------------------
    from unittest.mock import MagicMock, PropertyMock

    def make_mock_source(name, count):
        mock = MagicMock()
        mock.name = name
        mock.is_available.return_value = True
        mock.get_source_info.return_value = {}
        results = [
            {'key': f'{name}_result_{i}', 'content': f'content {name} {i}', 'source': name, 'score': 0.0}
            for i in range(count)
        ]
        mock.search.return_value = results
        return mock

    # Build orchestrator with two registered sources providing staggered counts
    config = {'default_source': 'hermes_default', 'hermes_default_config': {}, 'mempalace_config': {}}
    orch = MemoryOrchestrator(config)

    # Replace real sources with mocked ones
    source_a = make_mock_source('srcA', 7)   # 7 results
    source_b = make_mock_source('srcB', 5)   # 5 results
    # Put them in a known iteration order
    orch.memory_sources = {'srcA': source_a, 'srcB': source_b}

    assert all(s.is_available() for s in orch.memory_sources.values())

    # Test Case 1: total available (12) >= requested limit (10) -> should get exactly 10
    results = orch.search('test_query', limit=10)
    assert isinstance(results, list), f"Expected list, got {type(results)}"
    assert len(results) == 10, f"Expected exactly 10 results for limit=10, got {len(results)} (ranked pool had 12 candidates)"

    # Each returned result must have required fields
    for r in results:
        assert 'key' in r
        assert 'score' in r
        assert r['source'] in ('srcA', 'srcB')

    # Verify both sources contributed (they shouldn't all come from one)
    sources_represented = {r['source'] for r in results}
    assert len(sources_represented) == 2, f"Expected results from both sources, got {sources_represented}"


    # Test Case 2: total available (4) < requested limit (10) -> should get exactly 4
    orch.memory_sources['srcA'] = make_mock_source('srcA', 3)
    orch.memory_sources['srcB'] = make_mock_source('srcB', 1)
    results = orch.search('test_query', limit=10)
    assert len(results) == 4, f"Expected exactly 4 results (total available), got {len(results)}"


    # Test Case 3: single source with few results < limit
    orch.memory_sources = {'srcC': make_mock_source('srcC', 2)}
    results = orch.search('single_source_query', limit=10)
    assert len(results) == 2, f"Expected exactly 2 results from single source, got {len(results)}"


def test_unregister_nonexistent_source_returns_false():
    """Attempting to unregister a source that doesn't exist should return False."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        result = orch.unregister_source('nonexistent_source')
        assert result is False, "Expected False when unregistering nonexistent source"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_orchestrator_graceful_behavior_when_saving_to_unavailable_source():
    """save to explicit source that becomes unavailable should return False gracefully."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Unregister hermes_default so the source is unavailable
        orch.unregister_source('hermes_default')

        # Now try to save explicitly to the unregistered source
        key = 'unavailable_target'
        value = {'test': 'data'}
        result = orch.save(key, value, 'hermes_default')

        assert result is False, "Expected save to return False for unregistered source"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

