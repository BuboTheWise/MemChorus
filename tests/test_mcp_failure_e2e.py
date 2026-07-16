#!/usr/bin/env python3
"""
test_mcp_failure_e2e.py - CRITICAL: End-to-end MemPalace MCP failure simulation.

Verifies that MemoryOrchestrator continues working when MemPalace is unavailable,
falling back to HermesDefaultMemorySource only. Uses REAL source instances, not mocks.
"""

import os
import sys
import json
import shutil
import pytest
import tempfile

# Ensure the package is importable from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.memory_source import MemorySource
from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource


def test_orchestrator_continues_when_mempalace_unavailable():
    """Ensure orchestrator continues working end-to-end when MemPalace is absent."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Verify hermes_default IS available
        assert orch.is_available()

        # Unregister mempalace to simulate complete outage scenario
        orch.unregister_source('mempalace')

        # Save a memory - should succeed via hermes_default even without mempalace
        key = 'test_key_mcp_failure'
        value = {
            'message': 'saved during mempalace outage',
            'timestamp': '2024-01-01T00:00:00',
        }
        result = orch.save(key, value)
        assert result is True

        # Retrieve should find it via hermes_default fallback
        retrieved = orch.retrieve(key)
        assert retrieved is not None
        assert retrieved['message'] == 'saved during mempalace outage'

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_degrades_gracefully_with_only_hermes_default():
    """When only one source is available (hermes_default), search returns from that source."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Unregister mempalace to simulate outage
        result = orch.unregister_source('mempalace')
        assert result is True
        assert 'mempalace' not in orch.memory_sources

        # Save by key that contains 'critical' so search can find it
        key = 'critical_test_data'
        value = {'data': 'important findings from system audit'}
        assert orch.save(key, value) is True

        results = orch.search('critical')
        assert len(results) > 0

        # Verify the result came from hermes_default
        found_from_hermes = False
        for r in results:
            if r.get('source') == 'hermes_default':
                found_from_hermes = True
                break
        assert found_from_hermes

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_full_orchestrator_lifecycle_with_one_source():
    """Test save/retrieve/search lifecycle when only hermes_default is available."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        if 'mempalace' in orch.memory_sources:
            orch.unregister_source('mempalace')

        source_names = list(orch.memory_sources.keys())
        assert len(source_names) == 1
        assert source_names[0] == 'hermes_default'

        for i in range(5):
            orch.save('data_key_%d' % i, {'iteration': i, 'status': 'saved'})

        for i in range(5):
            result = orch.retrieve('data_key_%d' % i)
            assert result is not None
            assert result['iteration'] == i

        results = orch.search('data')
        assert len(results) > 0

        info = orch.get_orchestrator_info()
        assert 'orchestrator' in info
        assert 'sources' in info

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_orchestrator_graceful_degradation_fills_source_gaps():
    """When one source is gone, the other must fill in - no None returns to callers."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_test_')
    hermes_dir = os.path.join(tmpdir, 'hermes_mem')

    try:
        config = {
            'default_source': 'hermes_default',
            'hermes_default_config': {'memory_dir': hermes_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Unregister mempalace to simulate offline MCP scenario
        orch.unregister_source('mempalace')

        key = 'gap_filler_key'
        value = {'gap': 'filled_by_hermes'}
        saved = orch.save(key, value)
        assert saved is True

        result = orch.retrieve(key)
        assert result is not None
        assert result.get('gap') == 'filled_by_hermes'

        # Search for a term that ACTUALLY appears in the stored value
        # so hermes_default can find it via its content-matching search.
        results = orch.search('filled_by_hermes')
        assert len(results) > 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

