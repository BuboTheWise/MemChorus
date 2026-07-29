"""Integration tests for feedback correction injection in on_pre_llm_call.

Covers the injection path where FeedbackLoopIntegration evaluates TurnContext
during pre-LLM hooks and appends corrections alongside memory recall blocks:

  1. inject_feedback_corrections is called during on_pre_llm_call execution
  2. Both recall and feedback appear in correct block ordering
  3. Returns empty / None when no feedback loops match
  4. Graceful degradation on internal exceptions (returns None/[])
  5. Only recall block appears when feedback evaluates to None
  6. TurnContext built from kwargs is passed correctly

Uses unittest.TestCase for consistency with the existing suite.
"""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_loop_yaml(base_dir: str) -> None:
    """Create a minimal valid feedback loop definition YAML file."""
    import shutil

    # Clean up any leftover feedback dir so we start fresh.
    loop_dir_path = os.path.join(base_dir, "feedback")
    shutil.rmtree(loop_dir_path, ignore_errors=True)
    os.makedirs(loop_dir_path, exist_ok=True)

    yaml_content = textwrap.dedent("""\
        schema: schema_v1
        name: test_inject_loop
        trigger_event: pre_llm_call
        cooldown_interval: 0
        priority: 50
        enabled: true
        correction_prompt: "CORRECTION: this is a test feedback adjustment"
        conditions:
          kw:
            type: keyword_pattern
            value:
              - test
    """)
    with open(os.path.join(loop_dir_path, "test_loop.yaml"), "w") as f:
        f.write(yaml_content)


# --------------------------------------------------------------------------- #
# Test cases                                                                   #
# --------------------------------------------------------------------------- #


class TestFeedbackCorrectionInjection(unittest.TestCase):
    """Integration tests for the feedback correction injection path."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _make_loop_yaml(self.tmpdir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 1. Verify inject_feedback_corrections is called during on_pre_llm_call --

    @patch("memchorus.hooks._get_orchestrator")
    def test_injection_path_called_during_pre_llm_hook(
        self, mock_get_orchestrator
    ):
        """on_pre_llm_call fires the feedback loop injection path."""
        orchestrator = MagicMock()

        # Search returns results so we don't hit the early-None exit.
        orchestrator.search.return_value = [
            {"key": "k1", "content": "existing memory"},
        ]

        mock_get_orchestrator.return_value = orchestrator

        from memchorus.hooks import MemChorusHooks

        hooks = MemChorusHooks()

        # Track whether inject_feedback_corrections was actually called.
        with patch(
            "memchorus.feedback_loop.integration.inject_feedback_corrections"
        ) as mock_inject:
            mock_inject.return_value = "-- Feedback Loop Corrections --\ncorrection text"

            result = hooks.on_pre_llm_call(
                user_message="test message for recall",
            )

            # The injection function should be called exactly once.
            self.assertEqual(mock_inject.call_count, 1)
            # Verify TurnContext was passed (called as keyword arg 'turn_context').
            turn_ctx = mock_inject.call_args.kwargs["turn_context"]
            self.assertIsNotNone(result)

    # -- 2. When both recall and feedback fire, corrections appear after recall --

    @patch("memchorus.hooks._get_orchestrator")
    def test_both_blocks_appear_in_order(self, mock_get_orchestrator):
        """Recall block comes first, then feedback corrections."""
        orchestrator = MagicMock()
        orchestrator.search.return_value = [
            {"key": "k1", "content": "session context memory"},
        ]
        mock_get_orchestrator.return_value = orchestrator

        from memchorus.hooks import MemChorusHooks

        hooks = MemChorusHooks()

        with patch("memchorus.feedback_loop.integration.inject_feedback_corrections") as mock_inject:
            mock_inject.return_value = (
                "-- Feedback Loop Corrections --\ntest correction"
            )

            result = hooks.on_pre_llm_call(user_message="search terms here")

        self.assertIsNotNone(result)
        context = str(result["context"]) if isinstance(result, dict) else str(result.get("context", ""))
        recall_pos = context.find("[MemChorus Memory Recall]")
        feedback_pos = context.find("-- Feedback Loop Corrections --")
        self.assertLess(recall_pos, feedback_pos)

    # -- 3. Returns empty / None when no feedback loops match --

    @patch("memchorus.hooks._get_orchestrator")
    def test_no_match_returns_none(self, mock_get_orchestrator):
        """When inject_feedback_corrections returns None and no recall items exist, the hook returns None."""
        orchestrator = MagicMock()
        orchestrator.search.return_value = []  # no hits at all

        mock_get_orchestrator.return_value = orchestrator

        from memchorus.hooks import MemChorusHooks

        hooks = MemChorusHooks()

        with patch("memchorus.feedback_loop.integration.inject_feedback_corrections") as mock_inject:
            mock_inject.return_value = None

            result = hooks.on_pre_llm_call(user_message="nothing to match")
            self.assertIsNone(result)

    # -- 4. Graceful degradation when feedback integration raises internally --

    @patch("memchorus.hooks._get_orchestrator")
    def test_graceful_degradation_on_error(self, mock_get_orchestrator):
        """Internal exception in on_pre_llm_call returns None without crashing."""
        orchestrator = MagicMock()
        orchestrator.search.return_value = [
            {"key": "k1", "content": "a memory"},
        ]

        mock_get_orchestrator.return_value = orchestrator

        from memchorus.hooks import MemChorusHooks

        hooks = MemChorusHooks()

        with patch("memchorus.feedback_loop.integration.inject_feedback_corrections") as mock_inject:
            # Force an exception inside injection path.
            mock_inject.side_effect = RuntimeError("boom")

            result = hooks.on_pre_llm_call(user_message="test content")

        # The hook should still return data (recall exists), but without feedback block.
        self.assertIsNotNone(result)
        context_str = "" if result is None else str(result.get("context", ""))
        # Feedback block should not appear after the error.
        self.assertNotIn("Feedback Loop Corrections", context_str)

    # -- 5. Only recall appears when feedback returns None --

    @patch("memchorus.hooks._get_orchestrator")
    def test_only_recall_when_feedback_none(self, mock_get_orchestrator):
        """When orchestrator.search hits but feedback is empty, only the recall block appears."""
        orchestrator = MagicMock()
        orchestrator.search.return_value = [
            {"key": "recall_k", "content": "important context"},
        ]

        mock_get_orchestrator.return_value = orchestrator

        from memchorus.hooks import MemChorusHooks

        hooks = MemChorusHooks()

        with patch("memchorus.feedback_loop.integration.inject_feedback_corrections") as mock_inject:
            mock_inject.return_value = None  # no feedback corrections

            result = hooks.on_pre_llm_call(user_message="something")

        self.assertIsNotNone(result)
        context = "" if result is None else str(result.get("context", ""))
        self.assertIn("[MemChorus Memory Recall]", context)
        self.assertNotIn("Feedback Loop Corrections", context)

    # -- 6. TurnContext built from kwargs is passed correctly --

    @patch("memchorus.hooks._get_orchestrator")
    def test_turn_context_built_from_kwargs(self, mock_get_orchestrator):
        """TurnContext receives correct values extracted from hook kwargs."""
        orchestrator = MagicMock()
        orchestrator.search.return_value = [
            {"key": "k1", "content": "data"},
        ]

        mock_get_orchestrator.return_value = orchestrator

        from memchorus.hooks import MemChorusHooks

        hooks = MemChorusHooks()

        with patch("memchorus.feedback_loop.integration.inject_feedback_corrections") as mock_inject:
            mock_inject.return_value = None

            hooks.on_pre_llm_call(
                user_message="user query text",
                conversation_length=42,
                tool_calls_this_turn=3,
                empty_tool_responses=1,
                recent_messages=["m1", "m2"],
            )

            call_kwargs = mock_inject.call_args.kwargs
            turn_ctx = call_kwargs["turn_context"]
            # Verify TurnContext fields match what the hook extracted.
            self.assertEqual(turn_ctx.conversation_length, 42)
            self.assertEqual(turn_ctx.tool_calls_this_turn, 3)
            self.assertEqual(turn_ctx.empty_tool_responses, 1)

    # -- Additional: overall module integration smoke test --

    @patch("memchorus.hooks._get_orchestrator")
    def test_full_injection_returns_expected_shape(self, mock_get_orchestrator):
        """End-to-end happy path produces dict with 'source' and 'context' keys."""
        orchestrator = MagicMock()
        orchestrator.search.return_value = [
            {"key": "k1", "content": "retrieved item"},
        ]

        mock_get_orchestrator.return_value = orchestrator

        from memchorus.hooks import MemChorusHooks

        hooks = MemChorusHooks()

        with patch("memchorus.feedback_loop.integration.inject_feedback_corrections") as mock_inject:
            mock_inject.return_value = "-- Feedback Loop Corrections --\nCorr"

            result = hooks.on_pre_llm_call(user_message="happy path test")

        self.assertIsInstance(result, dict)
        self.assertEqual(result["source"], "memchorus_pre_llm_call")
        self.assertIn("context", result)


class TestFeedbackLoopIntegrationEvaluate(unittest.TestCase):
    """Unit-level tests for the FeedbackLoopIntegration.evaluate() pipeline."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        _make_loop_yaml(self.tmpdir)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- Verify evaluate returns correction prompts --

    def test_evaluate_returns_prompts_on_match(self):
        """Integration.evaluate() returns a list of prompt strings when conditions match."""
        from memchorus.feedback_loop.integration import (
            FeedbackLoopIntegration,
            TurnContext,
            TriggerEvent,
        )

        integration = FeedbackLoopIntegration.build(
            loop_dir=Path(self.tmpdir) / "feedback",
        )

        ctx = TurnContext(
            user_message="this is a test message with the keyword",
            conversation_length=0,
            tool_calls_this_turn=0,
            empty_tool_responses=0,
            recent_messages=[],
        )

        promos = integration.evaluate(ctx, TriggerEvent.PRE_LLM_CALL)
        # The kw should match "test_loop.yaml" (which has keyword 'test').
        self.assertTrue(promos)

    # -- Verify evaluate returns empty list on no-match keywords --

    def test_evaluate_returns_empty_when_no_match(self):
        """Integration evaluates to None/empty when conditions do not match."""
        from memchorus.feedback_loop.integration import (
            FeedbackLoopIntegration,
            TurnContext,
            TriggerEvent,
        )

        integration = FeedbackLoopIntegration.build(
            loop_dir=Path(self.tmpdir) / "feedback",
        )

        ctx = TurnContext(
            user_message="zebra pineapple cactus — no keyword match here",
            conversation_length=0,
            tool_calls_this_turn=0,
            empty_tool_responses=0,
            recent_messages=[],
        )

        promos = integration.evaluate(ctx, TriggerEvent.PRE_LLM_CALL)
        self.assertEqual(promos, [])

    # -- Verify cooldown respects interval --

    def test_cooldown_prevents_fire_within_window(self):
        """Loops do not fire again before cooldown expires (using a large cooldown)."""
        # Build a temp loop with large cooldown (max 3600 per schema validation).
        loop_dir = Path(self.tmpdir) / "feedback"
        yaml_content = textwrap.dedent("""\
            schema: schema_v1
            name: cooldown_test_loop
            trigger_event: pre_llm_call
            cooldown_interval: 3600
            priority: 50
            enabled: true
            correction_prompt: "cooldown test prompt"
            conditions:
              cdkw:
                type: keyword_pattern
                value:
                  - xyz
        """)
        with open(loop_dir / "2_cooldown.yaml", "w") as f:
            f.write(yaml_content)

        from memchorus.feedback_loop.integration import (
            FeedbackLoopIntegration,
            TurnContext,
            TriggerEvent,
        )

        integration = FeedbackLoopIntegration.build(loop_dir=loop_dir)

        ctx = TurnContext(
            user_message="xyz — this matches keyword and triggers the loop",
            conversation_length=0,
            tool_calls_this_turn=0,
            empty_tool_responses=0,
            recent_messages=[],
        )

        first_round = integration.evaluate(ctx, TriggerEvent.PRE_LLM_CALL)
        # First call should fire.
        self.assertTrue(first_round)

        second_call = integration.evaluate(ctx, TriggerEvent.PRE_LLM_CALL)
        # Second call should be blocked by cooldown.
        self.assertEqual(second_call, [])


# --------------------------------------------------------------------------- #
# Run tests when invoked directly                                              #
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover - invoked via pytest
    unittest.main()
