"""test_207_hot_injection.py — Issue #207 acceptance tests.

Verifies the "hot-injection" upgrade of the recall block:

  * A configurable **per-entry threshold** decides inline vs locator+preview
    (default 300, ``null`` = pre-#207 keys-only behaviour).
  * Bodies above the threshold render as *locator line + opening-lines preview*
    (per the "first ~3 lines" spec) — not the full body, and not keys-only.
  * Bodies at or under the threshold render inline in full.
  * The structured report carries a per-entry ``mode`` tag plus a
    ``mode_split`` count so the doctor can print the required
    "N of M entries injected inline, K as key+preview" line (#207 AC4).
  * The threshold is 3-layer (env → profile config → default), ``null``/``""``
    selects the legacy path.
  * Legacy mode (threshold ``None``) keeps the pre-#207 gate: locator injected
    only when body > ``LOCATOR_INJECT_THRESHOLD`` (240), no preview appended.

Covers the acceptance criteria that #140 alone did not satisfy: the "keys
only" line was unusable — the agent saw a pointer with no content to read.
"""
import os
import unittest
from unittest.mock import patch


class _FreshWindow:
    """Minimal stand-in for the cross-turn suppression window.

    Every ``suppressed`` call returns ``False`` so the recall renderer
    never collapses to a marker; ``mark`` is a no-op so the render pass
    doesn't pollute subsequent tests.
    """

    def __init__(self):
        self.marked = []

    def suppressed(self, key, content_hash):  # noqa: D401 - protocol
        return False

    def mark(self, key, content_hash):  # noqa: D401 - protocol
        self.marked.append((key, content_hash))


def _item(key, content, score, locator=None):
    it = {"key": key, "content": content, "score": score}
    if locator is not None:
        it["locator"] = locator
    return it


_LOCALENG = {
    "gist": "gist about a topic",
    "path_or_url": "http://127.0.0.1:4444",
    "key": "LEARNING_deadbeef",
}
_LOCSHORT = {
    "gist": "short note",
    "path_or_url": "http://localhost:11434",
    "key": "KEY_01",
}


class Test207_ResolveInlineThreshold(unittest.TestCase):
    """_resolve_inline_threshold(): 3-layer resolution order (env → config → default)."""

    def test_default_300(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("HERMES_PROFILE", None)
            self.assertEqual(_resolve_inline_threshold(), 300)

    def test_env_int(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": "500"}):
            self.assertEqual(_resolve_inline_threshold(), 500)

    def test_env_zero(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": "0"}):
            self.assertEqual(_resolve_inline_threshold(), 0)

    def test_env_none_means_legacy(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": "none"}):
            self.assertIsNone(_resolve_inline_threshold())

    def test_env_null_lower(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": "null"}):
            self.assertIsNone(_resolve_inline_threshold())

    def test_env_tilde(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": "~"}):
            self.assertIsNone(_resolve_inline_threshold())

    def test_env_empty_string_falls_through_to_default(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": ""}):
            # Empty string is treated as unset → default 300 (not legacy-None).
            self.assertEqual(_resolve_inline_threshold(), 300)

    def test_env_clamped_min(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": "-10"}):
            self.assertEqual(_resolve_inline_threshold(), 0)

    def test_env_clamped_max(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": "999999"}):
            self.assertEqual(_resolve_inline_threshold(), 50000)

    def test_env_invalid_int_falls_through_to_default(self):
        from memchorus.hooks import _resolve_inline_threshold
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_INLINE_THRESHOLD": "abc"}):
            # Invalid int → fall through to config/default (which is 300).
            os.environ.pop("HERMES_PROFILE", None)
            self.assertEqual(_resolve_inline_threshold(), 300)


class Test207_InlineThresholdRendering(unittest.TestCase):
    """simulate_recall_render() honours the threshold and tags each entry's mode."""

    def _render(self, items, env_threshold=None):
        import memchorus.hooks as h
        # Reset cross-turn window so tests are hermetic.
        window = _FreshWindow()
        env = os.environ.copy()
        env.pop("MEMCHORUS_RECALL_MAX_CHARS", None)
        # Large block budget so entry-level decisions (not block eviction) are
        # what we observe.
        env["MEMCHORUS_RECALL_MAX_CHARS"] = "8000"
        if env_threshold is not None:
            env["MEMCHORUS_RECALL_INLINE_THRESHOLD"] = env_threshold
        else:
            env.pop("MEMCHORUS_RECALL_INLINE_THRESHOLD", None)
        os.environ.pop("HERMES_PROFILE", None)
        with patch.dict(os.environ, env, clear=True):
            return h._build_context_entries(items, 8000, window)  # type: ignore[arg-type]

    # --- threshold > 0 (new default: 300) --------------------------------------

    def test_threshold_300_short_body_inlines_in_full(self):
        # 60-char body < 300 → inline, full body, no "read it:" line.
        body = "short note about the I2P search pipeline"
        report = self._render(
            [_item("learning-1", body, 0.9, locator=_LOCALENG)],
            env_threshold="300",
        )
        self.assertIn("short note about the I2P search pipeline", report["rendered"])
        self.assertNotIn("read it:", report["rendered"])
        self.assertEqual(report["mode_split"]["inline"], 1)
        self.assertEqual(report["mode_split"]["locator_preview"], 0)
        self.assertEqual(report["injected"][0]["mode"], "inline")

    def test_threshold_300_long_body_collapses_to_locator_plus_preview(self):
        # ~1500-char multi-line body > 300 → locator line + ~2-line preview,
        # not the full body (per AC4 "key — locator — read it").
        body = "\n".join(
            [
                "Line 1 of a long note about the I2P search pipeline.",
                "Line 2 covers lease-set monitoring and SAM detection.",
                "Line 3 discusses protocol detection and link following.",
                "Line 4 covers BM25 ranking and simhash deduplication.",
                "Line 5 is deep technical detail that goes well past the preview.",
                "Line 6 continues with retrieval pointer plumbing.",
                "Line 7 ends with a retrieval pointer to the original source.",
            ]
        ) * 3
        report = self._render(
            [_item("learning-2", body, 0.9, locator=_LOCALENG)],
            env_threshold="300",
        )
        rendered = report["rendered"]
        # Locator line is present (per AC4 "rendered text shows 'key — locator — read it'").
        self.assertIn("read it:", rendered)
        # Preview is present (opening lines of the body).
        self.assertIn("Line 1 of a long note", rendered)
        self.assertIn("Line 2 covers lease-set monitoring", rendered)
        # The LAST line of the repeated 3× body is NOT in the rendered text:
        # make_preview kept only the opening lines.
        self.assertNotIn("Line 7 ends with a retrieval pointer", rendered)
        self.assertEqual(report["mode_split"]["locator_preview"], 1)
        self.assertEqual(report["injected"][0]["mode"], "locator_preview")

    def test_threshold_zero_collapses_even_tiny_body(self):
        # threshold=0 → every entry with a locator gets locator+preview form.
        # body "tiny" (4 chars) fits the preview cap so the preview IS the
        # body verbatim — but the "read it:" line is still attached, so the
        # rendered text shows BOTH the locator and the body content peek.
        body = "tiny"
        report = self._render(
            [_item("learning-3", body, 0.9, locator=_LOCSHORT)],
            env_threshold="0",
        )
        self.assertIn("read it:", report["rendered"])
        self.assertIn("tiny", report["rendered"])
        self.assertEqual(report["injected"][0]["mode"], "locator_preview")

    # --- threshold is None (legacy path) ---------------------------------------

    def test_legacy_threshold_none_long_body_collapses_no_preview(self):
        # threshold=None → pre-#207 gate: LOCATOR_INJECT_THRESHOLD=240.
        # A 1000-char body triggers the locator path, but no preview is appended
        # (byte-identical rendered line to the pre-#207 output).
        body = ("legacy body — " * 80).strip()  # ~960 chars
        report = self._render(
            [_item("learning-4", body, 0.9, locator=_LOCALENG)],
            env_threshold="none",
        )
        rendered = report["rendered"]
        self.assertIn("read it:", rendered)
        # The body is NOT in the rendered text — legacy mode never previews.
        self.assertNotIn("legacy body", rendered)
        self.assertEqual(report["injected"][0]["mode"], "locator_preview")

    def test_legacy_threshold_none_short_body_inlines(self):
        # threshold=None, 60-char body → under LOCATOR_INJECT_THRESHOLD (240)
        # → inline in full (pre-#207 gate also inlines short bodies).
        body = "short legacy body"
        report = self._render(
            [_item("learning-5", body, 0.9, locator=_LOCALENG)],
            env_threshold="none",
        )
        self.assertIn("short legacy body", report["rendered"])
        self.assertNotIn("read it:", report["rendered"])
        self.assertEqual(report["injected"][0]["mode"], "inline")

    # --- no locator stored → always inline ------------------------------------

    def test_no_locator_stored_always_inlines(self):
        body = "x" * 900
        report = self._render(
            [_item("learning-6", body, 0.9, locator=None)],
            env_threshold="300",
        )
        self.assertEqual(report["mode_split"]["inline"], 1)
        self.assertEqual(report["mode_split"]["locator_preview"], 0)

    # --- mode_split counts -----------------------------------------------------

    def test_mode_split_counts_a_mixed_batch(self):
        short = "just one short line"
        long_body = ("line one\n" * 40 + "line forty\n")
        items = [
            _item("a", short, 0.9, locator=_LOCALENG),
            _item("b", long_body, 0.8, locator=_LOCALENG),
            _item("c", short, 0.7, locator=_LOCALENG),
            _item("d", long_body, 0.6, locator=_LOCALENG),
        ]
        report = self._render(items, env_threshold="300")
        self.assertEqual(report["mode_split"]["inline"], 2)
        self.assertEqual(report["mode_split"]["locator_preview"], 2)
        self.assertEqual(len(report["injected"]), 4)

    # --- degraded path (no items) --------------------------------------------

    def test_no_items_empty_report_shape(self):
        import memchorus.hooks as h
        window = _FreshWindow()
        with patch.dict(os.environ, {"MEMCHORUS_RECALL_MAX_CHARS": "8000"}, clear=True):
            report = h.simulate_recall_render([])
        self.assertEqual(report["rendered"], "")
        self.assertEqual(report["injected"], [])
        self.assertEqual(
            report["mode_split"],
            {"inline": 0, "locator_preview": 0, "suppressed": 0},
        )


if __name__ == "__main__":
    unittest.main()
