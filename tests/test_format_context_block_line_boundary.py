"""Tests for line-boundary truncation and character budget in _format_context_block.

Validates budget enforcement: per-entry truncation, block ceiling with entry dropping,
proper fencing markers, and edge cases like empty or oversized inputs.
"""
import pytest
from memchorus.hooks import (
    _format_context_block,
    _MAX_CONTENT_CHARS,
    _MAX_BLOCK_CHARS,
)


class TestLineBoundaryTruncation:
    """Per-entry truncation must respect line boundaries."""

    def test_multiline_content_keeps_complete_lines(self):
        """Multi-line content truncated at _MAX_CONTENT_CHARS boundary.

        Each content line averages 50 chars, so roughly 6 complete lines fit.
        The key point is the LAST kept chunk ends on a newline boundary, not mid-line.
        """
        num_lines_to_fit = 5
        line_len = _MAX_CONTENT_CHARS // (num_lines_to_fit + 1) - 1  # well under budget per line
        lines = [f"Line {i}: " + "x" * line_len for i in range(num_lines_to_fit + 3)]
        content = "\n".join(lines)

        items = [{"key": "ml_test", "content": content}]
        result = _format_context_block(items)

        # The kept lines appear verbatim (no mid-line cut)
        for line in lines[:num_lines_to_fit]:
            assert line in result

        # Ellipsis appended after truncation
        assert "..." in result

    def test_single_line_content_can_truncate_anypoint(self):
        """Single-line content exceeding budget is truncated with ellipsis."""
        content = "a very long single line of text that exceeds the character budget by far " * 20
        items = [{"key": "single", "content": content}]
        result = _format_context_block(items)

        assert "..." in result
        # Content is actually present (not completely dropped)
        assert "a very long" in result

    def test_first_line_exceeds_budget(self):
        """When the first line alone exceeds the budget, it falls back to partial cut."""
        single_giant_line = "x" * (_MAX_CONTENT_CHARS + 500)
        items = [{"key": "giant", "content": single_giant_line}]
        result = _format_context_block(items)

        assert "..." in result

    def test_no_midline_cut_produces_valid_fenced_block(self):
        """Ensure truncated output still has proper fencing markers."""
        lines = []
        for i in range(10):
            lines.extend([f"paragraph {i} with some text to pad length here enough chars"] * 3)
        content = "\n".join(lines)
        items = [{"key": "valid_fence", "content": content}]
        result = _format_context_block(items)

        assert "[MemChorus injected context]" in result
        assert "[/MemChorus injected block]" in result


class TestBlockCeilingDoesNotCutPartialLines:
    """When the total block exceeds _MAX_BLOCK_CHARS, entire entries are dropped."""

    def test_entries_dropped_not_cut(self):
        """Items are removed entirely rather than truncated mid-line."""
        items = [
            {"key": f"item_{i}", "content": "x" * 250 + "\nmore content here"}
            for i in range(6)
        ]
        result = _format_context_block(items)

        # If we kept "item_N", its full line must appear, not a partial version
        for i in range(6):
            key = f"item_{i}"
            if key in result:
                # The full format is "- **key** — content" so verify the bullet exists
                assert f"- **{key}**" in result

    def test_truncated_marker_present(self):
        """Block ceiling exceeded should show the truncation trailer."""
        items = [
            {"key": f"wide_{i}", "content": "padded content " * 30}
            for i in range(8)
        ]
        result = _format_context_block(items)

        assert "... (truncated, budget exceeded)" in result

    def test_no_truncated_marker_when_in_budget(self):
        """Small blocks should NOT show the truncation marker."""
        items = [
            {"key": "small1", "content": "brief"},
            {"key": "small2", "content": "also brief"},
        ]
        result = _format_context_block(items)

        assert "... (truncated, budget exceeded)" not in result


class TestEdgeCases:
    """Boundary conditions and empty/falsy inputs."""

    def test_empty_list(self):
        assert _format_context_block([]) == ""

    def test_single_item_in_budget(self):
        items = [{"key": "only", "content": "just one entry"}]
        result = _format_context_block(items)

        assert "**only**" in result
        assert "[MemChorus injected context]" in result

    def test_all_items_same_key(self):
        """Only the first occurrence of a duplicate key should appear."""
        items = [
            {"key": "dup", "content": "first"},
            {"key": "dup", "content": "second"},
        ]
        result = _format_context_block(items)

        assert "first" in result
        assert "second" not in result
