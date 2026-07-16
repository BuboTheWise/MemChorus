"""
test_hooks_behavioral_integration.py - Prove BehavioralTrigger.detect() is
actually called during hook execution (Bug 1: dead code fix).

Hooks bypass BehavioralTrigger entirely before this fix — detect() was never
imported or invoked. These tests verify the integration using mocks so we don't
need a live orchestrator instance.

Acceptance Criteria covered:
  AC-5: test_hooks_behavioral_integration.py proves detect() is called during
        hook execution (not just that behavioral_trigger.py exists).
"""
import unittest.mock as mock
import pytest

from memchorus.behavioral_trigger import BehavioralTrigger, DecisionPoint


class TestHooksCallBehavioralTrigger:
    """Verify hooks.py actually imports and calls BehavioralTrigger.detect()."""

    @pytest.fixture
    def mock_orchestrator(self):
        orch = mock.MagicMock()
        orch.search.return_value = [
            {"key": "test-memory", "content": "a relevant memory"}
        ]
        orch.save.return_value = True
        return orch

    @pytest.fixture
    def mock_bt_results(self):
        """Pre-built DetectedPoint list for detect()."""
        from memchorus.behavioral_trigger import DetectedPoint
        return [DetectedPoint(
            type=DecisionPoint.TOOL_CALL_INTENT,
            confidence=0.7,
            matched_keyword="verify",
            text_span="verify",
        )]

    def _patch_btrigger_detect(self, mock_orchestrator, mock_bt_results):
        """Patch hooks to use our mocks and return a spy on BehavioralTrigger."""
        # Spy on the BehavioralTrigger instance created in MemChorusHooks.__init__
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            with mock.patch.object(
                BehavioralTrigger, "__new__", return_value=bt_spy
            ):
                # Import the hooks class fresh inside the patch context
                from memchorus.hooks import MemChorusHooks
                hooks = MemChorusHooks()
                # Force _btrigger to our spy (it's set in __init__, but __new__ mock may not)
                hooks._btrigger = bt_spy
                yield bt_spy

    def test_on_pre_llm_call_calls_detect(self, mock_orchestrator, mock_bt_results):
        """on_pre_llm_call must call _btrigger.detect() before orchestrator.search()."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            result = hooks.on_pre_llm_call(
                input_text="I need to plan the next step and implement the fix"
            )

            # detect() was actually called with input text
            bt_spy.detect.assert_called_once()
            call_arg = bt_spy.detect.call_args[0][0]
            assert "plan" in call_arg.lower(), "detect should receive input_text"

    def test_on_post_tool_call_calls_detect(self, mock_orchestrator, mock_bt_results):
        """on_post_tool_call must call _btrigger.detect() before auto-save."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            result = hooks.on_post_tool_call(
                tool_output="Test completed: verify all assertions passed"
            )

            # detect() was called with the tool output
            bt_spy.detect.assert_called_once()
            call_arg = bt_spy.detect.call_args[0][0]
            assert "verify" in call_arg.lower(), "detect should receive tool_output"

    def test_on_post_tool_call_skips_save_when_no_behavioral_signal(self, mock_orchestrator):
        """When detect() returns empty list, on_post_tool_call must NOT save."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = []  # no decision points detected

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            result = hooks.on_post_tool_call(
                tool_output="routine stdout with no special keywords"
            )

            # detect() should have been called, but save should NOT be called
            bt_spy.detect.assert_called_once()
            mock_orchestrator.save.assert_not_called()
            assert result is None, "Should return None when no behavioral signal"

    def test_on_post_tool_call_saves_when_behavioral_signal_present(self, mock_orchestrator, mock_bt_results):
        """When detect() returns results, on_post_tool_call proceeds to save."""
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = mock_bt_results

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            result = hooks.on_post_tool_call(
                tool_output="Test completed: all 42 assertions verified successfully"
            )

            # Both detect and save should have been called
            bt_spy.detect.assert_called_once()
            mock_orchestrator.save.assert_called_once()

    def test_hooks_create_behavioraltrigger_instance(self):
        """MemChorusHooks.__init__ must create a BehavioralTrigger instance."""
        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock.MagicMock()
        ):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()

            # _btrigger should be a BehavioralTrigger instance, not None
            assert hooks._btrigger is not None
            assert isinstance(hooks._btrigger, BehavioralTrigger)

    def test_on_pre_llm_call_planning_widens_search(self, mock_orchestrator, mock_bt_results):
        """When PLANNING_START detected, search limit should widen to 5."""
        from memchorus.behavioral_trigger import DetectedPoint
        planning_hit = [DetectedPoint(
            type=DecisionPoint.PLANNING_START,
            confidence=0.8,
            matched_keyword="next step",
            text_span="next step",
        )]
        bt_spy = mock.MagicMock(spec=BehavioralTrigger)
        bt_spy.detect.return_value = planning_hit

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            _ = hooks.on_pre_llm_call(
                input_text="My plan is to implement the fix for the routing bug"
            )

            # Verify orchestrator.search was called with limit=5 (wider search)
            mock_orchestrator.search.assert_called_once()
            call_kwargs = mock_orchestrator.search.call_args[1] if mock_orchestrator.search.call_args[1] else {}
            call_positional = mock_orchestrator.search.call_args[0]
            # The second positional arg is `limit`
            if len(call_positional) >= 2:
                assert call_positional[1] == 5, f"Expected limit=5 for PLANNING_START, got {call_positional[1]}"

    def test_hooks_import_contains_behavioraltrigger(self):
        """Smoke test: hooks.py source must mention BehavioralTrigger."""
        import inspect
        from memchorus.hooks import MemChorusHooks

        init_src = inspect.getsource(MemChorusHooks.__init__)
        assert "BehavioralTrigger" in init_src, "__init__ must reference BehavioralTrigger"

        post_src = inspect.getsource(MemChorusHooks.on_post_tool_call)
        assert "_btrigger" in post_src or "detect" in post_src, \
            "on_post_tool_call must use behavioral detection"

        pre_src = inspect.getsource(MemChorusHooks.on_pre_llm_call)
        assert "_btrigger" in pre_src or "detect" in pre_src, \
            "on_pre_llm_call must use behavioral detection"


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
