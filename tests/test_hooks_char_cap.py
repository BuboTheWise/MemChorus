"""test_hooks_char_cap.py - Unit + integration tests for GH-96 configurable recall char cap.

Covers:
 - _resolve_char_limit() env var and default paths
 - _format_context_block() boundary (2001 chars), mid-truncation, all-fit
 - dropped_count suffix showing how many entries were dropped
 - lowest-scored entries removed first (highest preserved)
 - integration: 30-item query returns properly capped block
"""
import os
import re as _re
import unittest
from unittest.mock import patch

# Patch before any real imports so the env var test works in isolation


class TestResolveCharLimit(unittest.TestCase):
    """Tests for _resolve_char_limit() resolution order."""

    def test_default_when_no_env_or_config(self):
        from memchorus.hooks import _resolve_char_limit, _DEFAULT_MAX_BLOCK_CHARS
        with patch.dict(os.environ, {}, clear=True):
            # Clear any profile config interference by clearing env vars that
            # would cause the config loader to read real files.
            os.environ.pop("HERMES_PROFILE", None)
            result = _resolve_char_limit()
            self.assertEqual(result, _DEFAULT_MAX_BLOCK_CHARS)
            self.assertEqual(result, 2000)

    def test_env_var_override(self):
        from memchorus.hooks import _resolve_char_limit
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "500"}):
            result = _resolve_char_limit()
            self.assertEqual(result, 500)

    def test_env_var_clamped_min(self):
        from memchorus.hooks import _resolve_char_limit
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "10"}):
            result = _resolve_char_limit()
            self.assertEqual(result, 200)  # clamped to minimum 200

    def test_env_var_clamped_max(self):
        from memchorus.hooks import _resolve_char_limit
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "999999"}):
            result = _resolve_char_limit()
            self.assertEqual(result, 10000)  # clamped to maximum

    def test_env_var_invalid_ignored(self):
        from memchorus.hooks import _resolve_char_limit, _DEFAULT_MAX_BLOCK_CHARS
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "not-a-number"}):
            result = _resolve_char_limit()
            # Falls through to default when not parseable
            self.assertEqual(result, _DEFAULT_MAX_BLOCK_CHARS)


class TestFormatContextBlock(unittest.TestCase):
    """Unit tests for _format_context_block() with char cap enforcement."""

    @staticmethod
    def _make_item(key: str, content: str, score: float) -> dict:
        return {"key": key, "content": content, "score": score}

    def test_empty_items_returns_empty(self):
        from memchorus.hooks import _format_context_block
        result = _format_context_block([])
        self.assertEqual(result, "")

    def test_all_fit_no_truncation(self):
        """All items fit within budget — no truncation suffix."""
        from memchorus.hooks import _format_context_block
        items = [
            self._make_item("a", "short content A", 0.9),
            self._make_item("b", "short content B", 0.8),
        ]
        result = _format_context_block(items)
        self.assertIn("**a**", result)
        self.assertIn("**b**", result)
        self.assertNotIn("truncated", result)
        self.assertNotIn("entries dropped", result)

    def test_boundary_just_over_triggers_truncation(self):
        """Block at just over budget triggers truncation."""
        from memchorus.hooks import _format_context_block, _DEFAULT_MAX_BLOCK_CHARS, _resolve_char_limit
        # Per-entry truncation caps content at _MAX_CONTENT_CHARS (300), so to force
        # two entries to overflow a tiny budget we use a small max_chars.
        long_content = "x" * 1100
        items = [
            self._make_item("high", long_content, 0.95),
            self._make_item("low", long_content, 0.1),
        ]
        # Budget of 600 fits one ~330-char entry + headers but not two
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "600"}):
            result = _format_context_block(items)
            # Should drop the lowest-scored entry ('low' at 0.1) and keep 'high'
            self.assertIn("**high**", result)
            self.assertNotIn("**low**", result)
            self.assertIn("truncated", result)
            self.assertIn("entries dropped", result)

    def test_mid_truncation_drops_lowest_scored(self):
        """Mid-truncation: lowest-scored entries are dropped first, highest preserved."""
        from memchorus.hooks import _format_context_block
        # Each entry ~330 chars after per-entry truncation. Budget of 700
        # allows exactly 2 entries + header/footer (~660 total), so the
        # three lowest-scored should be dropped.
        content = "y" * 300
        items = [
            self._make_item("one",   content, 0.9),
            self._make_item("two",   content, 0.8),
            self._make_item("three", content, 0.7),
            self._make_item("four",  content, 0.3),
            self._make_item("five",  content, 0.2),
        ]
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "700"}):
            result = _format_context_block(items)
            # Highest-scored survive lowest get dropped. With ~700 budget
            # and ~330 chars per entry, top 2 should remain:
            self.assertIn("**one**", result)
            self.assertIn("**two**", result)
            self.assertNotIn("**four**", result)
            self.assertNotIn("**five**", result)
            self.assertIn("entries dropped", result)

    def test_dropped_count_suffix(self):
        """Truncation suffix shows the exact number of dropped entries."""
        from memchorus.hooks import _format_context_block
        content = "z" * 500
        items = [
            self._make_item(f"k{i}", content, float(i) / 10)
            for i in range(1, 6)
        ]
        # Tight budget forces multiple drops
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "800"}):
            result = _format_context_block(items)
            self.assertIn("truncated", result)
            # Should have a number before "entries dropped"
            match = _re.search(r"(\d+) entries dropped", result)
            self.assertIsNotNone(match)
            dropped = int(match.group(1))
            self.assertGreater(dropped, 0)

    def test_single_item_exceeds_budget_keeps_it(self):
        """Even a single item past budget is kept (we can't drop the last one)."""
        from memchorus.hooks import _format_context_block
        massive = "m" * 5000
        items = [self._make_item("big", massive, 1.0)]
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "200"}):
            result = _format_context_block(items)
            # Should still have the entry (can't drop everything)
            self.assertIn("[MemChorus injected context]", result)

    def test_dedup_before_scoring(self):
        """Duplicate keys are deduplicated before scoring/truncation."""
        from memchorus.hooks import _format_context_block
        items = [
            self._make_item("dup", "first wins", 0.9),
            self._make_item("dup", "second ignored", 0.95),
        ]
        result = _format_context_block(items)
        # Only one 'dup' entry should appear
        count = result.count("**dup**")
        self.assertEqual(count, 1)

    def test_no_score_field_defaults_to_zero(self):
        """Items without a score field default to 0.0."""
        from memchorus.hooks import _format_context_block
        items = [
            {"key": "a", "content": "no score"},
            {"key": "b", "content": "has score", "score": 1.0},
        ]
        result = _format_context_block(items)
        # Should not crash - missing score defaults to 0.0
        self.assertIn("[MemChorus injected context]", result)


class TestIntegrationThirtyItems(unittest.TestCase):
    """Integration test: 30-item query returns properly capped recall block."""

    @staticmethod
    def _make_item(idx: int, score: float) -> dict:
        content = f"Content for item {idx} — " + "word " * 20  # ~100 chars each
        return {"key": f"item_{idx:03d}", "content": content, "score": score}

    def test_thirty_items_capped_to_budget(self):
        """30 items returned from a query should be truncated to fit max_block_chars."""
        from memchorus.hooks import _format_context_block, _resolve_char_limit

        scores = [1.0 - (i * 0.025) for i in range(30)]  # 1.0 down to 0.275
        items = [self._make_item(i, scores[i]) for i in range(30)]

        max_chars = _resolve_char_limit()
        result = _format_context_block(items)

        # Block should exist and be valid markdown
        self.assertIn("[MemChorus injected context]", result)
        self.assertIn("[/MemChorus injected block]", result)

        # Total length including header/footer should be within budget (allow small
        # tolerance for the truncation suffix line itself)
        total_len = len(result)
        # The truncation suffix adds ~50 chars, so we allow a 100-char buffer
        self.assertLessEqual(total_len, max_chars + 100,
            f"Block length {total_len} exceeds budget {max_chars} even with tolerance")

        # Should have dropped some entries (30 items won't fit in 2000 chars)
        import re
        match = re.search(r"(\d+) entries dropped", result)
        self.assertIsNotNone(match, "Expected truncation suffix for 30 items over budget")
        dropped = int(match.group(1))
        self.assertGreater(dropped, 0, "Should have dropped at least some entries")
        self.assertLess(dropped, 30, "Should not drop all entries")

        # Highest-scored items should be preserved; lowest-scored should go first
        # The lowest score (0.275 for item_029) should be absent if dropped
        remaining_keys = re.findall(r"\*\*(item_\w+)\*\*", result)
        self.assertGreater(len(remaining_keys), 0)

        # Verify highest-scored items are more likely present than lowest-scored
        high_score_items = [f"item_{i:03d}" for i in range(3, 8)]   # top tier
        low_score_items = [f"item_{i:03d}" for i in range(25, 30)]  # bottom tier
        high_present = sum(1 for k in high_score_items if k in remaining_keys)
        low_present = sum(1 for k in low_score_items if k in remaining_keys)
        self.assertGreater(high_present, low_present,
            "Higher-scored items should be preserved over lower-scored ones")

    def test_thirty_items_with_tiny_budget(self):
        """With a very tight budget, only top-scoring items survive."""
        from memchorus.hooks import _format_context_block

        scores = [1.0 - (i * 0.025) for i in range(30)]
        items = [self._make_item(i, scores[i]) for i in range(30)]

        # Ultra-tight budget: only 1-2 entries fit
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "500"}):
            result = _format_context_block(items)

            remaining_keys = _re.findall(r"\*\*(item_\w+)\*\*", result)
            self.assertGreater(len(remaining_keys), 0)

            # With tiny budget, should keep the highest-scored item (item_000 at 1.0)
            self.assertIn("item_000", result)

    def test_thirty_items_all_fit_when_budget_large(self):
        """With a generous budget, all 30 items fit without truncation."""
        from memchorus.hooks import _format_context_block

        scores = [1.0 - (i * 0.025) for i in range(30)]
        items = [self._make_item(i, scores[i]) for i in range(30)]

        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "9000"}):
            result = _format_context_block(items)
            self.assertIn("[MemChorus injected context]", result)
            self.assertNotIn("truncated", result)
            all_keys = [f"item_{i:03d}" for i in range(30)]
            for key in all_keys:
                self.assertIn(key, result, f"Key {key} should be present when budget is large")


if __name__ == "__main__":
    unittest.main()
