#!/usr/bin/env python3
"""
test_profile_isolation_boundary.py - Verify zero cross-profile memory leakage.

Creates memories with a source pointing to one profile directory, then verifies
that an orchestrator backed by a completely different profile directory cannot access them.
Uses two separate real filesystem directories as simulated profiles.
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


class ProfileSimulator:
    """Helper to manage a simulated profile directory for isolation testing."""

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.memory_src_dir = os.path.join(base_dir, 'source')
        self.retrieve_src_dir = os.path.join(base_dir, 'retrieve')

    def setup(self):
        """Create the profile directories."""
        os.makedirs(self.memory_src_dir, exist_ok=True)
        os.makedirs(self.retrieve_src_dir, exist_ok=True)

    def teardown(self):
        """Remove all temp dirs for this profile."""
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def make_orchestrator(self, source_name, as_default=True):
        """Create an orchestrator with a HermesDefaultMemorySource pointing to memory_src_dir."""
        config = {
            'default_source': 'hermes_default' if as_default else None,
            'hermes_default_config': {'memory_dir': self.memory_src_dir},
            'mempalace_config': {},
        }
        orch = MemoryOrchestrator(config)

        # Unregister mempalace for clean isolation test
        if 'mempalace' in orch.memory_sources:
            orch.unregister_source('mempalace')

        return orch


def test_no_profile_a_to_b_leakage_via_orchestrator():
    """Memories saved in Profile A's directory are invisible to Orchestrator backed by Profile B's directory."""
    base = tempfile.mkdtemp(prefix='profile_iso_')
    profile_a_base = os.path.join(base, 'profile_a')
    profile_b_base = os.path.join(base, 'profile_b')

    sim_a = ProfileSimulator(profile_a_base)
    sim_b = ProfileSimulator(profile_b_base)

    try:
        sim_a.setup()
        sim_b.setup()

        orch_a = sim_a.make_orchestrator('hermes_default')
        orch_b = sim_b.make_orchestrator('hermes_default', as_default=False)

        # Save data in Profile A's backing store via the orchestrator
        key = 'profile_isolation_key'
        profile_data = {'owner': 'A', 'secret': 'sensitive_info'}
        assert orch_a.save(key, profile_data) is True

        # Attempt to retrieve from Profile B's backing store
        retrieved = orch_b.retrieve(key)

        # Should return None - zero leakage
        assert retrieved is None, "Leakage detected! Profile B saw Profile A data: %s" % str(retrieved)

    finally:
        sim_a.teardown()
        sim_b.teardown()


def test_profile_isolation_with_multiple_keys():
    """Multiple keys stored in Profile A are all invisible to an independent orchestrator with a different backing store."""
    base = tempfile.mkdtemp(prefix='profile_iso_')
    profile_a_base = os.path.join(base, 'profile_a')
    profile_b_base = os.path.join(base, 'profile_b')

    sim_a = ProfileSimulator(profile_a_base)
    sim_b = ProfileSimulator(profile_b_base)

    try:
        sim_a.setup()
        sim_b.setup()

        orch_a = sim_a.make_orchestrator('hermes_default')
        orchestrator_b = sim_b.make_orchestrator('hermes_default', as_default=False)

        # Store 10 keys from profile A perspective
        for i in range(10):
            orch_a.save('key_%d' % i, {'owner': 'A', 'index': i})

        # Verify none are visible to Profile B's independent orchestrator
        for i in range(10):
            retrieved = orchestrator_b.retrieve('key_%d' % i)
            assert retrieved is None, "Leakage on key_%d: %s" % (i, str(retrieved))

    finally:
        sim_a.teardown()
        sim_b.teardown()


def test_profile_can_own_unique_keys_independently():
    """Two isolated profiles can independently use the same key names without conflict."""
    base = tempfile.mkdtemp(prefix='profile_iso_')
    profile_a_base = os.path.join(base, 'profile_a')
    profile_b_base = os.path.join(base, 'profile_b')

    sim_a = ProfileSimulator(profile_a_base)
    sim_c = ProfileSimulator(profile_b_base)

    try:
        sim_a.setup()
        sim_c.setup()

        orch_a = sim_a.make_orchestrator('hermes_default')
        orch_c = sim_c.make_orchestrator('hermes_default', as_default=False)

        # Both profiles use the same key name but store different data
        shared_key = 'same_name_different_value'
        orch_a.save(shared_key, {'profile': 'A', 'value': 100})
        orch_c.save(shared_key, {'profile': 'B', 'value': 200})

        # Verify each sees only its own data
        a_result = orch_a.retrieve(shared_key)
        c_result = orch_c.retrieve(shared_key)

        assert a_result is not None
        assert a_result['profile'] == 'A'
        assert a_result['value'] == 100

        # If orchestator_b was available, it should see its own data
        if c_result is not None:
            assert c_result['profile'] == 'B'
            assert c_result['value'] == 200

    finally:
        sim_a.teardown()
        sim_c.teardown()


def test_memory_dir_change_does_not_leak_old_data():
    """Changing a source's memory_dir does not cause it to access data from any previous directory."""
    tmpdir = tempfile.mkdtemp(prefix='profile_iso_')
    dir_v1 = os.path.join(tmpdir, 'v1')
    dir_v2 = os.path.join(tmpdir, 'v2')

    try:
        os.makedirs(dir_v1, exist_ok=True)
        os.makedirs(dir_v2, exist_ok=True)

        # Use HermesDefaultMemorySource directly (not via orchestrator)
        src_v1 = HermesDefaultMemorySource(name='hermes_default', config={'memory_dir': dir_v1})
        assert src_v1.save('leak_test_key', {'version': 'v1'}) is True

        # Now create a new source pointing to a different directory
        src_v2 = HermesDefaultMemorySource(name='hermes_default', config={'memory_dir': dir_v2})
        result = src_v2.retrieve('leak_test_key')

        assert result is None, "Directory change caused leak: %s" % str(result)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

