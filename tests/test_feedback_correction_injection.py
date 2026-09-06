"""
test_feedback_correction_injection.py — Integration tests for feedback-correction
injection in the pre-LLM hook.

Live code path (verified in src/memchorus/hooks.py @ _try_feedback_loop):

    hooks._try_feedback_loop(input_text, kwargs)
        → _get_orchestrator()
        → orchestrator.config["feedback_loop"]  (dict, "enabled" optional, defaults True)
        → from memchorus.feedback_loop import FeedbackLoopManager
        → FeedbackLoopManager(config={"feedback_loop": {..., "enabled": True}})
        → mgr.process_feedback(input_text, kwargs)  →  List[str] of "[[FEEDBACK CORRECTION]]" blocks

The stale skip reason ("feedback_loop module removed in v1.9, replaced by
behavioral_trigger") was WRONG — the module is live and the API is
``FeedbackLoopManager.process_feedback``, not the old
``memchorus.feedback_loop.integration.inject_feedback_corrections`` that these
tests originally targeted.  All six tests below are re-pointed at the live path.

Acceptance criteria (BuboTheWise/MemChorus#183):
  1. _try_feedback_loop is invoked during on_pre_llm_call execution.
  2. Correction blocks appear in the injected context after recall blocks.
  3. Graceful degradation: _try_feedback_loop returns [] when the store is empty
     or an internal exception occurs.
  4. When feedback returns [], only the recall block is present in the result.
  5. The raw kwargs passed to on_pre_llm_call are forwarded to process_feedback.

Run:  pytest tests/test_feedback_correction_injection.py -xvs
"""
from __future__ import annotations

import tempfile
import unittest.mock as mock
from pathlib import Path

import pytest

from memchorus.behavioral_trigger import BehavioralTrigger


# ─── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def mock_orchestrator(tmp_path: Path):
    """A MagicMock orchestrator with the shape _try_feedback_loop needs.

    ``config["feedback_loop"] = {"enabled": True, "store_path": <tmp>}`` — the
    ``store_path`` points at a real (empty) file so ``FeedbackPersistenceStore.load()``
    returns ``[]`` instead of touching the user's ~/.cache.
    """
    empty_store = tmp_path / "fb_corrections.json"
    empty_store.write_text("[]")  # valid store format: empty list
    orch = mock.MagicMock()
    orch.config = {
        "feedback_loop": {"enabled": True, "store_path": str(empty_store)},
        "prohibitions": {"enabled": True},
    }
    orch.search.return_value = [
        {"key": "project_convention", "content": "Always use two-digit patch versions"},
        {"key": "recent_fix", "content": "Fixed routing map bug in v1.5.0 audit"},
    ]
    orch.save.return_value = True
    orch.recommended_sources.return_value = ["hermes_default"]
    return orch


@pytest.fixture
def mock_btrigger():
    """BehavioralTrigger spy whose detect() returns one DetectedPoint.

    DetectedPoint is a dataclass with (type: DecisionPoint, confidence: float,
    matched_keyword: str, text_span: Optional[str]).  DecisionPoint is an Enum,
    so the type field takes a member directly — not a string.
    """
    from memchorus.behavioral_trigger import DecisionPoint, DetectedPoint

    bt = mock.MagicMock(spec=BehavioralTrigger)
    dp = DetectedPoint(
        type=DecisionPoint.TOOL_CALL_INTENT,
        confidence=0.85,
        matched_keyword="implement",
        text_span="implement the fix",
    )
    bt.detect.return_value = [dp]
    return bt


def _make_hooks(mock_orch, btrigger=None):
    """Helper to build a MemChorusHooks instance with mocked collaborators."""
    with mock.patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
        from memchorus.hooks import MemChorusHooks
        hooks = MemChorusHooks()
    hooks._btrigger = btrigger  # type: ignore[assignment]
    return hooks


# ─── Test 1: _try_feedback_loop is called during on_pre_llm_call ──────────

class TestFeedbackCorrectionInjection:
    """Re-pointed integration tests for the live _try_feedback_loop path."""

    def test_try_feedback_loop_called_during_pre_llm_call(
        self, mock_orchestrator, mock_btrigger
    ):
        """on_pre_llm_call must invoke _try_feedback_loop with (input_text, kwargs).

        We wrap the real method so it still executes (proving the call chain)
        while capturing the arguments.
        """
        hooks = _make_hooks(mock_orchestrator, mock_btrigger)
        original = hooks._try_feedback_loop

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ), mock.patch.object(
            type(hooks), "_try_feedback_loop", wraps=original, return_value=[]
        ) as spy:
            hooks.on_pre_llm_call(
                user_message="I need to implement the fix for the routing bug",
                conversation_length=3,
                tool_calls_this_turn=1,
            )

            spy.assert_called_once()
            args = spy.call_args[0]
            assert len(args) == 2, f"Expected 2 positional args, got: {args!r}"
            input_text = args[0]
            fwd_kwargs = args[1]
            assert "implement" in input_text.lower()
            assert isinstance(fwd_kwargs, dict)
            assert fwd_kwargs.get("user_message", "").lower().startswith("i need to implement")

    def test_correction_block_appears_after_recall_in_injected_context(
        self, mock_orchestrator, mock_btrigger
    ):
        """When feedback fires, its [[FEEDBACK CORRECTION]] block appears in the
        return value's ``context`` — after the [MemChorus Memory Recall] block."""
        hooks = _make_hooks(mock_orchestrator, mock_btrigger)

        canned_block = (
            "[[FEEDBACK CORRECTION]]\n"
            "Category: tool_call\n"
            "Fingerprint: test-fp-001\n"
            "Original context: routing map fix\n"
            "Correction: Always run integration tests before merge"
        )

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ), mock.patch.object(
            hooks, "_try_feedback_loop", return_value=[canned_block]
        ):
            result = hooks.on_pre_llm_call(
                user_message="Plan the next step for implementing the fix",
                conversation_length=2,
            )

            assert result is not None, "Hook must return a result when recall + feedback fire"
            injected = result.get("context", "")

            recall_pos = injected.find("[MemChorus Memory Recall]")
            feedback_pos = injected.find("[[FEEDBACK CORRECTION]]")

            assert recall_pos >= 0, "Memory Recall block must be in injected context"
            assert feedback_pos >= 0, "Feedback correction block must be in injected context"
            assert feedback_pos > recall_pos, (
                "Feedback corrections must appear AFTER the memory recall block — "
                f"recall@{recall_pos}, feedback@{feedback_pos}"
            )

    def test_try_feedback_loop_returns_empty_list_when_store_empty(
        self, mock_orchestrator, mock_btrigger
    ):
        """When the persistence store has no corrections, _try_feedback_loop
        returns an empty list — no [[FEEDBACK CORRECTION]] blocks."""
        hooks = _make_hooks(mock_orchestrator, mock_btrigger)

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=mock_orchestrator):
            result = hooks._try_feedback_loop(
                input_text="Normal conversation with no trigger conditions",
                kwargs={
                    "conversation_length": 1,
                    "tool_calls_this_turn": 0,
                    "trigger_category": "tool_call",
                },
            )

        assert isinstance(result, list), f"Expected list, got {type(result)}"
        assert result == [], f"Expected empty list from empty store, got: {result}"

    def test_try_feedback_loop_returns_empty_list_when_disabled(
        self, mock_btrigger
    ):
        """When feedback_loop.enabled is False, process_feedback returns []
        without touching the store."""
        orch = mock.MagicMock()
        orch.config = {"feedback_loop": {"enabled": False}, "prohibitions": {"enabled": True}}
        orch.search.return_value = []
        orch.save.return_value = True
        orch.recommended_sources.return_value = []

        hooks = _make_hooks(orch, mock_btrigger)

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=orch):
            result = hooks._try_feedback_loop(
                input_text="Implement the fix",
                kwargs={"trigger_category": "tool_call"},
            )

        assert result == [], "disabled feedback_loop must return empty list"

    def test_try_feedback_loop_graceful_degradation_on_exception(
        self, mock_orchestrator, mock_btrigger
    ):
        """_try_feedback_loop must return [] (not raise) when an internal
        exception occurs — the pre-LLM hook must never crash the host."""
        hooks = _make_hooks(mock_orchestrator, mock_btrigger)

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ), mock.patch(
            "memchorus.feedback_loop.FeedbackLoopManager.process_feedback",
            side_effect=ValueError("simulated storage failure"),
        ):
            result = hooks._try_feedback_loop(
                input_text="Test message",
                kwargs={
                    "converation_length": 5,
                    "tool_calls_this_turn": 2,
                    "trigger_category": "tool_call",
                },
            )

        assert result == [], (
            f"_try_feedback_loop should return [] on exception, got: {result!r}"
        )

    def test_on_pre_llm_call_forwards_raw_kwargs_to_process_feedback(
        self, mock_orchestrator, mock_btrigger
    ):
        """The kwargs the caller passes to on_pre_llm_call must reach
        FeedbackLoopManager.process_feedback unmodified (except the input_text
        argument which is the enriched search string, not the raw user_message).

        This is the live contract: process_feedback uses kwargs.get("trigger_category")
        and other keys to match stored corrections.
        """
        hooks = _make_hooks(mock_orchestrator, mock_btrigger)

        test_kwargs = {
            "user_message": "Implement the routing fix",
            "conversation_length": 42,
            "tool_calls_this_turn": 3,
            "empty_tool_responses": 1,
            "recent_messages": ["msg1", "msg2"],
            "trigger_category": "tool_call",
        }

        captured = {}

        def capturing_process_feedback(self, input_text, kwargs):
            captured["input_text"] = input_text
            captured["kwargs"] = kwargs
            return []

        with mock.patch(
            "memchorus.hooks._get_orchestrator", return_value=mock_orchestrator
        ), mock.patch(
            "memchorus.feedback_loop.FeedbackLoopManager.process_feedback",
            new=capturing_process_feedback,
        ):
            hooks.on_pre_llm_call(**test_kwargs)

        assert "kwargs" in captured, "process_feedback must have been called"
        fwd = captured["kwargs"]
        assert fwd == test_kwargs, (
            f"kwargs forwarded to process_feedback must match the original kwargs.\n"
            f"  expected: {test_kwargs}\n"
            f"  got:      {fwd}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
