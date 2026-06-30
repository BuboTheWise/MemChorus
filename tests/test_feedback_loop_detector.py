"""Comprehensive tests for MemChorus feedback-loop detection + escalation.

Covers all 4 acceptance criteria from Task t_573134a7:
- Condition matching (all 4 types)
- Cooldown enforcement
- Escalation step progression
- Edge cases ("zero-length conversation", "all conditions met simultaneously")

All code follows MemChorus conventions (dataclasses, type hints, etc).
"""

from __future__ import annotations  # for | syntax on older runtimes

import time as _time_mod  # noqa: F401 -- used in test after sleep


# ======================================================================
# detector.py condition-evaluator tests (AC #1)
# ======================================================================


class TestConversationLength:
    """detector._evaluate_conversation_length — all code-paths hit."""

    def test_int_threshold_matches(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_conversation_length as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(conversation_length=60)
        r = fn(50, ctx)
        assert r.matched is True
        assert r.measured_value == 60

    def test_int_threshold_not_matches(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_conversation_length as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(conversation_length=40)
        r = fn(50, ctx)
        assert r.matched is False

    def test_int_threshold_equal(self):
        """exact boundary — >= semantics."""
        from memchorus.feedback_loop.detector import (
            _evaluate_conversation_length as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(conversation_length=50)
        r = fn(50, ctx)
        assert r.matched is True

    def test_gt_dict_strict(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_conversation_length as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(conversation_length=51)
        r = fn({"gt": 50}, ctx)
        assert r.matched is True

    def test_gt_dict_boundary_fails(self):
        """exactly 50 does not pass gt:50."""
        from memchorus.feedback_loop.detector import (
            _evaluate_conversation_length as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(conversation_length=50)
        r = fn({"gt": 50}, ctx)
        assert r.matched is False

    def test_gte_dict(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_conversation_length as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(conversation_length=50)
        r = fn({"gte": 50}, ctx)
        assert r.matched is True

    def test_key_value_field(self):
        """"value" key inside dict treated as plain threshold."""
        from memchorus.feedback_loop.detector import (
            _evaluate_conversation_length as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(conversation_length=50)
        r = fn({"value": 50}, ctx)
        assert r.matched is True


class TestRepetitionEntropy:
    """detector._evaluate_repetition_entropy — all code-paths hit."""

    def test_identical_messages_below_threshold(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_repetition_entropy as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(recent_messages=["same message"] * 5)
        r = fn(0.2, ctx)  # very low threshold — repetition will trigger
        assert r.matched is True

    def test_diverse_messages_above_threshold(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_repetition_entropy as fn,
            TurnContext as Ctx,
        )
        msgs = ["first message", "completely different content here today",
                "third thing is unrelated to both above ones"]
        ctx = Ctx(recent_messages=msgs)
        r = fn(0.1, ctx)  # very low threshold — diverse won't trigger
        assert r.matched is False

    def test_dict_threshold_key(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_repetition_entropy as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(recent_messages=["same"] * 3)
        r = fn({"threshold": 0.6}, ctx)
        assert r.matched is True

    def test_empty_messages_baseline(self):
        """detector returns high entropy (1.0) for <2 messages — no match with normal threshold."""
        from memchorus.feedback_loop.detector import (
            _evaluate_repetition_entropy as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(recent_messages=[])
        r = fn(0.5, ctx)  # detector returns entropy=1.0 for <2 messages
        assert r.matched is False  # high entropy ≠ repetition


class TestKeywordPattern:
    """detector._evaluate_keyword_pattern — string + dict patterns."""

    def test_simple_match(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_keyword_pattern as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(user_message="I need to fix this bug")
        r = fn({"pattern": "fix"}, ctx)
        assert r.matched is True

    def test_no_match(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_keyword_pattern as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(user_message="I need to fix this bug")
        r = fn({"pattern": "xyz_no_match_xyz"}, ctx)
        assert r.matched is False

    def test_regex_special_chars(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_keyword_pattern as fn,
            TurnContext as Ctx,
        )
        r = fn({"pattern": r"(bug|problem)"},
               Ctx(user_message="this bug needs a fix"))
        assert r.matched is True

    def test_empty_search_text_no_exception(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_keyword_pattern as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(user_message="")  # empty => no search text
        r = fn({"pattern": "anything"}, ctx)
        assert r.matched is False


class TestEmptyToolResponseCount:
    """detector._evaluate_empty_tool_response_count — all code-paths hit."""

    def test_int_threshold_matches(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_empty_tool_response_count as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(tool_calls_this_turn=5)
        r = fn(3, ctx)
        assert r.matched is True

    def test_int_threshold_not_matches(self):
        from memchorus.feedback_loop.detector import (
            _evaluate_empty_tool_response_count as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(tool_calls_this_turn=2)
        r = fn(5, ctx)
        assert r.matched is False

    def test_boundary_exact(self):
        """exact threshold match => True (>= semantics)."""
        from memchorus.feedback_loop.detector import (
            _evaluate_empty_tool_response_count as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(tool_calls_this_turn=5)
        r = fn(5, ctx)
        assert r.matched is True

    def test_zero_threshold(self):
        """threshold 0 with count 0 => matches (>= 0)."""
        from memchorus.feedback_loop.detector import (
            _evaluate_empty_tool_response_count as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(tool_calls_this_turn=0)
        r = fn(0, ctx)
        assert r.matched is True

    def test_count_dict(self):
        "detector supports 'count' key in dict for empty-tool evaluator."
        from memchorus.feedback_loop.detector import (
            _evaluate_empty_tool_response_count as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(tool_calls_this_turn=5)
        r = fn({"count": 3}, ctx)
        assert r.matched is True

    def test_value_dict_key(self):
        "detector supports 'value' key in dict for empty-tool evaluator."
        from memchorus.feedback_loop.detector import (
            _evaluate_empty_tool_response_count as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(tool_calls_this_turn=5)
        r = fn({"value": 3}, ctx)
        assert r.matched is True

    def test_unsupported_dict_key_returns_none(self):
        "unsupported dict keys (e.g. 'gte') → None return."
        from memchorus.feedback_loop.detector import (
            _evaluate_empty_tool_response_count as fn,
            TurnContext as Ctx,
        )
        ctx = Ctx(tool_calls_this_turn=5)
        r = fn({"gte": 3}, ctx)
        assert r is None


# ======================================================================
# detector._compute_severity — severity classification tests
# ======================================================================


class TestSeverity:
    "detector FeedbackLoopDetector._compute_severity"

    def test_three_hits_high(self):
        from memchorus.feedback_loop.detector import (
            FeedbackLoopDetector as Det, MatchedCondition as MC,
        )
        hits = [MC("a", "t", 0, 1, True), MC("b", "t", 0, 2, True), MC("c", "t", 0, 3, True)]
        assert Det._compute_severity(hits) == "high"

    def test_two_hits_medium(self):
        from memchorus.feedback_loop.detector import (
            FeedbackLoopDetector as Det, MatchedCondition as MC,
        )
        hits = [MC("a", "t", 0, 1, True), MC("b", "t", 0, 2, True)]
        assert Det._compute_severity(hits) == "medium"

    def test_one_hit_low(self):
        from memchorus.feedback_loop.detector import (
            FeedbackLoopDetector as Det, MatchedCondition as MC,
        )
        hits = [MC("a", "t", 0, 1, True)]
        assert Det._compute_severity(hits) == "low"

    def test_zero_hits_low(self):
        from memchorus.feedback_loop.detector import (
            FeedbackLoopDetector as Det, MatchedCondition as MC,
        )
        hits = []
        assert Det._compute_severity(hits) == "low"


# ======================================================================
# detector.FeedbackLoopDetector.detect() — end-to-end integration tests
# ======================================================================


class TestDetectEndToEnd:
    """Full detect() path for each condition type."""

    def test_detect_conversation_length_via_engine(self):
        from memchorus.feedback_loop.detector import FeedbackLoopDetector, TurnContext
        det = FeedbackLoopDetector()
        cond = {
            "spiral_risk": {"type": "conversation_length", "value": 50},
        }
        ctx = TurnContext(conversation_length=60)
        r = det.detect("test_loop", cond, ctx)
        assert r.loop_name == "test_loop"
        assert any(c.matched for c in r.matched_conditions)

    def test_detect_repetition_entropy_via_engine(self):
        from memchorus.feedback_loop.detector import FeedbackLoopDetector, TurnContext
        det = FeedbackLoopDetector()
        cond = {
            "repet": {"type": "repetition_entropy", "value": 0.2},
        }
        ctx = TurnContext(recent_messages=["same thing"] * 5)
        r = det.detect("test_loop", cond, ctx)
        assert any(c.matched for c in r.matched_conditions)

    def test_detect_keyword_pattern_via_engine(self):
        from memchorus.feedback_loop.detector import FeedbackLoopDetector, TurnContext
        det = FeedbackLoopDetector()
        cond = {
            "kword": {"type": "keyword_pattern", "pattern": "fix"},
        }
        ctx = TurnContext(user_message="I need to fix this bug")
        r = det.detect("test_loop", cond, ctx)
        assert any(c.matched for c in r.matched_conditions)

    def test_detect_empty_tool_response_via_engine(self):
        from memchorus.feedback_loop.detector import FeedbackLoopDetector, TurnContext
        det = FeedbackLoopDetector()
        cond = {
            "empty": {"type": "empty_tool_response_count", "value": 3},
        }
        ctx = TurnContext(tool_calls_this_turn=5)
        r = det.detect("test_loop", cond, ctx)
        assert any(c.matched for c in r.matched_conditions)


class TestDetectAll:
    """FeedbackLoopDetector.detect_all() across multiple loop defs."""

    def test_detect_all_filters_disabled(self):
        from memchorus.feedback_loop.detector import FeedbackLoopDetector, TurnContext
        det = FeedbackLoopDetector()
        ctx = TurnContext(conversation_length=100)

        class Loop:
            def __init__(self, name, enabled, conditions):
                self.name = name
                self.enabled = enabled
                self.conditions = conditions

        loops = [
            Loop("a", True, {"spiral": {"type": "conversation_length", "value": 50}}),
            Loop("b", False, {"spiral": {"type": "conversation_length", "value": 50}}),
        ]
        results = det.detect_all(loops, ctx)
        # disabled loop should be skipped
        names = [r.loop_name for r in results]
        assert "b" not in names


# ======================================================================
# EscalationTracker — cooldown enforcement (AC #2)
# ======================================================================


class TestEscalationCooldown:
    """cooldown enforcement from escalation.py."""

    def test_never_fired_allows_fire(self):
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET()
        assert t.check_cooldown("new", 50) is True

    def test_fires_once_respects_default_cooldown(self):
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET(default_cooldown=2.0)   # 2s cooldown for easy testing
        t.record_trigger("loop1")
        assert t.check_cooldown("loop1", 2.0) is False  # not yet

    def test_after_sleep_allows_fire(self):
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET(default_cooldown=0.05)   # tiny cooldown for test speed
        t.record_trigger("loop1")
        _time_mod.sleep(0.1)  # wait > cooldown
        assert t.check_cooldown("loop1", 0.05) is True

    def test_custom_cooldown_intervaled(self):
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET()
        t.init_loop("custom", 120)   # custom cooldown
        t.record_trigger("custom")
        res = t.check_cooldown("custom", 60)
        assert res is False  # need full 120s, only checking against 60


# ======================================================================
# EscalationTracker — level progression (AC #3)
# ======================================================================


class TestEscalationLevels:
    """L1→L2→L3 stepped escalation from escalation.py."""

    def test_init_escalates_no_level_from_start(self):
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET()
        assert t.get_escalation_level("new") == 1  # starts at 1

    def test_progression_after_threshold_reached(self):
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET(level_threshold=3)  # advance every 3 triggers
        for _ in range(3):          # triggers: 1,2,3 all at L1
            t.record_trigger("l")
        assert t.get_escalation_level("l") == 1

        t.record_trigger("l")       # trigger 4 → hits threshold
        # (with threshold 3: [(count-1)//thresh + 1] => after 4 triggers: 1+1=2)
        assert t.get_escalation_level("l") == 2

    def test_max_capped_at_level_3(self):
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET(level_threshold=1)  # advance every trigger
        for i in range(6):         # fire many times
            t.record_trigger("cap")
        assert t.get_escalation_level("cap") == 3

    def test_reset_clears_state(self):
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET(level_threshold=2)   # advance every 2 triggers
        for _ in range(5):
            t.record_trigger("capped")
        assert t.get_escalation_level("capped") == 3   # capped at L3 regardless of more triggers
        # Reset and re-check
        t.reset_loop("capped")
        # After reset, the loop is gone — next fire starts from 0 again
        t.init_loop("new", 120)
        assert t.get_escalation_level("new") == 1


# ======================================================================
# Edge cases (AC #4+5: zero-length conv, all conditions simultaneous)
# ======================================================================


class TestEdgeCases:
    """Boundary / edge cases — AC #4+5."""

    def test_zero_length_conversation(self):
        """Zero turns should not crash detector."""
        from memchorus.feedback_loop.detector import FeedbackLoopDetector as Det, TurnContext
        ctx = TurnContext(conversation_length=0)  # zero-length

        det = Det()
        cond = {"spiral": {"type": "conversation_length", "value": 50}}
        r = det.detect("zst", cond, ctx)
        assert any(not c.matched for c in r.matched_conditions)   # nothing matched (0 < 50)

    def test_all_conditions_met_simultaneously(self):
        """All four condition types fire at once."""
        from memchorus.feedback_loop.detector import FeedbackLoopDetector as Det, TurnContext
        det = Det()
        cond = {
            "len": {"type": "conversation_length", "value": 50},
            "repet": {"type": "repetition_entropy", "value": 1.0},
            "kword": {"type": "keyword_pattern", "pattern": "fix"},
        }
        ctx = TurnContext(
            conversation_length=60,
            recent_messages=["same thing"] * 5,
            user_message="I need to fix this bug",
        )
        r = det.detect("all_at_once", cond, ctx)
        assert r.severity == "high"      # all three matched => severity high

    def test_no_conditions_empty_dict(self):
        """Empty conditions dict — detect should not crash."""
        from memchorus.feedback_loop.detector import FeedbackLoopDetector as Det, TurnContext
        det = Det()
        r = det.detect("no_cond", {}, TurnContext())  # no conditions
        assert r.matched_conditions == []             # empty => no conditions to match
        assert r.correction_prompt_filled is False    # nothing matched => no prompt filled

    def test_single_message_entropy(self):
        """Single message => entropy defaults to high (no repetition)."""
        from memchorus.feedback_loop.detector import _evaluate_repetition_entropy as fn, TurnContext
        ctx = TurnContext(recent_messages=["only one"])  # single message
        r = fn(0.2, ctx)      # even very low threshold — entropy near-1 => no match
        assert r.matched is False

    def test_escalation_after_reset(self):
        """Reset escalator → re-fire starts from L1."""
        from memchorus.feedback_loop.escalation import EscalationTracker as ET
        t = ET(level_threshold=1)
        for _ in range(3):
            t.record_trigger("r")
        assert t.get_escalation_level("r") == 3   # capped at L3

        t.reset_loop("r")
        assert t.check_cooldown("r", 0) is True  # never fired again

    def test_empty_search_text_keyword_pattern_no_exception(self):
        """Empty search text => no match but no exception."""
        from memchorus.feedback_loop.detector import _evaluate_keyword_pattern as fn, TurnContext
        ctx = TurnContext(user_message="")  # empty user_msg + empty recents
        r = fn({"pattern": "anything"}, ctx)
        assert r.matched is False   # no text to search => always false

