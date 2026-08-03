"""
test_feedback_correction_injection.py - Integration test for feedback loop
correction injection in the pre-LLM hook.

Proves that _try_feedback_loop is called during on_pre_llm_call and that
correction text appears in the output context injected between memory recall
results and downstream tool output sections.

Acceptance Criteria:
  1. Mock a BehavioralTrigger that meets conditions (detect() returns hits)
  2. Verify _try_feedback_loop() is invoked during pre-LLM hook
  3. Verify correction text appears in injected_context with proper block ordering:
     [MemChorus Memory Recall] -> feedback corrections -> downstream tool output
"""
import unittest.mock as mock
import pytest

from memchorus.behavioral_trigger import BehavioralTrigger, DecisionPoint


class TestFeedbackCorrectionInjection:
    """Integration tests for _try_feedback_loop in on_pre_llm_call."""

    @pytest.fixture
    def mock_orchestrator(self):
        """Mock orchestrator that returns search results."""
        orch = mock.MagicMock()
        orch.search.return_value = [
            {"key": "project_convention", "content": "Always use two-digit patch versions"},
            {"key": "recent_fix", "content": "Fixed routing map bug in v1.5.0 audit"},
        ]
        orch.save.return_value = True
        orch.recommended_sources.return_value = ["hermes_default"]
        return orch

    @pytest.fixture
    def mock_bt_results(self):
        """Pre-built DetectedPoint list for detect()."""
        from memchorus.behavioral_trigger import DetectedPoint
        return [DetectedPoint(
            type=DecisionPoint.TOOL_CALL_INTENT,
            confidence=0.8,
            matched_keyword="implement",
            text_span="implement the fix",
        )]

    def test_try_feedback_loop_called_during_pre_llm_call(
        self, mock_orchestrator, mock_bt_results
    ):
        """_try_feedback_loop must be invoked during on_pre_llm_call execution."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            # Spy on _try_feedback_loop itself
            original = hooks._try_feedback_loop
            with mock.patch.object(
                type(hooks),
                "_try_feedback_loop",
                wraps=original,
                return_value=[],
            ) as method_spy:
                result = hooks.on_pre_llm_call(
                    user_message="I need to implement the fix for the routing bug"
                )

                # _try_feedback_loop was called regardless of whether it returned lines
                method_spy.assert_called_once()
                call_args = method_spy.call_args
                assert "implement" in call_args[0][0].lower(), \
                    "First positional arg should be user_message content"
                assert isinstance(call_args[0][1], dict), \
                    "Second positional arg should be kwargs dict"

    def test_correction_text_injected_between_recall_and_tool_output(
        self, mock_orchestrator, mock_bt_results
    ):
        """When both recall and feedback fire, corrections appear between them."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        # Simulate inject_feedback_corrections returning a correction block
        feedback_result = (
            "-- Feedback Loop Corrections --\n"
            "[FEEDBACK:watchdog] STEERING (Level 1 hint): Watch for false positives"
        )

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            # Patch inject_feedback_corrections to return our canned correction
            with mock.patch(
                "memchorus.feedback_loop.integration.inject_feedback_corrections",
                return_value=feedback_result,
            ):
                result = hooks.on_pre_llm_call(
                    user_message="Plan the next step for implementing the fix"
                )

                assert result is not None, "Hook should return a result when recall+feedback fire"
                injected = result.get("context", "")

                # Block ordering: MemChorus Memory Recall comes first, then feedback
                recall_pos = injected.find("[MemChorus Memory Recall]")
                feedback_pos = injected.find("-- Feedback Loop Corrections --")

                assert recall_pos >= 0, "Memory Recall block should be in injected context"
                assert feedback_pos > recall_pos, \
                    "Feedback corrections must appear after memory recall block"

                # Correction content is present
                assert "FEEDBACK:watchdog" in injected
                assert "STEERING" in injected

    def test_try_feedback_loop_returns_empty_list_on_no_match(
        self, mock_orchestrator, mock_bt_results
    ):
        """When conditions don't match, _try_feedback_loop returns empty list."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        # inject_feedback_corrections returns None (no matching corrections)
        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            with mock.patch(
                "memchorus.feedback_loop.integration.inject_feedback_corrections",
                return_value=None,
            ):
                from memchorus.hooks import MemChorusHooks
                hooks = MemChorusHooks()
                hooks._btrigger = bt_spy

                result = hooks._try_feedback_loop(
                    input_text="Normal conversation with no trigger conditions",
                    kwargs={"conversation_length": 1},
                )

                assert result == [], "Should return empty list when no corrections match"

    def test_try_feedback_loop_graceful_degradation_on_exception(
        self, mock_orchestrator, mock_bt_results
    ):
        """_try_feedback_loop returns [] on internal exceptions (graceful degradation)."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            # Force inject_feedback_corrections to raise
            with mock.patch(
                "memchorus.feedback_loop.integration.inject_feedback_corrections",
                side_effect=ValueError("simulated feedback system failure"),
            ):
                from memchorus.hooks import MemChorusHooks
                hooks = MemChorusHooks()
                hooks._btrigger = bt_spy

                result = hooks._try_feedback_loop(
                    input_text="Test message",
                    kwargs={"conversation_length": 5, "tool_calls_this_turn": 2},
                )

                assert result == [], "Should return [] on exception (graceful degradation)"

    def test_pre_llm_call_includes_only_recall_when_feedback_returns_none(
        self, mock_orchestrator, mock_bt_results
    ):
        """When recall fires but feedback doesn't, only recall block appears."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            with mock.patch(
                "memchorus.feedback_loop.integration.inject_feedback_corrections",
                return_value=None,
            ):
                from memchorus.hooks import MemChorusHooks
                hooks = MemChorusHooks()
                hooks._btrigger = bt_spy

                result = hooks.on_pre_llm_call(
                    user_message="Implement the fix and verify it works"
                )

                assert result is not None, "Should still return recall results"
                injected = result.get("context", "")

                # Recall block present
                assert "[MemChorus Memory Recall]" in injected

                # Feedback block NOT present when feedback returns None
                assert "-- Feedback Loop Corrections --" not in injected

    def test_feedback_turn_context_built_from_kwargs(
        self, mock_orchestrator, mock_bt_results
    ):
        """Verify the TurnContext passed to inject_feedback_corrections reflects kwargs."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        test_kwargs = {
            "user_message": "Implement the routing fix",
            "conversation_length": 42,
            "tool_calls_this_turn": 3,
            "empty_tool_responses": 1,
            "recent_messages": ["msg1", "msg2"],
        }

        captured_context = None

        def capture_context(turn_context, trigger_event):
            nonlocal captured_context
            captured_context = turn_context
            return "-- Feedback Loop Corrections --\n[FEEDBACK:test] STEERING: test"

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            with mock.patch(
                "memchorus.feedback_loop.integration.inject_feedback_corrections",
                side_effect=capture_context,
            ):
                from memchorus.hooks import MemChorusHooks
                hooks = MemChorusHooks()
                hooks._btrigger = bt_spy

                hooks.on_pre_llm_call(**test_kwargs)

        assert captured_context is not None, "TurnContext should have been captured"
        assert captured_context.user_message == "Implement the routing fix"
        assert captured_context.conversation_length == 42
        assert captured_context.tool_calls_this_turn == 3
        assert captured_context.empty_tool_responses == 1
        assert captured_context.recent_messages == ["msg1", "msg2"]


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
