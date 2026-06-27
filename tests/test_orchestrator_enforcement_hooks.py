"""
Test orchestration enforcement hooks in save()/retrieve()/search().

These tests verify the behavioral enforcement wiring inside MemoryOrchestrator:

  BE-HOOK-1:  retrieve() calls BehavioralEnforcementManager when enforce_on_read=True
  BE-HOOK-2:  search() calls BehavioralEnforcementManager when enforce_on_read=True
  BE-HOOK-3:  save()    calls BehavioralEnforcementManager when enforce_on_write=True
  BE-HOOK-4:  hooks are silently skipped when enforcement is disabled
  BE-HOOK-5:  graceful degradation when manager fails
"""

from __future__ import annotations

import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.orchestrator import MemoryOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orch(config: dict | None = None):
    """Create an orchestrator with the given config (default sources will be stubbed)."""
    return MemoryOrchestrator(config=config or {})


class TestRetrieveEnforcementHook:
    """BE-HOOK-1 & BE-HOOK-4 (read path)"""

    def test_retrieve_calls_enforce_when_enabled(self):
        orch = _make_orch({'enforce_on_read': True, 'enforce_on_write': False})
        mock_manager = MagicMock()
        mock_manager.enforce.return_value.recall_context = []
        orch._enforcement_manager = mock_manager

        orch.retrieve("some_key")
        mock_manager.enforce.assert_called_once_with("some_key")

    def test_retrieve_skips_enforce_when_disabled(self):
        orch = _make_orch({'enforce_on_read': False, 'enforce_on_write': False})
        # Enforcement manager should never be created when both flags are off
        assert orch._get_enforcement_manager() is None


class TestSearchEnforcementHook:
    """BE-HOOK-1 & BE-HOOK-4 (read path)"""

    def test_search_calls_enforce_when_enabled(self):
        orch = _make_orch({'enforce_on_read': True, 'enforce_on_write': False})
        mock_manager = MagicMock()
        mock_manager.enforce.return_value.recall_context = []
        orch._enforcement_manager = mock_manager

        orch.search("test query", limit=5)
        mock_manager.enforce.assert_called_once_with("test query")


class TestSaveEnforcementHook:
    """BE-HOOK-3 & BE-HOOK-4 (write path)"""

    @patch.object(MemoryOrchestrator, '_initialize_default_sources')
    def test_save_calls_enforce_after_successful_write(self, mock_init):
        orch = _make_orch({'enforce_on_read': False, 'enforce_on_write': True})
        # Make at least one source available and returning True on save
        mock_src = MagicMock()
        mock_src.name = 'hermes_default'
        mock_src.is_available.return_value = True
        mock_src.save.return_value = True
        orch.memory_sources['hermes_default'] = mock_src

        mock_manager = MagicMock()
        mock_manager.enforce.return_value.triggered_points = 0
        mock_manager.enforce.return_value.errors = []
        orch._enforcement_manager = mock_manager

        result = orch.save('test_key', 'test_value')
        assert result is True
        # Verify enforcement fired AFTER the actual save
        call_args = mock_manager.enforce.call_args
        assert call_args is not None
        # The outcome text should mention the key that was saved
        assert 'test_key' in str(call_args)

    @patch.object(MemoryOrchestrator, '_initialize_default_sources')
    def test_save_skips_enforce_when_disabled(self, mock_init):
        orch = _make_orch({'enforce_on_read': False, 'enforce_on_write': False})
        assert orch._get_enforcement_manager() is None

    @patch.object(MemoryOrchestrator, '_initialize_default_sources')
    def test_save_does_not_call_enforce_on_failure(self, mock_init):
        orch = _make_orch({'enforce_on_read': False, 'enforce_on_write': True})
        mock_src = MagicMock()
        mock_src.name = 'hermes_default'
        mock_src.is_available.return_value = True
        mock_src.save.return_value = False  # <-- save fails
        orch.memory_sources['hermes_default'] = mock_src

        mock_manager = MagicMock()
        orch._enforcement_manager = mock_manager

        result = orch.save('failing_key', 'value')
        assert result is False
        mock_manager.enforce.assert_not_called()


class TestGracefulDegradation:
    """BE-HOOK-5"""

    @patch.object(MemoryOrchestrator, '_initialize_default_sources')
    def test_save_survives_manager_exception(self, mock_init):
        orch = _make_orch({'enforce_on_read': False, 'enforce_on_write': True})
        mock_src = MagicMock()
        mock_src.name = 'hermes_default'
        mock_src.is_available.return_value = True
        mock_src.save.return_value = True
        orch.memory_sources['hermes_default'] = mock_src

        # Manager explodes — the save itself should still succeed
        mock_manager = MagicMock()
        mock_manager.enforce.side_effect = RuntimeError("boom")
        orch._enforcement_manager = mock_manager

        result = orch.save('resilient_key', 'value')
        assert result is True  # save was not rolled back by manager failure


class TestLazyInitialization:
    """Verify _get_enforcement_manager only instantiates when needed."""

    def test_lazily_creates_manager(self):
        orch = _make_orch({'enforce_on_read': True, 'enforce_on_write': False})
        assert orch._enforcement_manager is None
        mgr = orch._get_enforcement_manager()
        # After first call it should be cached
        assert mgr is orch._enforcement_manager

    def test_no_manager_when_both_disabled(self):
        orch = _make_orch({'enforce_on_read': False, 'enforce_on_write': False})
        assert orch._get_enforcement_manager() is None
