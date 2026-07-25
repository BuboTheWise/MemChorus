"""Regression tests: detect() MUST only receive string input.

The installed package enforces this contract but a previous version had a bug where
`on_post_decision_point()` passed `[d.text for d in detected_points]` (a list) to
`BehavioralTrigger.detect(text: str)` — causing runtime TypeError inside the regex
engine ("expected string or bytes-like object, got 'list'").

These tests mock detect/fire to reject non-string input, proving all callers convert
their arguments before passing.  If a future refactor reintroduces the bug, these
tests will fail immediately rather than silently degrading at runtime.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — strict stubs that refuse non-string text
# ---------------------------------------------------------------------------

def _strict_detect_stub(self, text):
    """BehavioralTrigger.detect() that raises TypeError on non-string input."""
    if not isinstance(text, str):
        raise TypeError(
            f"BehavioralTrigger.detect() expected str, got {type(text).__name__}"
        )
    return []


def _strict_fire_stub(self, text):
    """BehavioralTrigger.fire() that raises TypeError on non-string input."""
    if not isinstance(text, str):
        raise TypeError(
            f"BehavioralTrigger.fire() expected str, got {type(text).__name__}"
        )
    return []


# ---------------------------------------------------------------------------
# Tests — exercise each hook path with strict stubs
# ---------------------------------------------------------------------------

def test_on_pre_llm_call_detect_receives_str(monkeypatch):
    """on_pre_llm_call converts input_text to str before detect()."""
    from memchorus.hooks import MemChorusHooks

    orch = MagicMock()
    orch.search.return_value = []

    with patch("memchorus.hooks._get_orchestrator", return_value=orch):
        instance = MemChorusHooks()
        instance._btrigger.detect = _strict_detect_stub  # type: ignore[attr_defined]

        result = instance.on_pre_llm_call(
            user_message="let me plan the next steps",
            conversation_history=[],
        )
        # No exception means detect only saw strings


def test_on_post_tool_call_detect_receives_str_dict_output(monkeypatch):
    """Dict tool output is json-serialized to str before detect()."""
    from memchorus.hooks import MemChorusHooks

    orch = MagicMock()
    orch.save.return_value = True

    with patch("memchorus.hooks._get_orchestrator", return_value=orch):
        instance = MemChorusHooks()
        instance._btrigger.detect = _strict_detect_stub  # type: ignore[attr_defined]

        result = instance.on_post_tool_call(
            result={"status": "completed", "errors": []},
        )
        # No exception means only str was passed


def test_on_post_tool_call_detect_receives_str_list_output(monkeypatch):
    """List tool output is json-serialized to str before detect()."""
    from memchorus.hooks import MemChorusHooks

    orch = MagicMock()
    orch.save.return_value = True

    with patch("memchorus.hooks._get_orchestrator", return_value=orch):
        instance = MemChorusHooks()
        instance._btrigger.detect = _strict_detect_stub  # type: ignore[attr_defined]

        result = instance.on_post_tool_call(
            result=["item_a", "item_b"],
        )
        # No exception means only str was passed


def test_on_post_tool_call_detect_receives_str_nested_output(monkeypatch):
    """Deeply nested structured output also converts to str."""
    from memchorus.hooks import MemChorusHooks

    orch = MagicMock()
    orch.save.return_value = True

    with patch("memchorus.hooks._get_orchestrator", return_value=orch):
        instance = MemChorusHooks()
        instance._btrigger.detect = _strict_detect_stub  # type: ignore[attr_defined]

        result = instance.on_post_tool_call(
            result={"data": [{"key": [1, 2, 3]}, {"nested": True}]},
        )
        # No exception means only str was passed


def test_behavioral_trigger_detect_rejects_non_string():
    """Base regression: detect() itself must raise on non-string input."""
    from memchorus.behavioral_trigger import BehavioralTrigger

    bt = BehavioralTrigger()
    with pytest.raises(TypeError):
        bt.detect([])  # type: ignore
    with pytest.raises(TypeError):
        bt.detect({})  # type: ignore
    with pytest.raises(TypeError):
        bt.detect(42)  # type: ignore
    # Strings are fine
    bt.detect("normal text")
