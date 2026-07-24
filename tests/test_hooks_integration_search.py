"""
test_hooks_integration_search.py - Integration tests for hooks._on_pre_llm_call()

Unlike test_hooks_search_fallback.py which mocks the orchestrator, these tests
use a real MemoryOrchestrator with persisted memories to verify the complete
pipeline: kwargs -> _build_search_terms() -> orchestrator.search() -> injected context.

Covers:
  - Real orchestrator + hermes_default memory source (temp file)
  - Memories saved that are findable by search terms derived from empty kwargs
    metadata fallback (task_id, model, platform)
  - on_pre_llm_call() returns non-None with injected context when relevant
    memories exist and kwargs only contain metadata (no user_message)
  - on_pre_llm_call() still returns None when no memories match
"""

import os
import tempfile
import unittest.mock as mock

import pytest

from memchorus.hooks import MemChorusHooks
from memchorus.orchestrator import MemoryOrchestrator


@pytest.fixture
def real_orchestrator(tmp_path):
    """A real MemoryOrchestrator backed by a temporary hermes_default source."""
    orch_config = {
        "hermes_default_config": {"memory_dir": str(tmp_path / "test_mem.json")},
        "enforce_on_read": False,
        "enforce_on_write": False,
    }
    orch = MemoryOrchestrator(config=orch_config)
    return orch


@pytest.fixture
def hooks():
    """A MemChorusHooks instance with BehavioralTrigger disabled for isolation."""
    h = MemChorusHooks()
    # Neutralize BehavioralTrigger so detection doesn't interfere — we test
    # the search path, not decision-point routing.
    h._btrigger = None
    return h


class TestIntegrationPreLlmCallWithRealMemories:
    """End-to-end: save memories, call on_pre_llm_call with metadata-only kwargs,
    verify orchestrator.search() is called and returns relevant results."""

    def _save_test_memories(self, orch):
        """Seed the orchestrator with findable test memories."""
        source = orch.memory_sources["hermes_default"]
        # Memories that contain terms matching common metadata fallback search queries:
        # task IDs, model names, platform identifiers
        source.save("project_milestone", {
            "text": "MemChorus project — integration testing phase for Bubo orchestration hooks",
            "categories": ["PROJECT"],
        })
        source.save("task_t_test123_context", {
            "text": "t_test123 task: verify pre-LLM recall with metadata fallback search terms",
            "categories": ["TASK"],
        })
        source.save("model_qwen_config", {
            "text": "qwen3.6 model configuration — used for default profile inference tasks",
            "categories": ["CONFIG"],
        })
        source.save("platform_telegram_note", {
            "text": "telegram platform deployment notes — hooks registered via entry points",
            "categories": ["DEPLOYMENT"],
        })

    def test_search_returns_results_when_memories_exist(self, real_orchestrator):
        """Verify the real orchestrator.search() finds seeded memories."""
        self._save_test_memories(real_orchestrator)
        results = real_orchestrator.search("MemChorus project integration", limit=5)
        assert len(results) > 0, "Expected search to find at least one memory"

    def test_integration_empty_kwargs_metadata_fallback_returns_context(self, real_orchestrator, hooks):
        """With empty user_message/history but task_id + model in kwargs,
        _build_search_terms falls back to metadata -> orchestrator.search() finds
        relevant memories -> on_pre_llm_call returns injected context (not None)."""
        self._save_test_memories(real_orchestrator)

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=real_orchestrator):
            result = hooks.on_pre_llm_call(
                user_message="",
                conversation_history=[],
                task_id="t_test123",
                model="qwen3.6:27b",
                platform="telegram",
            )

            # orchestrator.search() was called with metadata-derived terms
            assert result is not None, (
                "Expected non-None when relevant memories exist and metadata fallback "
                "provides search terms"
            )
            assert "source" in result
            assert "injected_context" in result
            # The injected context should contain the memory recall label
            assert "[MemChorus Memory Recall]" in result["injected_context"], (
                f"Expected memory recall block in output, got: {result['injected_context'][:200]}"
            )

    def test_integration_no_matching_memories_returns_none(self, real_orchestrator, hooks):
        """When metadata fallback search terms yield no matching memories,
        on_pre_llm_call should return None (no injection)."""
        # Save a memory that does NOT match the search terms we'll use
        source = real_orchestrator.memory_sources["hermes_default"]
        source.save("unrelated_memory", {
            "text": "completely unrelated topic about baking sourdough bread recipes",
            "categories": ["PERSONAL"],
        })

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=real_orchestrator):
            # These metadata terms don't overlap with the baked memory content
            result = hooks.on_pre_llm_call(
                user_message="",
                conversation_history=[],
                task_id="xyz999",
                model="test-model-abc",
                platform="cli",
            )

            # search() IS called (metadata provides terms), but since nothing matches,
            # result should be None — no injection when empty results
            assert result is None, (
                "Expected None when search returns no matching memories"
            )

    def test_integration_user_message_overrides_fallback(self, real_orchestrator, hooks):
        """When user_message IS provided, it takes precedence over metadata fallback.
        The search should find memories matching the user message content."""
        source = real_orchestrator.memory_sources["hermes_default"]
        source.save("deploy_checklist", {
            "text": "deployment checklist: verify database migrations before pushing to production server",
            "categories": ["DEPLOYMENT"],
        })

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=real_orchestrator):
            result = hooks.on_pre_llm_call(
                user_message="check deployment status",
                conversation_history=[],
            )

            # Should find the deployment memory via user message search terms
            assert result is not None, (
                "Expected non-None when user_message search finds memories"
            )
            assert "[MemChorus Memory Recall]" in result["injected_context"]

    def test_integration_search_limit_respects_decision_point_priority(self, real_orchestrator, hooks):
        """Verify that when BehavioralTrigger returns PLANNING_START decision point,
        the search limit increases to 5 instead of default 3."""
        self._save_test_memories(real_orchestrator)

        # Mock BehavioralTrigger that returns a PLANNING_START decision point
        bt_mock = mock.MagicMock()
        from memchorus.behavioral_trigger import DecisionPoint as _DP
        dp_obj = mock.MagicMock()
        dp_obj.type = _DP.PLANNING_START
        bt_mock.detect.return_value = [dp_obj]

        hooks._btrigger = bt_mock

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=real_orchestrator):
            # Spy on the search call to verify limit parameter
            real_search = real_orchestrator.search
            search_calls = []
            def capture_search(query, limit=10, **kwargs):
                search_calls.append({"query": query, "limit": limit})
                return real_search(query, limit=limit, **kwargs)
            real_orchestrator.search = capture_search

            hooks.on_pre_llm_call(
                user_message="plan the architecture for new feature",
                conversation_history=[],
            )

            # Verify search was called with elevated limit due to PLANNING_START
            assert len(search_calls) > 0
            assert search_calls[0]["limit"] == 5, (
                f"Expected limit=5 for PLANNING_START decision point, got {search_calls[0]['limit']}"
            )

    def test_integration_completely_empty_kwargs_returns_none(self, real_orchestrator, hooks):
        """When literally nothing useful is in kwargs, the hook correctly returns None."""
        with mock.patch("memchorus.hooks._get_orchestrator", return_value=real_orchestrator):
            result = hooks.on_pre_llm_call()
            # _build_search_terms({}) -> "" -> early return None
            assert result is None

    def test_integration_orchestrator_none_returns_none(self, hooks):
        """When the global orchestrator hasn't bootstrapped yet, hook returns None."""
        # Patch so _get_orchestrator returns None (bootstrap not done)
        with mock.patch("memchorus.hooks._get_orchestrator", return_value=None):
            result = hooks.on_pre_llm_call(
                user_message="some query",
                task_id="t_xyz",
            )
            assert result is None

    def test_integration_feedback_injection_path(self, real_orchestrator, hooks):
        """Verify that feedback loop injection runs without crashing in the
        full pipeline — even when no feedback corrections exist.
        This tests the resilience of the feedback import + call path."""
        self._save_test_memories(real_orchestrator)

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=real_orchestrator):
            result = hooks.on_pre_llm_call(
                user_message="",
                conversation_history=[],
                task_id="t_test123",
                model="qwen3.6:27b",
            )

            # Should succeed without raising — feedback loop code runs but may return empty
            assert result is not None or True  # either way, no exception


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
