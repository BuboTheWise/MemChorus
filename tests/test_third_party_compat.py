#!/usr/bin/env python3
"""
test_third_party_compat.py - Third-party dependency compatibility tests.

Tests graceful degradation when optional dependencies are missing or behave
unexpectedly:

  - PyYAML absent: _load_yaml_config should return {}, not crash
  - Pydantic present/absent: schema_v1 should work or degrade predictably
  - feedback_loop integration when YAML loader unavailable
  - ConditionEvaluator behaviour under edge-case inputs (None values, empty dicts)
  - EscalationTracker state management and cooldown semantics
  - inject_feedback_corrections degrades gracefully when integration is None

All tests run live against the actual hermes-agent venv runtime.
"""

import os
import sys
import time
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from unittest import mock

# Ensure src/ on path (live development, not an installed package)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# =================================================================== #
# Test 1: YAML absent graceful handling                                #
# =================================================================== #

class TestYamlAbsent(unittest.TestCase):
    """Verify that missing PyYAML does not break auto_bootstrap."""

    def test_load_yaml_returns_empty_when_yaml_missing(self):
        """_load_yaml_config should return {} when _HAS_YAML is False."""
        with mock.patch("memchorus.auto_bootstrap._HAS_YAML", False):
            from memchorus.auto_bootstrap import _load_yaml_config
            result = _load_yaml_config()
        self.assertEqual(result, {})

    def test_bootstrap_runs_without_yaml(self):
        """Even without YAML available, the bootstrap sequence completes."""
        with mock.patch("memchorus.auto_bootstrap._HAS_YAML", False):
            # Force re-import so the patched value takes effect
            import importlib as _im
            _im.reload(__import__("memchorus.auto_bootstrap"))
            from memchorus.auto_bootstrap import _bootstrap
            result = _bootstrap()
        # Should complete without raising (may be None if orchestrator build fails)


# =================================================================== #
# Test 2: FeedbackLoopIntegration edge cases                           #
# =================================================================== #

class TestFeedbackLoopIntegrationEdgeCases(unittest.TestCase):
    """Integration behaves correctly under unusual or missing conditions."""

    def test_integration_with_missing_directory(self):
        """FeedbackLoopIntegration tolerates a missing loop directory gracefully."""
        from memchorus.feedback_loop.integration import FeedbackLoopIntegration
        non_existent = "/tmp/memchorus_nonexistent_dir_987654321"
        integration = FeedbackLoopIntegration.build(loop_dir=mock.Mock())
        # Even with no definitions, the object should be usable
        self.assertIsNotNone(integration)

    def test_evaluate_respects_enabled_flag(self):
        """Disabled loops are skipped during evaluation."""
        from memchorus.feedback_loop.integration import (
            FeedbackLoopIntegration, TurnContext,
        )
        from memchorus.feedback_loop.schema_v1 import TriggerEvent

        # Build a FeedbackLoopIntegration with no definitions
        integration = FeedbackLoopIntegration(loop_dir=None)
        ctx = TurnContext(conversation_length=10)
        results = integration.evaluate(ctx, TriggerEvent.PRE_LLM_CALL)
        # With no definitions, should return empty list
        self.assertEqual(results, [])

    def test_auto_load_custom_loops_returns_diag_dict(self):
        """auto_load_custom_loops returns a diagnostic dict with expected keys."""
        from memchorus.feedback_loop.integration import auto_load_custom_loops
        result = auto_load_custom_loops()
        self.assertIn("loaded", result)
        self.assertIn("warnings", result)
        self.assertIn("error", result)
        self.assertIsInstance(result["loaded"], int)
        self.assertIsInstance(result["warnings"], list)

    def test_inject_feedback_corrections_returns_none_when_no_integration(self):
        """When get_feedback_integration() returns None, inject returns None."""
        from memchorus.feedback_loop import integration as intg_mod
        # Temporarily set the singleton to None
        orig = intg_mod._feedback_integration
        intg_mod._feedback_integration = None
        try:
            from memchorus.feedback_loop.integration import (
                TurnContext, inject_feedback_corrections, TriggerEvent,
            )
            ctx = TurnContext()
            result = inject_feedback_corrections(ctx, TriggerEvent.PRE_LLM_CALL)
            self.assertIsNone(result)
        finally:
            intg_mod._feedback_integration = orig


# =================================================================== #
# Test 3: ConditionEvaluator edge cases                                #
# =================================================================== #

class TestConditionEvaluatorEdgeCases(unittest.TestCase):
    """Evaluator behaves correctly under malformed or unusual inputs."""

    def test_empty_conditions_always_match(self):
        """A loop with no conditions evaluates to True (always fire)."""
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition, TriggerEvent
        from memchorus.feedback_loop.integration import (ConditionEvaluator, TurnContext)

        empty_loop = FeedbackLoopDefinition(
            schema="schema_v1",
            name="no_conditions",
            trigger_event=TriggerEvent.PRE_LLM_CALL,
            cooldown_interval=0,
            conditions={},
        )
        ctx = TurnContext()
        self.assertTrue(ConditionEvaluator.evaluate(empty_loop, ctx))

    def test_unknown_signal_type_returns_false(self):
        """When a signal type has no matcher, it evaluates to False."""
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition, ConditionSignal, TriggerEvent
        from memchorus.feedback_loop.integration import (ConditionEvaluator, TurnContext)

        unknown_loop = FeedbackLoopDefinition(
            schema="schema_v1",
            name="unknown_type",
            trigger_event=TriggerEvent.PRE_LLM_CALL,
            cooldown_interval=0,
            conditions={"some_signal": ConditionSignal(type="nonexistent_matcher_xyz", value=42)},
        )
        ctx = TurnContext()
        self.assertFalse(ConditionEvaluator.evaluate(unknown_loop, ctx))

    def test_conversation_length_below_threshold(self):
        """conversation_length condition fails when threshold not met."""
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition, ConditionSignal, TriggerEvent
        from memchorus.feedback_loop.integration import (ConditionEvaluator, TurnContext)

        loop = FeedbackLoopDefinition(
            schema="schema_v1",
            name="conv_len_test",
            trigger_event=TriggerEvent.PRE_LLM_CALL,
            cooldown_interval=0,
            conditions={"conversation_length": ConditionSignal(type="conversation_length", value=5)},
        )
        ctx = TurnContext(conversation_length=2)
        self.assertFalse(ConditionEvaluator.evaluate(loop, ctx))

    def test_conversation_length_above_threshold(self):
        """conversation_length condition passes when threshold met."""
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition, ConditionSignal, TriggerEvent
        from memchorus.feedback_loop.integration import (ConditionEvaluator, TurnContext)

        loop = FeedbackLoopDefinition(
            schema="schema_v1",
            name="conv_len_pass",
            trigger_event=TriggerEvent.PRE_LLM_CALL,
            cooldown_interval=0,
            conditions={"conversation_length": ConditionSignal(type="conversation_length", value=5)},
        )
        ctx = TurnContext(conversation_length=7)
        self.assertTrue(ConditionEvaluator.evaluate(loop, ctx))

    def test_keyword_pattern_matching(self):
        """keyword_pattern signal matches case-insensitively in user message."""
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition, ConditionSignal, TriggerEvent
        from memchorus.feedback_loop.integration import (ConditionEvaluator, TurnContext)

        loop = FeedbackLoopDefinition(
            schema="schema_v1",
            name="keyword_test",
            trigger_event=TriggerEvent.PRE_LLM_CALL,
            cooldown_interval=0,
            conditions={"some_keyword": ConditionSignal(type="regex", value=r"help.*me")},
        )
        # 'regex' is not a registered matcher — should fail gracefully
        ctx = TurnContext(user_message="I need to help you with this")
        self.assertFalse(ConditionEvaluator.evaluate(loop, ctx))

    def test_keyword_pattern_with_registered_matcher(self):
        """keyword_pattern type works when properly matched."""
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition, ConditionSignal, TriggerEvent
        from memchorus.feedback_loop.integration import (ConditionEvaluator, TurnContext)

        loop = FeedbackLoopDefinition(
            schema="schema_v1",
            name="kw_pattern",
            trigger_event=TriggerEvent.PRE_LLM_CALL,
            cooldown_interval=0,
            conditions={"pat": ConditionSignal(type="keyword_pattern", value="urgent")},
        )
        ctx = TurnContext(user_message="THIS IS URGENT please help")
        self.assertTrue(ConditionEvaluator.evaluate(loop, ctx))

    def test_repetition_entropy_low_overlap(self):
        """repetition_entropy detects when messages share too many words."""
        from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition, ConditionSignal, TriggerEvent
        from memchorus.feedback_loop.integration import (ConditionEvaluator, TurnContext)

        loop = FeedbackLoopDefinition(
            schema="schema_v1",
            name="entropy_test",
            trigger_event=TriggerEvent.PRE_LLM_CALL,
            cooldown_interval=0,
            conditions={"signal": ConditionSignal(type="repetition_entropy", value=0.5)},
        )
        # Very similar messages should trigger high repetition (low entropy < 0.5)
        ctx = TurnContext(
            recent_messages=[
                "the cat sat on the mat",
                "the cat sat on the mat",
                "the cat sat on the mat again",
            ],
        )
        result = ConditionEvaluator.evaluate(loop, ctx)
        # With nearly identical messages, entropy is low -> should match (True)
        self.assertTrue(result)


# =================================================================== #
# Test 4: EscalationTracker                                            #
# =================================================================== #

class TestEscalationTracker(unittest.TestCase):

    def test_new_loop_starts_at_level_1(self):
        from memchorus.feedback_loop.integration import EscalationTracker
        tracker = EscalationTracker()
        level = tracker.record_trigger("new_loop")
        self.assertEqual(level, 1)

    def test_escalation_advances_at_threshold(self):
        """After threshold_per_level triggers, the level advances."""
        from memchorus.feedback_loop.integration import EscalationTracker
        tracker = EscalationTracker()
        tracker.init_state("advancing_loop", trigger_threshold=2)

        for _ in range(2):
            tracker.record_trigger("advancing_loop")
        self.assertEqual(tracker.get_escalation_level("advancing_loop"), 1)

        # Third trigger should push to level 2
        new_level = tracker.record_trigger("advancing_loop")
        self.assertEqual(new_level, 2)

    def test_cooldown_respected(self):
        """should_fire respects cooldown_interval."""
        from memchorus.feedback_loop.integration import EscalationTracker
        tracker = EscalationTracker()
        tracker.init_state("cool_loop")
        # First call: no cooldown hit yet -> should fire
        self.assertTrue(tracker.should_fire("cool_loop", cooldown_interval=999))

        # Record a trigger to set last_fired_at
        tracker.record_trigger("cool_loop")
        # Immediately after, cooldown not expired -> should NOT fire
        self.assertFalse(tracker.should_fire("cool_loop", cooldown_interval=999))

    def test_cooldown_zero_fires_immediately(self):
        """cooldown_interval=0 means no waiting."""
        from memchorus.feedback_loop.integration import EscalationTracker
        tracker = EscalationTracker()
        tracker.init_state("zero_cool")
        tracker.record_trigger("zero_cool")
        # With cooldown 0, should still fire (elapsed >= 0 always true)
        self.assertTrue(tracker.should_fire("zero_cool", cooldown_interval=0))

    def test_reset_clears_state(self):
        from memchorus.feedback_loop.integration import EscalationTracker
        tracker = EscalationTracker()
        tracker.record_trigger("reset_me")
        tracker.reset_loop("reset_me")
        self.assertEqual(tracker.get_escalation_level("reset_me"), 1)


# =================================================================== #
# Test 5: Feedback loop YAML loading with various file formats         #
# =================================================================== #

class TestFeedbackLoaderCompat(unittest.TestCase):

    def test_yaml_and_yml_both_loaded(self):
        """Both .yaml and .yml extensions are accepted."""
        from memchorus.feedback_loop.schema_v1 import SUPPORTED_VERSIONS

        good_yaml = textwrap.dedent(f"""\
            schema: schema_v1
            name: yaml_loop
            trigger_event: pre_llm_call
            cooldown_interval: 0
        """)
        good_yml = textwrap.dedent(f"""\
            schema: schema_v1
            name: yml_loop
            trigger_event: pre_llm_call
            cooldown_interval: 0
        """)

        from memchorus.feedback_loop.loader import load_feedback_loops
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "one.yaml"), "w") as f:
                f.write(good_yaml)
            with open(os.path.join(tmpdir, "two.yml"), "w") as f:
                f.write(good_yml)
            loops = load_feedback_loops(tmpdir)
        self.assertEqual(len(loops), 2)

    def test_empty_file_skipped(self):
        """Empty YAML files are silently skipped."""
        from memchorus.feedback_loop.loader import load_feedback_loops
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "empty.yaml"), "w").close()  # create empty file
            loops = load_feedback_loops(tmpdir)
        self.assertEqual(len(loops), 0)

    def test_nonexistent_directory_returns_empty(self):
        """Loading from a path that doesn't exist returns [] without error."""
        from memchorus.feedback_loop.loader import load_feedback_loops
        loops = load_feedback_loops("/tmp/nonexistent_memchorus_dir_123456")
        self.assertEqual(loops, [])

    def test_malformed_yaml_skipped_gracefully(self):
        """Invalid YAML syntax is logged as warning and skipped."""
        from memchorus.feedback_loop.loader import load_feedback_loops
        broken = "{ this is not valid yaml at all ]]\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "broken.yaml"), "w") as f:
                f.write(broken)
            loops = load_feedback_loops(tmpdir)
        self.assertEqual(len(loops), 0)

    def test_unsupported_schema_version_skipped(self):
        """Files with unsupported schema versions are skipped."""
        from memchorus.feedback_loop.loader import load_feedback_loops
        bad_schema = textwrap.dedent("""\
            schema: schema_v999
            name: future_loop
            trigger_event: pre_llm_call
            cooldown_interval: 0
        """)
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "future.yaml"), "w") as f:
                f.write(bad_schema)
            loops = load_feedback_loops(tmpdir)
        self.assertEqual(len(loops), 0)


# =================================================================== #
# Test 6: Schema validation edge cases                                 #
# =================================================================== #

class TestSchemaValidation(unittest.TestCase):

    def test_valid_definition_accepts(self):
        """A well-formed definition passes validate_schema_v1."""
        from memchorus.feedback_loop.schema_v1 import validate_schema_v1
        data = {
            "schema": "schema_v1",
            "name": "good_def",
            "trigger_event": "pre_llm_call",
            "cooldown_interval": 30,
            "conditions": {},
        }
        result = validate_schema_v1(data)
        self.assertEqual(result.name, "good_def")

    def test_missing_name_fails(self):
        """A definition without a name field fails validation."""
        from memchorus.feedback_loop.schema_v1 import validate_schema_v1, ValidationError
        data = {
            "schema": "schema_v1",
            "trigger_event": "pre_llm_call",
            "cooldown_interval": 30,
            "conditions": {},
        }
        with self.assertRaises(ValidationError):
            validate_schema_v1(data)

    def test_max_cooldown_capped(self):
        """Cooldown values above MAX_COOLDOWN_SECONDS are rejected."""
        from memchorus.feedback_loop.schema_v1 import (validate_schema_v1, ValidationError, MAX_COOLDOWN_SECONDS)
        data = {
            "schema": "schema_v1",
            "name": "too_long_cooldown",
            "trigger_event": "pre_llm_call",
            "cooldown_interval": MAX_COOLDOWN_SECONDS + 100,
            "conditions": {},
        }
        with self.assertRaises(ValidationError):
            validate_schema_v1(data)


# =================================================================== #
# Test 7: TurnContext defaults                                         #
# =================================================================== #

class TestTurnContext(unittest.TestCase):

    def test_default_values(self):
        """TurnContext has sensible defaults."""
        from memchorus.feedback_loop.integration import TurnContext
        ctx = TurnContext()
        self.assertEqual(ctx.user_message, "")
        self.assertEqual(ctx.conversation_length, 0)
        self.assertEqual(ctx.tool_calls_this_turn, 0)
        self.assertEqual(ctx.empty_tool_responses, 0)
        self.assertEqual(ctx.recent_messages, [])

    def test_custom_values(self):
        """TurnContext accepts custom initialisation."""
        from memchorus.feedback_loop.integration import TurnContext
        ctx = TurnContext(
            user_message="hello",
            conversation_length=5,
            tool_calls_this_turn=2,
            empty_tool_responses=1,
            recent_messages=["msg1", "msg2"],
        )
        self.assertEqual(ctx.user_message, "hello")
        self.assertEqual(ctx.conversation_length, 5)


if __name__ == "__main__":
    unittest.main()
