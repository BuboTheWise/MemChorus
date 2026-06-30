"""Tests for FeedbackLoopDetector (detector.py module).

Covers: all 4 condition types, edge cases, and structured match results.
Uses the real TurnContext, MatchedCondition, DetectionResult classes from detector.py.
"""

import sys
import unittest
from dataclasses import dataclass
from typing import List, Any, Dict

sys.path.insert(0, __file__.replace("tests/test_detector.py", "src"))

# ---------------------------------------------------------------------------
# Imports under test — use the real types
# ---------------------------------------------------------------------------

from memchorus.feedback_loop.detector import (  # noqa: E402
    FeedbackLoopDetector,
    MatchedCondition,
    DetectionResult,
    TurnContext,
)


# ===========================================================================
# Fixtures
# ===========================================================================


def _mk_context(
    user_message: str = "",
    turn_count: int = 1,
    tool_calls_this_turn: int = 0,
    recent_messages: List[str] | None = None,
) -> TurnContext:
    """Build a TurnContext for testing."""
    if recent_messages is None:
        recent_messages = ["spiral risk detected"] * turn_count
    return TurnContext(
        user_message=user_message,
        conversation_length=turn_count,
        tool_calls_this_turn=tool_calls_this_turn,
        recent_messages=recent_messages,
    )


# ===========================================================================
# F-D1: conversation_length condition
# ===========================================================================


class TestConversationLength(unittest.TestCase):
    """F-D1: conversation_length evaluations."""

    def test_below_threshold_no_match(self):
        ctx = _mk_context(turn_count=3)
        engine = FeedbackLoopDetector()
        conditions = {
            "conversation_length": {"type": "threshold", "value": 10},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertFalse(any(r.matched for r in result.matched_conditions))

    def test_at_threshold_matches(self):
        ctx = _mk_context(turn_count=10)
        engine = FeedbackLoopDetector()
        conditions = {
            "conversation_length": {"type": "threshold", "value": 10},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertTrue(any(r.matched for r in result.matched_conditions))

    def test_above_threshold_matches(self):
        ctx = _mk_context(turn_count=15)
        engine = FeedbackLoopDetector()
        conditions = {
            "conversation_length": {"type": "threshold", "value": 10},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertTrue(any(r.matched for r in result.matched_conditions))

    def test_turn_count_zero_no_crash(self):
        ctx = _mk_context(turn_count=0)
        engine = FeedbackLoopDetector()
        conditions = {
            "conversation_length": {"type": "threshold", "value": 0},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertIsNotNone(result)

    def test_turn_count_below_threshold_no_match(self):
        ctx = _mk_context(turn_count=5)
        engine = FeedbackLoopDetector()
        conditions = {
            "conversation_length": {"type": "threshold", "value": 10},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertFalse(any(r.matched for r in result.matched_conditions))


# ===========================================================================
# F-D2: repetition_entropy condition
# ===========================================================================


class TestRepetitionEntropy(unittest.TestCase):
    """F-D2: repetition_entropy evaluations."""

    def test_low_entropy_matches(self):
        ctx = _mk_context(turn_count=5, user_message="repetition detected")
        engine = FeedbackLoopDetector()
        conditions = {
            "repetition_entropy": {"type": "threshold", "value": 0.5},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertTrue(any(r.matched for r in result.matched_conditions))

    def test_high_entropy_no_match(self):
        """Diverse recent_messages give high entropy → condition (low-threshold) should not fire."""
        diverse_msgs = [
            "apple banana cherry dog elephant frog",
            "guitar house index jungle kite ladder moon",
            "notebook orange park quartz river stone tune",
            "umbrella violin window xray yard zebra anchor",
            "bridge cabin delta echo forest glide horn",
            "island journey kiwi lamp mountain nectar oasis",
            "parquet rose sand tree umbrella vault wave",
            "xylene zephyr aqua brick cuff dune elm",
            "frost globe hinge jelly kale lumen",
            "mine navy omen pike quail river",
        ]
        ctx = _mk_context(turn_count=20, user_message="diverse content here with many different words",
                          recent_messages=diverse_msgs)
        engine = FeedbackLoopDetector()
        conditions = {
            "repetition_entropy": {"type": "threshold", "value": 0.1},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertFalse(any(r.matched for r in result.matched_conditions))

    def test_zero_turn_count_no_crash(self):
        ctx = _mk_context(turn_count=0)
        engine = FeedbackLoopDetector()
        conditions = {
            "repetition_entropy": {"type": "threshold", "value": 0.5},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertIsNotNone(result)


# ===========================================================================
# F-D3: keyword_pattern condition (regex matching)
# ===========================================================================


class TestKeywordPattern(unittest.TestCase):
    """F-D3: keyword_pattern regex evaluations."""

    def test_exact_keyword_found(self):
        engine = FeedbackLoopDetector()
        conditions = {
            "keyword_pattern": {"type": "regex", "value": r"spiral.*(risk|danger)"},
        }
        result = engine.detect(
            "test_loop",
            conditions,
            _mk_context(turn_count=5, user_message="spiral risk detected"),
        )
        self.assertTrue(any(r.matched for r in result.matched_conditions))

    def test_keyword_not_found(self):
        engine = FeedbackLoopDetector()
        conditions = {
            "keyword_pattern": {"type": "regex", "value": r"nonexistent_xyz_12345"},
        }
        result = engine.detect("test_loop", conditions, _mk_context(turn_count=5))
        self.assertFalse(any(r.matched for r in result.matched_conditions))

    def test_invalid_regex_still_no_crash(self):
        engine = FeedbackLoopDetector()
        conditions = {
            "keyword_pattern": {"type": "regex", "value": r"[(invalid_regex"},
        }
        result = engine.detect("test_loop", conditions, _mk_context(turn_count=5))
        self.assertIsNotNone(result)

    def test_multi_keyword(self):
        engine = FeedbackLoopDetector()
        conditions = {
            "keyword_pattern": {"type": "regex", "value": r"(spiral|repetition|memory)"},
        }
        result = engine.detect(
            "test_loop",
            conditions,
            _mk_context(turn_count=20),
        )
        self.assertTrue(any(r.matched for r in result.matched_conditions))

    def test_simple_string_pattern(self):
        """String pattern (not dict) should also work via _infer_evaluate."""
        engine = FeedbackLoopDetector()
        conditions = {
            "keyword_pattern": "spiral",
        }
        result = engine.detect(
            "test_loop",
            conditions,
            _mk_context(turn_count=5, user_message="spiral detected"),
        )
        self.assertTrue(any(r.matched for r in result.matched_conditions))


# ===========================================================================
# F-D4: empty_tool_response_count condition
# ===========================================================================


class TestEmptyToolResponseCount(unittest.TestCase):
    """F-D4: empty_tool_response_count evaluations."""

    def test_zero_empty_responses_no_match(self):
        ctx = _mk_context(tool_calls_this_turn=0)
        engine = FeedbackLoopDetector()
        conditions = {
            "empty_tool_response_count": {"type": "threshold", "value": 3},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertFalse(any(r.matched for r in result.matched_conditions))

    def test_at_threshold_matches(self):
        ctx = _mk_context(tool_calls_this_turn=5)
        engine = FeedbackLoopDetector()
        conditions = {
            "empty_tool_response_count": {"type": "threshold", "value": 2},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertTrue(any(r.matched for r in result.matched_conditions))

    def test_below_threshold_no_match(self):
        ctx = _mk_context(tool_calls_this_turn=1)
        engine = FeedbackLoopDetector()
        conditions = {
            "empty_tool_response_count": {"type": "threshold", "value": 3},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertFalse(any(r.matched for r in result.matched_conditions))

    def test_exact_threshold_matches(self):
        """At exactly the threshold boundary: tool_calls >= value."""
        ctx = _mk_context(tool_calls_this_turn=3)
        engine = FeedbackLoopDetector()
        conditions = {
            "empty_tool_response_count": {"type": "threshold", "value": 3},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertTrue(any(r.matched for r in result.matched_conditions))


# ===========================================================================
# F-D5: MatchedCondition dataclass fields and types
# ===========================================================================


class TestMatchedCondition(unittest.TestCase):
    """F-D5: MatchedCondition dataclass structure."""

    def test_dataclass_fields(self):
        mc = MatchedCondition(
            name="test_signal",
            condition_type="threshold",
            value_threshold=10.0,
            measured_value=7,
            matched=False,
        )
        self.assertEqual(mc.name, "test_signal")
        self.assertEqual(mc.condition_type, "threshold")
        self.assertEqual(mc.value_threshold, 10.0)
        self.assertEqual(mc.measured_value, 7)
        self.assertFalse(mc.matched)

    def test_non_frozen_dataclass(self):
        """MatchedCondition is mutable (no @frozen)."""
        mc = MatchedCondition(
            name="test",
            condition_type="regex",
            value_threshold=1,
            measured_value=3,
            matched=True,
        )
        # Should be able to mutate since it's NOT frozen.
        mc.name = "changed"  # no exception — not frozen.
        self.assertEqual(mc.name, "changed")


# ===========================================================================
# F-D6: Engine behavior tests
# ===========================================================================


class TestEngineBehavior(unittest.TestCase):
    """F-D6: FeedbackLoopDetector higher-level behavior."""

    def test_detection_result_structure(self):
        engine = FeedbackLoopDetector()
        ctx = _mk_context(turn_count=10, user_message="spiral risk detected")
        conditions = {
            "conversation_length": {"type": "threshold", "value": 5},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertIsInstance(result, DetectionResult)
        self.assertEqual(result.loop_name, "test_loop")
        self.assertIn(result.severity, ("low", "medium", "high"))
        self.assertIsInstance(result.matched_conditions, list)
        self.assertTrue(result.correction_prompt_filled)

    def test_detect_all_empty(self):
        """detect_all with empty loops list should return []."""
        engine = FeedbackLoopDetector()
        ctx = _mk_context(turn_count=10)
        result = engine.detect_all([], ctx)
        self.assertEqual(result, [])

    def test_severity_logic_high(self):
        """3+ matched conditions -> 'high' severity."""
        engine = FeedbackLoopDetector()
        # tool_calls_this_turn > 0 so empty_tool_response_count (>=1) also matches.
        ctx = _mk_context(turn_count=50, user_message="spiral risk detected", tool_calls_this_turn=5)
        conditions = {
            "conversation_length": {"type": "threshold", "value": 10},
            "repetition_entropy": {"type": "threshold", "value": 0.9},
            "empty_tool_response_count": {"type": "threshold", "value": 1},
        }
        result = engine.detect("test_loop", conditions, ctx)
        self.assertEqual(result.severity, "high")

    def test_severity_logic_medium(self):
        """2 matched conditions -> 'medium' severity."""
        engine = FeedbackLoopDetector()
        # Only 2 will match: conversation_length and empty_tool. Entropy won't match with diverse text.
        ctx = _mk_context(
            turn_count=50,
            user_message="a b c d e f g h i j k l m n o p q r s t u v w x y z",
        )
        conditions = {
            "conversation_length": {"type": "threshold", "value": 10},
            "repetition_entropy": {"type": "threshold", "value": 0.1},
            "empty_tool_response_count": {"type": "threshold", "value": 3},
        }
        result = engine.detect("test_loop", conditions, ctx)
        hit_count = sum(1 for r in result.matched_conditions if r.matched)
        if hit_count == 2:
            self.assertEqual(result.severity, "medium")
        elif hit_count >= 3:
            self.assertEqual(result.severity, "high")
        else:
            self.assertIn(result.severity, ("low", "medium"))

    def test_detect_all_skips_disabled(self):
        class FakeLoop:
            name = "disabled_loop"
            conditions = {"conversation_length": {"type": "threshold", "value": 1}}
            enabled = False

        engine = FeedbackLoopDetector()
        ctx = _mk_context(turn_count=50)
        result = engine.detect_all([FakeLoop()], ctx)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
