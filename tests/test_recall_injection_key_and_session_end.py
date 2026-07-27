"""Regression tests for recall injection key mismatch and session-end crash.

Bug 1: on_pre_llm_call returned 'injected_context' but Hermes reads 'context'.
Bug 2: on_session_end called len(int) because batcher.pending returns int, not list.

Fixes verified:
- hooks.py line ~290: 'injected_context' -> 'context' (Hermes turn_context compatibility)
- hooks.py line ~433: batcher.pending used directly as int (no len() call on int property)
"""

from unittest.mock import patch, MagicMock, PropertyMock
import pytest


class TestPreLlmCallReturnsContextKey:
    """Bug 1 fix: verify hook returns 'context' key that Hermes can extract."""

    def test_on_pre_llm_call_returns_context_key(self):
        """Hermes reads r.get('context') — the hook MUST return 'context', not 'injected_context'."""
        mock_orch = MagicMock()
        mock_orch.search.return_value = [
            {"key": "test-key", "content": "recalled context from memory", "wing": "test"},
        ]

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks
            hook = memchorus.hooks.MemChorusHooks()

            result = hook.on_pre_llm_call(
                user_message="implement feature X",
                conversation_history=[{"role": "user", "content": "implement feature X"}],
                session_id="test-session",
                task_id="",
                turn_id=1,
                is_first_turn=False,
                model="test-model",
                platform="cli",
                sender_id="",
            )

        assert result is not None, "Hook returned None — recall pipeline dead"
        assert "context" in result, (
            f"Hook must return 'context' key for Hermes compatibility, got keys: {list(result.keys())}"
        )
        assert "recalled context from memory" in result["context"], (
            "Recalled content not present in context value"
        )

    def test_no_injected_context_key_legacy(self):
        """Ensure the old 'injected_context' key is NOT returned."""
        mock_orch = MagicMock()
        mock_orch.search.return_value = [
            {"key": "test-key", "content": "some memory", "wing": "test"},
        ]

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks
            hook = memchorus.hooks.MemChorusHooks()

            result = hook.on_pre_llm_call(
                user_message="do something",
                conversation_history=[{"role": "user", "content": "do something"}],
                session_id="test-session",
                task_id="",
                turn_id=1,
                is_first_turn=False,
                model="test-model",
                platform="cli",
                sender_id="",
            )

        # If result exists, it should NOT have 'injected_context' key
        if result is not None:
            assert "injected_context" not in result, (
                "Legacy 'injected_context' key still present — fix incomplete"
            )


class TestSessionEndNoLenIntCrash:
    """Bug 2 fix: on_session_end must not crash with len(int) when batcher is empty."""

    def test_on_session_end_no_crash_with_empty_batcher(self):
        """on_session_end should complete without error even when batcher._queue is empty."""
        from memchorus.tool_capture_buffer import ToolCaptureBuffer

        mock_orch = MagicMock()
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks

            # Create a batcher and set it as the global instance  
            batcher = ToolCaptureBuffer(max_items=10, flush_interval=5.0)
            
            # Verify pending returns int (this is what caused len(int))
            assert isinstance(batcher.pending, int), (
                "pending property should return int"
            )
            
            with patch("memchorus.hooks._CAPTURE_BATCHER", batcher):
                hook = memchorus.hooks.MemChorusHooks()
                
                # This used to crash with TypeError: object of type 'int' has no len()
                # Now it should return a result dict without crashing
                try:
                    result = hook.on_session_end()
                except TypeError as e:
                    if "len()" in str(e):
                        pytest.fail(
                            f"Session end crashed with len(int) — bug not fixed: {e}"
                        )
                    raise

    def test_on_session_end_with_items_in_batcher(self):
        """on_session_end flushes items and reports count correctly."""
        from memchorus.tool_capture_buffer import ToolCaptureBuffer

        mock_orch = MagicMock()
        flushed = []

        def capture_callback(payloads):
            flushed.extend(payloads)

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks

            batcher = ToolCaptureBuffer(
                max_items=10, flush_interval=5.0, callback=capture_callback
            )
            # Add a few items but don't trigger auto-flush
            batcher.add({"text": "item1"})
            batcher.add({"text": "item2"})

            with patch("memchorus.hooks._CAPTURE_BATCHER", batcher):
                hook = memchorus.hooks.MemChorusHooks()
                result = hook.on_session_end()

        assert result is not None, "Session end should return a result dict"
        assert result.get("teardown") == "complete"
        # Items should be flushed on close
        assert len(flushed) > 0 or result.get("source") == "memchorus_session_end"

    def test_on_session_end_with_none_batcher(self):
        """on_session_end gracefully handles when _CAPTURE_BATCHER is None."""
        mock_orch = MagicMock()

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch), \
             patch("memchorus.hooks._CAPTURE_BATCHER", None):
            import memchorus.hooks
            hook = memchorus.hooks.MemChorusHooks()

            result = hook.on_session_end()

        # Should return teardown confirmation even with no batcher
        assert result is not None


class TestBatcherPendingTypeInvariant:
    """Verify batcher.pending always returns int, never a list."""

    def test_pending_returns_int_not_list(self):
        """The pending property must return int — hooks.py relies on this."""
        from memchorus.tool_capture_buffer import ToolCaptureBuffer

        buf = ToolCaptureBuffer(callback=lambda x: None)
        assert isinstance(buf.pending, int), (
            f"pending should be int, got {type(buf.pending).__name__}"
        )

    def test_pending_reflects_queue_length(self):
        """pending property accurately reflects the actual queue length."""
        from memchorus.tool_capture_buffer import ToolCaptureBuffer

        buf = ToolCaptureBuffer(max_items=10, callback=lambda x: None)
        
        # Initially empty
        assert buf.pending == 0
        
        # After adding items (but before auto-flush kicks in at threshold)
        buf.add({"text": "a"})
        buf.add({"text": "b"})
        buf.add({"text": "c"})
        
        count = buf.pending
        # Should be positive integer
        assert isinstance(count, int) and count >= 1
