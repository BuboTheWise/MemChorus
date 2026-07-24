"""Tests for line-boundary truncation and priority sorting in _format_context_block.

Validates GAP026-B fix: no mid-line cuts, feedback corrections prioritized,
block ceiling drops complete entries rather than cutting partial lines.
"""
import pytest
from memchorus.hooks import (
    _format_context_block,
    _has_feedback_priority,
    _MAX_CONTENT_CHARS,
    _MAX_BLOCK_CHARS,
)


class TestFeedbackPriorityHelper:
    """Verify _has_feedback_priority identifies feedback items correctly."""

    def test_feedback_in_key_returns_true(self):
        item = {"key": "feedback-correction-001", "content": "..."}
        assert _has_feedback_priority(item) is True

    def test_correction_in_key_returns_true(self):
        item = {"key": "my_corrections_v2", "content": "..."}
        assert _has_feedback_priority(item) is True

    def test_is_feedback_flag(self):
        item = {"key": "something_else", "content": "...", "_is_feedback": True}
        assert _has_feedback_priority(item) is True

    def test_normal_item_returns_false(self):
        item = {"key": "auto_result_abc123", "content": "..."}
        assert _has_feedback_priority(item) is False

    def test_empty_dict_returns_false(self):
        item: dict = {}
        assert _has_feedback_priority(item) is False


class TestLineBoundaryTruncation:
    """Per-entry truncation must respect line boundaries."""

    def test_multiline_content_keeps_complete_lines(self):
        """Multi-line content truncated at _MAX_CONTENT_CHARS boundary.

        Each content line averages 50 chars, so roughly 6 complete lines fit.
        The key point is the LAST kept chunk ends on a newline boundary, not mid-line.
        """
        # Create exactly-N-char-per-line content to control boundary precisely.
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


class TestFeedbackPrioritySorting:
    """Feedback items should appear before normal recall when budget is tight."""

    def test_feedback_before_normal(self):
        """Feedback corrections are sorted before general recall content."""
        items = [
            {"key": "normal_recall_1", "content": "Regular memory entry 1"},
            {"key": "feedback_fix_v2", "content": "Correct the previous output"},
            {"key": "normal_recall_2", "content": "Regular memory entry 2"},
        ]
        result = _format_context_block(items)

        feedback_pos = result.index("feedback_fix_v2")
        normal1_pos = result.index("normal_recall_1")
        assert feedback_pos < normal1_pos, "Feedback should appear before normal recall"

    def test_feedback_preserved_when_budget_drops_entries(self):
        """When block ceiling forces removal, normal items drop first."""
        # Create enough large items to exceed the block budget.
        big_content = "x" * 300 + "\n"  # each hits per-entry truncation

        items = [
            {"key": "feedback_keep", "content": "Critical correction info"},
            {"key": "normal_drop_1", "content": big_content},
            {"key": "normal_drop_2", "content": big_content},
            {"key": "normal_drop_3", "content": big_content},
            {"key": "normal_drop_4", "content": big_content},
        ]
        result = _format_context_block(items)

        assert "feedback_keep" in result, "Feedback item must survive budget pressure"


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
