#!/usr/bin/env python3
"""C13b — ID-space mismatch: MemChorus pointer keys vs MemPalace drawer_ids.

The C13 liveness gate (t_510abeae / t_18c26df5) proved that a pruned/evicted
memo can be distinguished from a live one (COLD vs DEAD vs RESOLVED).  That
work is complete and this file does **not** re-test it.

C13b is a new, narrower bug class that sits ON TOP of C13: even a perfectly
live MemChorus memo carries a pointer key in the `<category>-<24-hex>`
namespace (e.g. ``mistake-ac5ab347a4211a3f``).  A reader who calls
``mempalace_get_drawer(key='mistake-ac5ab347a4211a3f')`` gets a 404 — not
because the memo is cold, but because that key was never a MemPalace
``drawer_id`` (which has the shape ``drawer_<wing>_<room>_<16-hex>``).
This is a formal **ID-space mismatch**, not a COLD miss.

What this test proves
--------------------
RED  — A MemChorus pointer key (with the exact three recurring keys from the
       Oct-23 cycle) does NOT resolve in MemPalace's local key space, while a
       legitimate ``drawer_…`` key does.  This is the structural ID-space
       mismatch, present at base, and independent of any cold-cache state.

GREEN — After the fix, ``_format_context_block`` renders an ID-space
       disambiguation hint for low-signal pointer entries so the reader can
       scope a ``mempalace_search`` call using category + source without a
       prior 404.  The ``read it: retrieve(key='…')`` pointer and the
       cold-cache note from #221 are preserved as regression guards.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.hooks import _format_context_block, _clear_suppression_windows
from memchorus.mempalace_memory_source import MemPalaceMemorySource

# ---------------------------------------------------------------------------
# The three recurring keys from the 2026-10-23 cycle that all 404'd in
# mempalace_get_drawer while being valid MemChorus pointers.
# ---------------------------------------------------------------------------
HERMES_KEYS = [
    "mistake-ac5ab347a4211a3f",
    "mistake-150f07b50e21044d",
    "learning-5ae6fc94-9940",
]

# A drawer key in MemPalace's own namespace (not a memchorus pointer).
MEMPALACE_DRAWER_KEY = "drawer_code_general_a1b2c3d4e5f6a7b8"
MEMPALACE_BODY = "legitimate drawer body content"


def _mp_src(tmpdir: str) -> MemPalaceMemorySource:
    return MemPalaceMemorySource(
        config={"skip_mcp": True, "cache_dir": os.path.join(tmpdir, "mp_cache")}
    )


# ===========================================================================
# SECTION 1 — RED: the structural ID-space mismatch (fails at base)
# ---------------------------------------------------------------------------
# A MemChorus pointer key does not exist in MemPalace's key namespace even
# when the palace has a legitimate drawer key present.  The two id-spaces
# are disjoint by construction.
# ===========================================================================
class TestIdSpaceMismatchRed(unittest.TestCase):
    """Prove that memchorus pointer keys and mempalace drawer keys are
    structurally disjoint — the 404 is not a cold-cache artefact."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c13b_red_")
        self.src = _mp_src(self._tmp)
        # Populate the local cache with a legitimate mempalace drawer key.
        self.src.save(MEMPALACE_DRAWER_KEY, {"text": MEMPALACE_BODY})

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def control_mempalace_drawer_key_resolves(self):
        """Control: the drawer key WAS saved and IS found in mempalace."""
        result = self.src.retrieve(MEMPALACE_DRAWER_KEY)
        self.assertIsNotNone(
            result,
            "Control failed: a saved mempalace drawer key did not resolve — "
            "this test setup is broken and the RED assertions below are "
            "independent of the mismatch.",
        )
        self.assertIn(MEMPALACE_BODY, str(result))

    def test_hermes_pointer_keys_do_not_resolve_in_mempalace(self):
        """RED: each of the three recurring keys is absent from mempalace.

        This is the structural ID-space mismatch.  A memchorus pointer key
        of the form ``<category>-<hex>`` is never the same key space as a
        mempalace ``drawer_id`` — so mempalace cannot resolve them, even
        though the memchorus body is live and retrievable via
        ``retrieve(key=…)`` on the hermes_default source.
        """
        for hk in HERMES_KEYS:
            result = self.src.retrieve(hk)
            # The hermes pointer key is simply not in mempalace's key space.
            # Depending on the miss representation (sentinel, None, or "")
            # the result varies — but it must NOT be the legitimate body.
            self.assertNotIn(
                MEMPALACE_BODY,
                str(result),
                f"RED assertion failed: hermes pointer {hk!r} unexpectedly "
                f"resolved to a mempalace drawer body ({result!r}).  This "
                f"means the ID-space mismatch is not structural — investigate "
                f"the key mapping before trusting this RED guard.",
            )
            # If a miss sentinel exists, verify the hermes key is a miss.
            try:
                from memchorus.mempalace_memory_source import RETRIEVE_MISS
                if result is not None and not isinstance(result, type(RETRIEVE_MISS)):
                    # result is a real value, not a sentinel — it should not
                    # contain any drawer body content.
                    self.assertNotIn("drawer", str(result).lower())
            except ImportError:
                pass  # pre-#221 code: bare None is the miss, that's fine


# ===========================================================================
# SECTION 2 — GREEN: the rendered block carries the disambiguation hint
# ---------------------------------------------------------------------------
# After the fix, low-signal entries with a memchorus pointer key in the
# rendered recall block include an ID-space annotation that tells the reader
# the key is a memchorus pointer (not a mempalace drawer_id) and provides a
# category + source hint for scoping a mempalace_search call.
# ===========================================================================
class TestIdSpaceHintGreen(unittest.TestCase):
    """GREEN: the fix adds an ID-space disambiguation hint to the render."""

    def setUp(self):
        _clear_suppression_windows()

    def tearDown(self):
        _clear_suppression_windows()

    @staticmethod
    def _render(key: str, source: str = "hermes_default",
                content: str = '{"ok": true, "n": 1, "v": 2}') -> str:
        """Produce the rendered recall block for one low-signal entry."""
        item = {"key": key, "content": content, "score": 0.9, "source": source}
        return _format_context_block([item])

    # ---- GREEN: the ID-space hint is present for memchorus pointer keys ---- #

    def test_id_space_hint_present_for_pointer_key(self):
        """GREEN: a memchorus pointer key renders with an ID-space annotation.

        The hint must contain:
          - a mention that the key is NOT a mempalace drawer_id
            (or equivalent: 'id-space', 'mempalace', 'drawer_id')
          - a scope/search hint (category, source) for mempalace_search
          - the key itself, so the reader can trace which key the hint applies to
        """
        out = self._render(HERMES_KEYS[0])
        low = out.lower()

        # The annotation is present and names the mismatch.
        has_id_space = ("id-space" in low) or ("id space" in low)
        has_mempalace = "mempalace" in low
        has_drawer = "drawer" in low
        self.assertTrue(
            has_id_space or (has_mempalace and has_drawer),
            f"GREEN failed: no ID-space annotation in rendered block. "
            f"id_space={has_id_space} mempalace={has_mempalace} drawer={has_drawer}\n"
            f"rendered: {out[:200]}",
        )

        # A scoping hint (category, source, or search) is present.
        has_scope = any(w in low for w in ("category", "scope", "search", "mempalace_search"))
        self.assertTrue(
            has_scope,
            f"GREEN failed: no mempalace_search scoping hint in rendered block.\n"
            f"rendered: {out[:200]}",
        )

    def test_id_space_hint_names_the_category(self):
        """GREEN: the category prefix of the key is printed as a search filter.

        For 'mistake-ac5ab347a4211a3f' the category 'mistake' should appear
        in the hint so a reader can scope mempalace_search by it.
        """
        out = self._render(HERMES_KEYS[0])
        low = out.lower()
        self.assertIn("mistake", low,
                      f"GREEN failed: category 'mistake' not in rendered hint:\n{out[:200]}")

    def test_id_space_hint_for_learning_key(self):
        """GREEN: a learning-* key also gets the ID-space hint with its category."""
        out = self._render(HERMES_KEYS[2])
        low = out.lower()
        self.assertIn("learning", low,
                      f"GREEN failed: category 'learning' not in rendered hint:\n{out[:250]}")
        self.assertTrue(
            ("id-space" in low) or ("mempalace" in low),
            f"GREEN failed: ID-space hint not present for learning key.\n{out[:200]}",
        )


    # ---- Regression: existing #221 directives still present ---------------- #

    def test_pointer_directive_still_present(self):
        """Regression: the canonical reachable-pointer directive must remain
        in the rendered gist so the C13 gate still recognises the entry as a
        pointer.  Per spec-253 the directive now names the CLI subcommand
        (``memchorus-recall retrieve <key>``) as the reachable handle, not
        the in-process ``retrieve(key='…')`` call.
        """
        out = self._render(HERMES_KEYS[0])
        self.assertIn(
            "memchorus-recall retrieve mistake-ac5ab347a4211a3f",
            out,
            f"Regression: reachable pointer directive missing from rendered block:\n{out[:250]}",
        )

    def test_cold_cache_note_still_present(self):
        """Regression (#221): the cold-cache note is still stated somewhere
        in the rendered block.
        """
        out = self._render(HERMES_KEYS[0])
        low = out.lower()
        self.assertTrue(
            ("cold" in low) or ("re-fetch" in low) or ("refetch" in low),
            f"Regression (#221): cold-cache note missing from rendered block:\n{out[:250]}",
        )

    def test_non_hex_key_does_not_crash_render(self):
        """Regression: a non-standard key (no category-hex pattern) still
        renders without crashing — it may or may not have the ID-space hint,
        but it must not raise.
        """
        out = self._render("LEARNING_PTR7770", source="hermes_default")
        self.assertIn("LEARNING_PTR7770", out)
        # Should not be blank or an error.
        self.assertGreater(len(out), 50)

    def test_non_hermes_source_still_renders(self):
        """Regression: a non-hermes_default source item also renders."""
        out = self._render("session-20261015", source="mempalace")
        self.assertIn("session-20261015", out)


if __name__ == "__main__":
    unittest.main()
