#!/usr/bin/env python3
"""
test_orchestrator_e2e_recall.py - End-to-end integration test for orchestrator search pipeline.

This test validates the recall pipeline from start to finish:
  1. Seed facts with known terms into a fresh HermesDefault source
  2. Call orchestrator.search() with each query term
  3. Assert that at least half the queries return results with expected enriched keys
   
The enriched result shape carries 'score', 'preview' and '_domain' from the orchestrator
layer, proving the full pipeline is wired correctly (ranked scoring + preview synthesis).

Written as part of feat/v2.0.0-orchestrator-recall-pipeline (commit <TBD>).
References gap G3 / t_f25f824b — recall fix and wiring verification.
"""

import os
import sys
import shutil
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.orchestrator import MemoryOrchestrator


# Fact set + corresponding query terms used for seeding and recall verification.
# Each fact contains unique terms that the orchestrator should be able to retrieve
# regardless of source-layer search quirks (key-name matching, content scoring).
FACTS = {
    'user_pref_timeout': {
        'text': 'session timeout is 900 seconds',
        'categories': ['USER_PREFERENCE'],
        'importance_score': 0.8,
    },
    'project_goal': {
        'text': 'the main goal is to improve recall accuracy for memory systems',
        'categories': ['LONG_LIVED_KNOWLEDGE'],
    },
    'api_endpoint_ref': {
        'text': 'search endpoint lives at /v2/memorystore/search?q=',
        'categories': ['REFERENCE'],
    },
    'team_standup_rule': {
        'text': 'daily standup happens at 09:30 every weekday',
        'categories': ['EPHEMERAL'],
    },
    'deployment_target': {
        'text': 'production deployment runs on kubernetes cluster named prod-gamma',
        'categories': ['INFRASTRUCTURE'],
    },
    'design_pattern_note': {
        'text': 'strategy pattern was chosen over factory for extensibility needs',
        'categories': ['DESISION_RECORD'],
    },
}

# Query terms to search after seeding. Each query targets one or more fact entries' content.
QUERIES = [
    ('session timeout', FACTS['user_pref_timeout']['text']),
    ('recall accuracy memory systems', FACTS['project_goal']['text']),
    ('search endpoint memorystore', FACTS['api_endpoint_ref']['text']),
    ('daily standup weekday', FACTS['team_standup_rule']['text']),
    ('kubernetes prod-gamma deployment', FACTS['deployment_target']['text']),
    ('strategy pattern extensibility', FACTS['design_pattern_note']['text']),
]


@pytest.fixture()
def _orchestrator():
    """Provide a fresh orchestrator backed by a temporary HermesDefault memory directory."""
    tmpdir = tempfile.mkdtemp(prefix='memchorus_e2e_recall_')
    mem_dir = os.path.join(tmpdir, 'hermes_mem')
    config = {
        'default_source': 'hermes_default',
        'hermes_default_config': {'memory_dir': mem_dir},
        'enforce_on_read': False,          # disable enforcement during testing
        'enforce_on_write': False,
    }
    try:
        orch = MemoryOrchestrator(config)
        # Disable non-default sources so they don't interfere with recall assertions.
        for name in ['mempalace', 'session_history']:
            if name in orch.memory_sources:
                orch.disable_source(name)
            yield orch
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestOrchestratorSearchRecallPipeline:
    """End-to-end recall tests for the orchestrator search pipeline."""

    def test_seed_and_recall_with_enriched_shape(self, _orchestrator):
        """Seed every fact, then verify that at least half of queries return results,
        and those results carry the enriched keys (score, preview, _domain)."""
        
        # --- Phase 1: Seed facts -------------------------------------------------
        orch = _orchestrator
        seeds_saved = 0
        for key, value in FACTS.items():
            result = orch.save(key, value)
            assert result is True, f"Failed to save fact with key '{key}'"
            seeds_saved += 1
        
        assert seeds_saved == len(FACTS), 'All facts should have been saved'

        # --- Phase 2: Query and verify recall ------------------------------------
        queries_succeeded = 0
        for query, expected_content_fragment in QUERIES:
            results = orch.search(query)
