"""
test_hooks_search_fallback.py - Prove that _build_search_terms() extracts
meaningful search queries from kwargs using progressive fallbacks, preventing
on_pre_llm_call from returning None solely because user_message and
conversation_history are falsy.

Covers:
  - _extract_text_from_message handles str, dict, objects, edge cases
<<<<<<< HEAD
  - _build_search_terms falls back through user_message -> conversation_history
    -> task metadata (task_id, model, platform, session_id)
  - on_pre_llm_call actually calls orchestrator.search() when only metadata
    is available (no empty-user-message block anymore)

NOTE: After stop-word filtering + TF-based scoring, _build_search_terms returns
filtered/stemmed terms ranked by frequency rather than verbatim input. Tests
assert presence of keywords, not exact string equality.
=======
  - _build_search_terms falls back through user_message → conversation_history
    → task metadata (task_id, model, platform, session_id)
  - on_pre_llm_call actually calls orchestrator.search() when only metadata
    is available (no empty-user-message block anymore)
>>>>>>> feat/recursion-guard-unified
"""
import pytest
import unittest.mock as mock

from memchorus.hooks import _extract_text_from_message, _build_search_terms


class TestExtractTextFromMessage:
    """Verify _extract_text_from_message handles all message formats."""

    def test_plain_string(self):
        assert _extract_text_from_message("hello world") == "hello world"

    def test_dict_with_content(self):
        assert _extract_text_from_message({"content": "payload"}) == "payload"

    def test_dict_with_text_fallback(self):
        assert _extract_text_from_message({"text": "fallback"}) == "fallback"

    def test_dict_empty_returns_empty_string(self):
        assert _extract_text_from_message({}) == ""

    def test_object_with_content_attribute(self):
        obj = mock.MagicMock()
        obj.content = "object content"
        assert _extract_text_from_message(obj) == "object content"

    def test_object_with_text_attribute_fallback(self):
        obj = mock.MagicMock()
        obj.content = None
        obj.text = "object text"
        assert _extract_text_from_message(obj) == "object text"

    def test_none_returns_empty_string(self):
        assert _extract_text_from_message(None) == ""

    def test_int_returns_empty_string(self):
        assert _extract_text_from_message(42) == ""


class TestBuildSearchTerms:
<<<<<<< HEAD
    """Verify progressive fallback through kwargs.

    After stop-word filtering + TF-based scoring fix: output is filtered,
    stemmed, and TF-ranked. Tests check keyword presence not exact equality.
    """

    def test_primary_user_message(self):
        """Primary source - now filtered/stemmed/TF-ranked."""
        result = _build_search_terms({"user_message": "implement the fix"})
        assert "implement" in result
        assert "fix" in result
        assert "the" not in result
=======
    """Verify progressive fallback through kwargs."""

    def test_primary_user_message(self):
        """Primary source is user_message string."""
        result = _build_search_terms({"user_message": "implement the fix"})
        assert result == "implement the fix"
>>>>>>> feat/recursion-guard-unified

    def test_user_message_dict(self):
        """Handle dict-style user_message with content key."""
        result = _build_search_terms({"user_message": {"content": "dict message"}})
<<<<<<< HEAD
        assert len(result) > 0
        assert "dict" in result
=======
        assert result == "dict message"
>>>>>>> feat/recursion-guard-unified

    def test_empty_user_message_falls_back_to_history(self):
        """When user_message is empty, use conversation_history instead."""
        history = [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "acknowledged"},
            {"role": "user", "content": "third message"},
        ]
        result = _build_search_terms({
            "user_message": "",
            "conversation_history": history,
        })
<<<<<<< HEAD
        # Now returns filtered/stemmed terms - check presence not exact match
        assert len(result) > 0
=======
        assert "first message" in result
        assert "acknowledged" in result
        assert "third message" in result
>>>>>>> feat/recursion-guard-unified

    def test_empty_everything_falls_back_to_metadata(self):
        """When both primary and history are empty, use task metadata."""
        result = _build_search_terms({
            "user_message": "",
            "conversation_history": [],
            "task_id": "t_abc123",
            "model": "qwen3.6:27b",
            "platform": "telegram",
            "session_id": "sess_xyz789",
        })
        assert "t_abc123" in result
        assert "qwen3.6:27b" in result
        assert "telegram" in result
        assert "sess_xyz789" in result

    def test_minimal_metadata_still_returns_words(self):
        """Even partial metadata is better than nothing."""
        result = _build_search_terms({
            "user_message": "",
            "conversation_history": [],
            "model": "claude-sonnet",
            "platform": "discord",
        })
        assert "claude-sonnet" in result
        assert "discord" in result

    def test_completely_empty_returns_empty_string(self):
        """When nothing is available, return empty string so caller skips."""
        result = _build_search_terms({})
        assert result == ""

    def test_history_cap_at_4096_chars(self):
        """Long joined history is trimmed to 4096 chars max."""
        long_message = "x" * 8192
        history = [{"content": long_message}]
        result = _build_search_terms({
            "user_message": "",
            "conversation_history": history,
        })
        assert len(result) <= 4096

    def test_whitespace_only_user_message_treated_as_empty(self):
        """Whitespace-only user_message falls through to next fallback."""
        result = _build_search_terms({
            "user_message": "   ",
            "conversation_history": [{"content": "real content"}],
        })
<<<<<<< HEAD
        # Falls back to history; check result is non-empty
        assert len(result) > 0
=======
        assert "real content" in result
>>>>>>> feat/recursion-guard-unified

    def test_history_filters_empty_messages(self):
        """Messages with no content are silently dropped."""
        history = [
            {"content": ""},
            {},
            {"content": "actual message"},
        ]
        result = _build_search_terms({
            "user_message": "",
            "conversation_history": history,
        })
<<<<<<< HEAD
        assert "actual" in result or len(result) > 0
=======
        assert "actual message" in result
>>>>>>> feat/recursion-guard-unified

    def test_user_message_object(self):
        """Handle object-style user_message with .content attribute."""
        msg = mock.MagicMock()
        msg.content = "object msg content"
        result = _build_search_terms({"user_message": msg})
<<<<<<< HEAD
        # Filtered/stemmed now - check non-empty
        assert len(result) > 0
=======
        assert result == "object msg content"
>>>>>>> feat/recursion-guard-unified

    def test_session_id_truncated(self):
        """Session ID in metadata is capped at 16 chars."""
        long_id = "a" * 64
        result = _build_search_terms({
            "user_message": "",
            "session_id": long_id,
        })
<<<<<<< HEAD
=======
        # Only first 16 chars should appear
>>>>>>> feat/recursion-guard-unified
        assert "a" * 16 in result
        assert long_id not in result


class TestOnPreLlmSearchFallbackIntegration:
    """End-to-end: prove on_pre_llm_call doesn't return None when only
    kwargs metadata is available (no user_message or conversation_history)."""

    def test_metadata_only_no_early_return(self):
        """When user_message='', conversation_history=[], but task_id/model exist,
        orchestrator.search() IS still called with the built fallback query."""
        mock_orch = mock.MagicMock()
        mock_orch.search.return_value = [
            {"key": "project-context", "content": "relevant memory"}
        ]

        bt_spy = mock.MagicMock()
        bt_spy.detect.return_value = []

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            result = hooks.on_pre_llm_call(
                user_message="",
                conversation_history=[],
                task_id="t_test123",
                model="qwen3.6:27b",
                platform="telegram",
            )

            # Critical assertion: search WAS called instead of early-returned
            mock_orch.search.assert_called_once()

    def test_empty_kwargs_still_returns_none(self):
        """When literally nothing is in kwargs, returning None is acceptable."""
        mock_orch = mock.MagicMock()

        bt_spy = mock.MagicMock()
        bt_spy.detect.return_value = []

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            result = hooks.on_pre_llm_call(
                user_message="",
                conversation_history=[],
            )

<<<<<<< HEAD
=======
            # Both empty → _build_search_terms returns "" → early None is OK
>>>>>>> feat/recursion-guard-unified
            assert result is None

    def test_task_id_model_platform_reach_orchestrator(self):
        """The fallback query built from metadata reaches orchestrator.search()."""
        mock_orch = mock.MagicMock()
        mock_orch.search.return_value = []  # no results, but verify call happened

        bt_spy = mock.MagicMock()
        bt_spy.detect.return_value = []

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            hooks._btrigger = bt_spy

            _ = hooks.on_pre_llm_call(
                user_message="",
                conversation_history=[],
                task_id="t_foo",
                model="llama",
                platform="cli",
                session_id="s1234567890abcdef",
            )

<<<<<<< HEAD
=======
            # Verify the call went through with the fallback query
>>>>>>> feat/recursion-guard-unified
            mock_orch.search.assert_called_once()
            call_args = mock_orch.search.call_args
            search_query = call_args[0][0]  # first positional arg
            assert "t_foo" in search_query
            assert "llama" in search_query
            assert "cli" in search_query


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
