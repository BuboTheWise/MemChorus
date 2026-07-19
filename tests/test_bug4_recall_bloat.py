"""Tests for recall bloat fix (MAX_CONTENT_CHARS truncation) and duplicate guard removal."""
import inspect
from memchorus.hooks import _format_context_block


class TestRecallTruncation:
    """Prove that _format_context_block truncates oversized content."""

    def test_long_content_is_truncated(self):
        blob = "x" * 10_000 + " distinctive end marker"
        items = [{"key": "big_item", "content": blob}]
        result = _format_context_block(items)
        assert "distinctive end marker" not in result  # truncated before that far
        assert "..." in result  # ellipsis present after truncation

    def test_short_content_passes_through(self):
        short = "this is a perfectly fine memory entry"
        items = [{"key": "small", "content": short}]
        result = _format_context_block(items)
        assert short in result
        assert "..." not in result  # no truncation needed

    def test_boundary_at_300_chars_exact(self):
        exact = "a" * 300  # exactly at the limit
        items = [{"key": "at_limit", "content": exact}]
        result = _format_context_block(items)
        assert exact in result
        assert "..." not in result  # 300 chars passes without truncation

    def test_just_over_boundary_truncates(self):
        over = "b" * 301  # one char over the limit
        items = [{"key": "over_limit", "content": over}]
        result = _format_context_block(items)
        assert "..." in result  # truncated

    def test_empty_content_no_crash(self):
        items = [{"key": "nada", "content": ""}]
        result = _format_context_block(items)
        assert "**nada**" in result

    def test_none_content_no_crash(self):
        items = [{"key": "null", "content": None}]
        result = _format_context_block(items)
        assert "**null**" in result


class TestDuplicateGuardRemoved:
    """Prove the duplicate _is_query_echo import was removed from on_post_tool_call."""

    def test_query_echo_import_appears_once(self):
        source = inspect.getsource(__import__("memchorus.hooks", fromlist=["MemChorusHooks"]).MemChorusHooks.on_post_tool_call)
        count = source.count("_is_query_echo")
        # Appears once as import, once as call — total should be exactly 2
        assert count == 2, f"Expected _is_query_echo to appear exactly twice (import + call), got {count}"

    def test_query_echo_if_block_appears_once(self):
        source = inspect.getsource(__import__("memchorus.hooks", fromlist=["MemChorusHooks"]).MemChorusHooks.on_post_tool_call)
        count = source.count("if _is_query_echo")
        assert count == 1, f"Expected exactly one 'if _is_query_echo' block, got {count}"
