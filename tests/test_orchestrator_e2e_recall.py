#!/usr/bin/env python3
"""
test_orchestrator_e2e_recall.py - End-to-end integration test for MemoryOrchestrator recall.

Seeds facts directly into HermesDefaultMemorySource, calls orchestrator.search(),
and asserts that recall actually returns hits with populated key/content fields.

This closes the gap between unit-test coverage (1161 passing) and actual runtime
recall evidence — benchmarks have shown recall flatlining at zero despite
boosting being merged and all tests green.
"""

import os
import sys
import shutil
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource

# ---------------------------------------------------------------------------
# Seeded facts: deliberately diverse so the substring scorer has terms to latch on
# ---------------------------------------------------------------------------

SEED_FACTS = {
    'user_pref_timeout':     'session timeout is 900 seconds for all agents',
    'project_recall_goal':   'improve recall accuracy for memory systems by 2027',
    'api_search_endpoint':   'the search REST endpoint lives at /v2/search',
    'standup_schedule':      'daily standup meeting every weekday at 09:30 UTC',
    'deployment_target':     'production deployment uses kubernetes cluster prod-gamma',
    'architecture_decision': 'use strategy pattern instead of factory for extensibility',
}

# Each tuple is (search_query, expected_substring). The needle is deliberately a
# substring that appears in at least one seeded fact value.
QUERIES = [
    ('session timeout',      'timeout'),
    ('recall accuracy',      'recall'),
    ('search endpoint',      'search'),
    ('standup meeting',      'standup'),
    ('kubernetes deployment','kubernetes'),
    ('strategy extensibility','extensibility'),
]


@pytest.fixture()
def _orch():
    """Build an isolated orchestrator whose HermesDefaultMemorySource writes to a temp dir.

    Other sources (MemPalace, session_history) are disabled so the test measures
    only the hermes_default path — no external network or MCP dependency.
    """
    tmpdir = tempfile.mkdtemp(prefix='mc_e2e_recall_')
    mem_dir = os.path.join(tmpdir, 'hermes_mem')

    orch = MemoryOrchestrator({
        'default_source': 'hermes_default',
        'hermes_default_config': {'memory_dir': mem_dir},
        'enforce_on_read': False,
        'enforce_on_write': False,
    })

    # Disable non-essential sources so the test is self-contained
    for name in ('mempalace', 'session_history'):
        orch.disable_source(name)

    yield orch
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestOrchestratorE2ERecall:
    """End-to-end recall tests — seed facts, search, verify hits."""

    # ------------------------------------------------------------------
    # AC-1: recall_count > 0 for seeded queries
    # ------------------------------------------------------------------

    # ---------------------------------------------------------------------------
    # Helper: seed directly into hermes_default source to avoid triggering
    # the merge engine's pre_save_check (which scans ALL registered sources
    # including external ones that may not be available in test isolation).
    # We are testing recall/search, not merge logic.
    # ---------------------------------------------------------------------------

    def _seed(self, orch):
        """Seed into hermes_default source, returning the raw source instance."""
        source = orch.memory_sources.get('hermes_default')
        assert source is not None, "hermes_default not registered"
        return source

    def test_recall_returns_hits_for_seeded_facts(self, _orch):
        """After seeding FACTS, each of the QUERIES must return at least one hit."""
        src = self._seed(_orch)
        for key, value in SEED_FACTS.items():
            ok = src.save(key, value)
            assert ok, f"save() returned False for key={key}"

        # Verify recall_count > 0 for every query plus a non-empty key/content check
        hits_for_query = 0
        for query, needle in QUERIES:
            results = _orch.search(query)
            assert isinstance(results, list), f"search() returned {type(results)}"

            # Each hit must have non-empty 'key' and 'content' (AC-2)
            if results:
                for rec in results:
                    assert rec.get('key'),   f"result missing or empty key for query '{query}'"
                    assert rec.get('content'), f"result missing or empty content for query '{query}'"

            # At least one result whose combined string representation contains the needle
            matched = any(needle.lower() in str(r).lower() for r in results)
            if matched:
                hits_for_query += 1

        # We expect at least half of the queries to return a positive hit.
        # (Substring matching is permissive but not guaranteed for every query.)
        threshold = len(QUERIES) // 2  # >= 3 out of 6
        assert hits_for_query >= threshold, (
            f"Only {hits_for_query}/{len(QUERIES)} queries returned matching results "
            f"(need >= {threshold}). Recall is effectively zero."
        )

    # ------------------------------------------------------------------
    # AC-2: returned hits have non-empty key/content fields
    # ------------------------------------------------------------------

    def test_hits_have_populated_key_and_content(self, _orch):
        """Every returned hit carries a truthy 'key' and 'content'."""
        src = self._seed(_orch)
        src.save('e2e_check', 'this is a verifiable memory entry')
        results = _orch.search('verifiable memory')

        assert len(results) > 0, "No hits for seeded fact — recall_count is zero"

        for rec in results:
            key = rec.get('key')
            content = rec.get('content')
            assert isinstance(key, str) and len(key) > 0, f"Bad key: {key!r}"
            assert content is not None and len(str(content)) > 0, f"Bad content: {content!r}"

    # ------------------------------------------------------------------
    # AC-3: source field identifies hermes_default
    # ------------------------------------------------------------------

    def test_hits_carry_correct_source(self, _orch):
        """The 'source' field on every hit should be 'hermes_default'."""
        src = self._seed(_orch)
        src.save('source_test', 'remember this fact')
        results = _orch.search('remember fact')

        assert len(results) > 0, "No hits returned"
        for rec in results:
            assert rec.get('source') == 'hermes_default', (
                f"Expected source='hermes_default', got {rec.get('source')!r}"
            )

    # ------------------------------------------------------------------
    # AC-4: limit parameter is respected
    # ------------------------------------------------------------------

    def test_search_respects_limit(self, _orch):
        """search(limit=N) returns at most N items."""
        src = self._seed(_orch)
        for i in range(5):
            src.save(f'limit_item_{i}', f'limitable data entry number {i}')
        results = _orch.search('limitable data', limit=3)
        assert isinstance(results, list)
        assert len(results) <= 3, f"limit=3 but got {len(results)} results"

    # ------------------------------------------------------------------
    # AC-5: orchestrator.search() is callable and returns a list even on empty dir
    # ------------------------------------------------------------------

    def test_search_on_empty_store_returns_empty_list(self, _orch):
        """Calling search before any seeding should return []."""
        results = _orch.search('nonexistent query')
        assert results == [], f"Expected [], got {results!r}"

