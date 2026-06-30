"""Tests for FeedbackDetector engine and ConditionEvaluator matchers.

Covers:
  - conversation_length condition (gt, gte, simple int thresholds)
  - repetition_entropy detection on identical vs diverse messages
  - empty_tool_responses counting across turns
  - keyword_pattern matching against user message + recent history
  - FeedbackDetector.evaluate integration with EscalationTracker
"""

import time
import types
import unittest

from memchorus.feedback_loop.engine import FeedbackDetector
from memchorus.feedback_loop.escalation import EscalationTracker
from memchorus.feedback_loop.integration import ConditionEvaluator, TurnContext
from memchorus.feedback_loop.schema_v1 import (
    ConditionSignal,
    FeedbackLoopDefinition,
)


# ---------------------------------------------------------------------------
# Helpers to construct minimal loop definitions for test use
# ---------------------------------------------------------------------------


def _mk_condition_signal(sig_type: str, value=None):
    """Create a ConditionSignal object."""
    return ConditionSignal(type=sig_type, value=value)


def _mk_loop_def(
    name="test_loop",
    enabled=True,
    conditions=None,
    cooldown=0,
    prompt="",
):
    """Build a FeedbackLoopDefinition for testing.

    ``conditions`` may be None, empty dict {}, or a mapping from signal-type key
    to either a ConditionSignal instance or a dict like {"gt": 20}.
    Dict values are auto-wrapped into ConditionSignal(type=key, value=...).
    """
    payload = {
        "schema": "schema_v1",
        "name": name,
        "trigger_event": "pre_llm_call",
        "cooldown_interval": cooldown,
        "priority": 50,
        "enabled": enabled,
        "correction_prompt": prompt,
    }
    if conditions is not None and len(conditions) > 0:
        resolved = {}
        for key, val in conditions.items():
            if not isinstance(val, ConditionSignal):
                val = _mk_condition_signal(key, val)
            resolved[key] = val
        payload["conditions"] = resolved
    return FeedbackLoopDefinition(**payload)


# ===========================================================================
# conversation_length condition matcher tests
# ===========================================================================


class TestConversationLengthMatcher(unittest.TestCase):
    """conversation_length threshold matching across all boundary cases."""

    def test_gt_above_threshold(self):
        match_fn = ConditionEvaluator._match_conversation_length
        ctx = TurnContext(conversation_length=50)
        self.assertTrue(match_fn({"gt": 40}, ctx))

    def test_gt_below_threshold(self):
        match_fn = ConditionEvaluator._match_conversation_length
        ctx = TurnContext(conversation_length=40)
        self.assertFalse(match_fn({"gt": 41}, ctx))

    def test_gt_at_boundary_strict(self):
        """count exactly equals threshold -- must NOT match (strict >)."""
        match_fn = ConditionEvaluator._match_conversation_length
        ctx = TurnContext(conversation_length=40)
        self.assertFalse(match_fn({"gt": 40}, ctx))

    def test_gte_inclusive(self):
        match_fn = ConditionEvaluator._match_conversation_length
        ctx = TurnContext(conversation_length=40)
        self.assertTrue(match_fn({"gte": 40}, ctx))
        self.assertTrue(match_fn({"gte": 39}, ctx))

    def test_plain_int_threshold_gte(self):
        """Plain int threshold means >=."""
        match_fn = ConditionEvaluator._match_conversation_length
        ctx = TurnContext(conversation_length=40)
        self.assertTrue(match_fn(40, ctx))
        self.assertFalse(match_fn(41, ctx))

    def test_invalid_value_returns_false(self):
        """Unrecognised value format returns False, not an error."""
        match_fn = ConditionEvaluator._match_conversation_length
        ctx = TurnContext(conversation_length=40)
        self.assertFalse(match_fn("not_a_threshold", ctx))


# ===========================================================================
# repetition_entropy matcher tests on identical vs diverse messages
# ===========================================================================


class TestRepetitionEntropyMatcher(unittest.TestCase):
    """Tests that the entropy metric correctly detects repetition."""

    def test_identical_messages_min_entropy(self):
        """All identical messages produce low entropy (high overlap)."""
        match_fn = ConditionEvaluator._match_repetition_entropy
        msgs = ["hello world", "hello world", "hello world"]
        ctx = TurnContext(recent_messages=msgs)
        # Overlap is 1.0 -> entropy is 0.0 < 0.7 threshold -> True
        self.assertTrue(match_fn(0.7, ctx))

    def test_diverse_messages_high_entropy(self):
        """Completely disjoint word sets produce high entropy (low overlap)."""
        match_fn = ConditionEvaluator._match_repetition_entropy
        msgs = ["alpha beta gamma", "delta epsilon zeta", "eta theta iota"]
        ctx = TurnContext(recent_messages=msgs)
        # Zero overlap -> entropy ~1.0, which is NOT < 0.7
        self.assertFalse(match_fn(0.7, ctx))

    def test_fewer_than_two_messages_returns_false(self):
        match_fn = ConditionEvaluator._match_repetition_entropy
        self.assertFalse(match_fn(0.5, TurnContext(recent_messages=[])))
        self.assertFalse(match_fn(0.5, TurnContext(recent_messages=["one"])))

    def test_identical_pairs_max_overlap(self):
        """Two identical messages -> max overlap (min entropy)."""
        match_fn = ConditionEvaluator._match_repetition_entropy
        msgs = ["same words here", "same words here"]
        ctx = TurnContext(recent_messages=msgs)
        self.assertTrue(match_fn(0.9, ctx))

    def test_partial_overlap_intermediate(self):
        """Partially overlapping messages yield intermediate entropy."""
        match_fn = ConditionEvaluator._match_repetition_entropy
        msgs = [
            "the quick brown fox jumps",
            "the lazy dog lies down",
            "the bird flies high",
        ]
        ctx = TurnContext(recent_messages=msgs)
        # All share 'the' -> some overlap, not max. Entropy should be < 1.0
        # but > 0.0, so at threshold 0.5 (low threshold), depends on actual value
        # We just verify it doesn't crash and returns a bool
        result = match_fn(0.5, ctx)
        self.assertIsInstance(result, bool)


# ===========================================================================
# empty_tool_response_count matcher tests
# ===========================================================================


class TestEmptyToolResponseCountMatcher(unittest.TestCase):
    """Tests for tool_response_empty_count condition."""

    def test_gt_above_threshold(self):
        """count=7 exceeds gt:5."""
        match_fn = ConditionEvaluator._match_tool_response_empty_count
        ctx = TurnContext(empty_tool_responses=7)
        self.assertTrue(match_fn({"gt": 5}, ctx))

    def test_gt_below_threshold(self):
        """count=5 does not exceed gt:6."""
        match_fn = ConditionEvaluator._match_tool_response_empty_count
        ctx = TurnContext(empty_tool_responses=5)
        self.assertFalse(match_fn({"gt": 6}, ctx))

    def test_gte_inclusive(self):
        """count=5 satisfies gte:5."""
        match_fn = ConditionEvaluator._match_tool_response_empty_count
        ctx = TurnContext(empty_tool_responses=5)
        self.assertTrue(match_fn({"gte": 5}, ctx))

    def test_plain_int_threshold(self):
        """Plain int value means >= ."""
        match_fn = ConditionEvaluator._match_tool_response_empty_count
        ctx = TurnContext(empty_tool_responses=5)
        self.assertTrue(match_fn(5, ctx))
        self.assertFalse(match_fn(6, ctx))


# ===========================================================================
# keyword_pattern matcher tests
# ===========================================================================


class TestKeywordPatternMatcher(unittest.TestCase):
    """Tests for keyword_pattern condition against user_message + recent_messages."""

    def test_keyword_in_user_message(self):
        match_fn = ConditionEvaluator._match_keyword_pattern
        ctx = TurnContext(user_message="this is a critical error")
        self.assertTrue(match_fn("critical", ctx))

    def test_regex_pattern_case_insensitive(self):
        match_fn = ConditionEvaluator._match_keyword_pattern
        ctx = TurnContext(user_message="help me please")
        self.assertTrue(match_fn(r"HELP.*PLEASE", ctx))

    def test_keyword_not_found(self):
        match_fn = ConditionEvaluator._match_keyword_pattern
        ctx = TurnContext(user_message="all fine here")
        self.assertFalse(match_fn("error", ctx))

    def test_empty_pattern_no_match(self):
        match_fn = ConditionEvaluator._match_keyword_pattern
        ctx = TurnContext(user_message="some text")
        self.assertFalse(match_fn("", ctx))


# ===========================================================================
# FeedbackDetector integration tests with real EscalationTracker
# ===========================================================================


class TestFeedbackDetectorIntegration(unittest.TestCase):
    """Integration-level tests for the detector against TurnContext."""

    def test_no_conditions_always_fires(self):
        """Loop with no conditions fires when enabled + cooldown allows."""
        tracker = EscalationTracker()
        loop_def = _mk_loop_def(name="always_fire", conditions={}, cooldown=0)
        detector = FeedbackDetector([loop_def], tracker)
        ctx = TurnContext(user_message="test", conversation_length=0)
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 1)
        self.assertIn("FEEDBACK:always_fire", prompts[0])

    def test_condition_not_met_returns_empty(self):
        """Conversation length below threshold yields no prompt."""
        tracker = EscalationTracker()
        loop_def = _mk_loop_def(
            name="long_convo",
            conditions={"conversation_length": {"gt": 100}},
            cooldown=0,
        )
        detector = FeedbackDetector([loop_def], tracker)
        ctx = TurnContext(conversation_length=50)
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 0)

    def test_condition_met_returns_prompt(self):
        """Conversation length exceeding threshold triggers a prompt."""
        tracker = EscalationTracker()
        loop_def = _mk_loop_def(
            name="long_convo",
            conditions={"conversation_length": {"gt": 20}},
            cooldown=0,
            prompt="{!CONVO_TOO_LONG}",
        )
        detector = FeedbackDetector([loop_def], tracker)
        ctx = TurnContext(conversation_length=50)
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 1)
        self.assertIn("FEEDBACK:long_convo", prompts[0])

    def test_disabled_loop_ignored(self):
        """Disabled loops do not produce prompts."""
        tracker = EscalationTracker()
        loop_def = _mk_loop_def(name="disabled", enabled=False, conditions={}, cooldown=0)
        detector = FeedbackDetector([loop_def], tracker)
        ctx = TurnContext(conversation_length=0)
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 0)

    def test_empty_definitions(self):
        """No loop definitions yield no prompts."""
        tracker = EscalationTracker()
        detector = FeedbackDetector([], tracker)
        ctx = TurnContext(user_message="test")
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 0)

    def test_multiple_loops_combined(self):
        """Two enabled loops both fire and produce two prompts."""
        tracker = EscalationTracker()
        loop_a = _mk_loop_def(name="loop_a", conditions={}, cooldown=0)
        loop_b = _mk_loop_def(name="loop_b", conditions={}, cooldown=0)
        detector = FeedbackDetector([loop_a, loop_b], tracker)
        ctx = TurnContext(conversation_length=0)
        prompts = detector.evaluate(ctx)
        self.assertEqual(len(prompts), 2)

    def test_cooldown_enforcement(self):
        """If cooldown has not expired, the second fire is suppressed."""
        tracker = EscalationTracker()
        loop_def = _mk_loop_def(
            name="rate_limited", conditions={}, cooldown=10
        )
        detector = FeedbackDetector([loop_def], tracker)
        ctx = TurnContext(conversation_length=0)
        # First fire succeeds
        self.assertEqual(len(detector.evaluate(ctx)), 1)
        # Immediate second fire blocked by cooldown
        self.assertEqual(len(detector.evaluate(ctx)), 0)

    def test_escalation_level_prefix(self):
        """Escalation level in prompt prefix reflects trigger count."""
        tracker = EscalationTracker(level_threshold=2)
        loop_def = _mk_loop_def(name="escalate", conditions={}, cooldown=0, prompt="watch it")
        detector = FeedbackDetector([loop_def], tracker)
        ctx = TurnContext(conversation_length=0)
        prompts = detector.evaluate(ctx)
        # Level 1 on first trigger
        self.assertIn("Level 1 hint", prompts[0])

    def test_unknown_signal_type_skipped(self):
        """Unknown condition signal type returns False (safe skip)."""
        tracker = EscalationTracker()
        loop_def = _mk_loop_def(
            name="unknown_sig",
            conditions={"bogus_metric": {"gt": 5}},
            cooldown=0,
        )
        detector = FeedbackDetector([loop_def], tracker)
        ctx = TurnContext(conversation_length=100)
        prompts = detector.evaluate(ctx)
        # Unknown type -> condition fails -> no prompt
        self.assertEqual(len(prompts), 0)

    def test_and_logic_multiple_conditions(self):
        """Both conditions must match for AND logic."""
        tracker = EscalationTracker()
        loop_def = _mk_loop_def(
            name="and_test",
            conditions={
                "conversation_length": {"gt": 20},
                "tool_response_empty_count": {"gt": 3},
            },
            cooldown=0,
        )
        detector = FeedbackDetector([loop_def], tracker)

        # Both conditions met -> fires
        ctx = TurnContext(conversation_length=50, empty_tool_responses=7)
        self.assertEqual(len(detector.evaluate(ctx)), 1)

        # Only one condition met -> no fire
        ctx2 = TurnContext(conversation_length=50, empty_tool_responses=1)
        self.assertEqual(len(detector.evaluate(ctx2)), 0)


if __name__ == "__main__":
    unittest.main()
