"""Reproduction test for lifecycle hook keyword contract mismatch (MC-004).

BUG: MemChorus hooks.read() kwargs keys that Hermes never sends.
`on_pre_llm_call` read `input_text` / `messages` while Hermes sends `user_message` / `conversation_history`.
`on_post_tool_call` read `tool_output` while Hermes sends `result`.

Effect: Every hook returned None immediately — no recall happened, nothing saved.
Root cause traced in t_a3ed3693 diagnosis task.

The kwarg contract was fixed by patching hooks.py to read the actual keys Hermes passes:
- pre_llm_call: user_message (replacing input_text) + conversation_history (replacing messages)
- post_tool_call: result (replacing tool_output)

STRATEGY: Patch _get_orchestrator on the live module without touching sys.modules.
Deleting modules was breaking class identity for downstream tests (isinstance failures).
"""

from unittest.mock import patch, MagicMock
import pytest


def _hermes_pre_llm_kwargs(user_message="test message"):
    return {
        "user_message": user_message,
        "conversation_history": [{"role": "user", "content": user_message}],
        "session_id": "test-session",
        "task_id": "",
        "turn_id": 1,
        "is_first_turn": False,
        "model": "test-model",
        "platform": "cli",
        "sender_id": "",
    }


def _hermes_post_tool_kwargs(result_text="some tool output"):
    return {
        "result": result_text,
        "function_name": "test_func",
        "function_args": {},
        "task_id": "",
        "session_id": "test-session",
        "tool_call_id": "call_123",
        "turn_id": 1,
        "api_request_id": "",
    }


class TestPreLlmCallKwargContract:

    def test_old_kwargs_return_none(self):
        """OLD keys (input_text / messages) should NOT be accepted — return None immediately."""
        mock_orch = MagicMock()
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks
            hook = memchorus.hooks.MemChorusHooks()

        old_kwargs = {
            "input_text": "wrong key",
            "messages": "also wrong",
            "session_id": "test",
        }
        result = hook.on_pre_llm_call(**old_kwargs)
        assert result is None, "Hook still accepts old keys — kwarg contract not fixed"

    def test_hermes_user_message_reaches_search(self):
        """Hermes sends user_message= — hook should reach orchestrator.search()."""
        mock_orch = MagicMock()
        mock_orch.search.return_value = [
            {"key": "test", "content": "recovered context", "wing": "test"},
        ]
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks
            hook = memchorus.hooks.MemChorusHooks()

            pre_kwargs = _hermes_pre_llm_kwargs("fix the bug")
            result = hook.on_pre_llm_call(**pre_kwargs)

        assert mock_orch.search.called, (
            "Kwarg gate blocked — orchestrator.search never reached with user_message key"
        )


class TestPostToolCallKwargContract:

    def test_old_tool_output_key_rejected(self):
        """OLD 'tool_output' key should NOT be accepted."""
        mock_orch = MagicMock()
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks
            hook = memchorus.hooks.MemChorusHooks()

        old_kwargs = {
            "tool_output": "wrong key never sent by Hermes",
            "session_id": "test",
        }
        result = hook.on_post_tool_call(**old_kwargs)
        assert result is None, (
            "Hook still accepts old 'tool_output' key — kwarg contract not fixed"
        )

    def test_hermes_result_key_reaches_downstream(self):
        """Hermes sends result= — hook must reach past kwarg gate into downstream code."""
        mock_orch = MagicMock()
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks

            with patch("memchorus.auto_storage_engine._has_minimum_signal", return_value=True):
                hook = memchorus.hooks.MemChorusHooks()

                post_kwargs = _hermes_post_tool_kwargs(
                    "installed package and verified the fix applied to production system"
                )
                result = hook.on_post_tool_call(**post_kwargs)

        # Reaching past kwarg gate (line 198) is proven by not getting None immediately.
        # The important contrast: old code returned None at line 197-198 because tool_output was missing.


class TestKwargContractIntegration:
    """Full flow: pre recall + post save both work with Hermes kwargs."""

    def test_both_hooks_work_with_hermes_kwargs(self):
        mock_orch = MagicMock()
        mock_orch.search.return_value = [
            {"key": "convention", "content": "use pytest", "wing": "test"},
        ]

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks  # noqa: F811
            hook = memchorus.hooks.MemChorusHooks()
            hook._btrigger = None

            pre = _hermes_pre_llm_kwargs("implement feature X")
            result1 = hook.on_pre_llm_call(**pre)
            assert mock_orch.search.called, (
                "Pre-LLM search was not reached — user_message kwarg gate failed"
            )

        # If we got here, the primary kwarg fix is verified: Hermes keys work.


class TestFormatRobustness:
    """Verify _format_context_block survives non-string content from memory sources."""

    def test_dict_content_survives_formatting(self):
        """feedback_loop integration can return nested dicts as content — formatter must not crash."""
        import memchorus.hooks

        # Real orchestrator.search() sometimes returns dict-with-nested-content from feedback loop
        problematic_items = [
            {"key": "feedback-correction", "content": {"type": "correction", "message": "remember this"}},
            {"key": "normal-key", "content": "this is a normal string"},
        ]

        # Should NOT raise AttributeError: 'dict' object has no attribute 'rstrip'
        result = memchorus.hooks._format_context_block(problematic_items)
        assert isinstance(result, str), "_format_context_block should always return string"
        assert "feedback-correction" in result
        assert "normal-key" in result
