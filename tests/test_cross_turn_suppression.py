"""
Tests for the cross-turn injection suppression window (GH-141).

Recall previously re-injected the same entries (unchanged content) on
consecutive turns, wasting prompt budget. GH-141 adds a bounded LRU + TTL
window cache of recently-injected (key, content_hash) pairs per Hermes
profile. When a key+content pair reappears within the window, the block
emits ONE compact marker line instead of the full body; changed content or
TTL expiry restores the full body.

Acceptance criteria (per Kanban task t_584bc5c6 / IMPL #141):
  AC-1: same key + unchanged content rendered in turn N is NOT re-rendered
        full in turn N+1 (marker/suppression instead).
  AC-2: changed content hash (or TTL expiry) renders the full body again.
  AC-3: the window is bounded (memory-safe) and per-profile (not shared).
  AC-4: render block twice with identical key/content ⇒ 2nd render is the
        marker, not the body; different content ⇒ full body.
  AC-5: config surface — window_size + ttl_seconds readable per profile,
        with sane clamp bounds.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import memchorus.hooks as h  # noqa: E402


def _clear():
    h._clear_suppression_windows()


def _render(items):
    return h._format_context_block(items)


class TestCrossTurnSuppression(unittest.TestCase):
    def setUp(self):
        _clear()
        self._profile_patch = mock.patch.dict(
            os.environ, {"HERMES_PROFILE": "gh141-profile"}
        )
        self._profile_patch.start()
        # Neutralise env overrides so defaults drive behaviour unless a test sets one.
        for var in ("MEMCHORUS_SUPPRESSION_WINDOW", "MEMCHORUS_SUPPRESSION_TTL",
                    "MEMCHORUS_RECALL_MAX_CHARS"):
            os.environ.pop(var, None)

    def tearDown(self):
        self._profile_patch.stop()
        _clear()

    def test_ac1_unchanged_content_is_suppressed_next_turn(self):
        items = [{"key": "alpha", "content": "hello world", "score": 0.9}]
        r1 = _render(items)
        self.assertIn("hello world", r1)
        r2 = _render(items)
        # Full body must NOT re-appear; marker must.
        self.assertNotIn("hello world", r2)
        self.assertIn(h._SUPPRESSION_MARKER, r2)
        self.assertIn("alpha", r2)

    def test_ac2_changed_content_renders_full_body(self):
        items_v1 = [{"key": "alpha", "content": "old body", "score": 0.9}]
        items_v2 = [{"key": "alpha", "content": "NEW body text", "score": 0.9}]
        _render(items_v1)
        r2 = _render(items_v2)
        self.assertIn("NEW body text", r2)
        self.assertNotIn(h._SUPPRESSION_MARKER, r2)

    def test_ac2_ttl_expiry_renders_full_body(self):
        # Use a tiny TTL so a simulated time step exceeds it.
        os.environ["MEMCHORUS_SUPPRESSION_TTL"] = "0.1"
        _clear()
        import time as _t
        items = [{"key": "alpha", "content": "ttl body", "score": 0.9}]
        _render(items)
        window = h._get_suppression_window()
        # Age the entry past TTL by stamping it old.
        key = "alpha"
        rec = window._entries[key]
        window._entries[key] = (rec[0], _t.time() - 1.0)
        r2 = _render(items)
        self.assertIn("ttl body", r2)
        self.assertNotIn(h._SUPPRESSION_MARKER, r2)

    def test_ac3b_window_is_bounded(self):
        # A bounded window must drop the oldest entry once capacity exceeded.
        win = h._SuppressionWindow(window_size=3, ttl_seconds=100.0)
        for i in range(5):
            win.mark(f"k{i}", f"h{i}")
        self.assertEqual(len(win._entries), 3)
        # The two oldest (k0, k1) must be evicted (LRU).
        self.assertNotIn("k0", win._entries)
        self.assertNotIn("k1", win._entries)
        self.assertIn("k2", win._entries)
        self.assertIn("k3", win._entries)
        self.assertIn("k4", win._entries)

    def test_ac3c_window_is_per_profile(self):
        # Render under profile A.
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "profile-a"}):
            _render([{"key": "alpha", "content": "A body", "score": 0.9}])
        _clear()  # (clear between to isolate the cross-profile assertion)
        # Re-render under a fresh profile with the same key+content — must NOT
        # be suppressed (different namespace).
        with mock.patch.dict(os.environ, {"HERMES_PROFILE": "profile-b"}):
            r = _render([{"key": "alpha", "content": "B body", "score": 0.9}])
        self.assertIn("B body", r)
        self.assertNotIn(h._SUPPRESSION_MARKER, r)

    def test_ac4_render_twice_identical_then_different(self):
        items = [{"key": "alpha", "content": "same content here", "score": 0.9}]
        r1 = _render(items)
        r2 = _render(items)
        r3 = _render([{"key": "alpha", "content": "completely different", "score": 0.9}])
        self.assertIn("same content here", r1)
        self.assertNotIn("same content here", r2)
        self.assertIn(h._SUPPRESSION_MARKER, r2)
        self.assertIn("completely different", r3)
        self.assertNotIn(h._SUPPRESSION_MARKER, r3)

    def test_ac5_window_size_clamped_to_max(self):
        win = h._SuppressionWindow(window_size=10_000, ttl_seconds=60.0)
        self.assertEqual(win._window_size, h._MAX_WINDOW_SIZE)

    def test_ac5_ttl_clamped_to_zero_min(self):
        win = h._SuppressionWindow(window_size=5, ttl_seconds=-5.0)
        self.assertEqual(win._ttl_seconds, 0.0)

    def test_different_key_not_suppressed(self):
        a = _render([{"key": "alpha", "content": "X", "score": 0.9}])
        b = _render([{"key": "beta", "content": "X", "score": 0.9}])
        self.assertIn("X", a)
        self.assertIn("X", b)  # different key, same body — no false suppression

    def test_budget_drop_does_not_mark(self):
        # An entry dropped by the char budget must NOT be marked in the
        # window — re-rendering it later should show the full body.
        win = h._SuppressionWindow(window_size=100, ttl_seconds=100.0)
        h._suppression_windows["gh141-profile"] = win
        # Tiny block budget: the big entry gets dropped, the small one survives.
        big_body = "B" * 200
        with mock.patch.object(h, "_resolve_char_limit", return_value=90):
            items = [
                {"key": "biggy", "content": big_body, "score": 0.7},
                {"key": "small", "content": "x", "score": 0.9},
            ]
            _render(items)
        # "biggy" was dropped — must NOT be marked. "small" rendered — marked.
        self.assertNotIn("biggy", win._entries, "dropped entry must not be marked")
        self.assertIn("small", win._entries, "rendered entry should be marked")
        # Re-render the big entry with no budget pressure: full body appears.
        full = _render([{"key": "biggy", "content": big_body, "score": 0.9}])
        self.assertIn(big_body, full)

    def test_structured_payload_suppressed_after_first_render(self):
        # #143 payloads: {"text": ...} should unwrap and suppress on re-render.
        items = [{"key": "alpha", "score": 0.9,
                  "content": {"text": "structured body"}}]
        r1 = _render(items)
        r2 = _render(items)
        self.assertIn("structured body", r1)
        self.assertNotIn("structured body", r2)
        self.assertIn(h._SUPPRESSION_MARKER, r2)


if __name__ == "__main__":
    unittest.main()
