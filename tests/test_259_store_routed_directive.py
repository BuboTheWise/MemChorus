#!/usr/bin/env python3
"""GH-259 / t_53c8e9bc — store-routed retrieve directive.

AC1 (authoritative): when a pointer key is injected with ``source=hermes_default``
(its canonical form — the class name every profile uses for its Hermes memory
source), the injected ``memchorus-recall retrieve <key>`` directive MUST carry
``--profile default`` (derived from the source marker, per AC1).

This makes the directive resolvable as written — an agent copying it into a
shell lands on the default leaf (``~/.hermes/memories``) where the key actually
lives, instead of cold-missing (exit 1) by accidentally routing to the *reader's*
own leaf (``~/.hermes/profiles/<reader>/memories``).

RED  — Base (c5f63d5): the directive is ``memchorus-recall retrieve <key>``
       with no ``--profile``.  An agent in cthugha executing it resolves
       against ``HERMES_PROFILE=cthugha`` → ``~/.hermes/profiles/cthugha/memories``
       → cold miss (exit 1) → re-fetches the live source.  ``--profile`` absent →
       assertion fails.

GREEN — Fixed: the directive renders
       ``memchorus-recall retrieve <key> --profile default`` — CLI resolves
       to ``~/.hermes/memories`` where the ``hermes_default`` key actually lives.

Non-hermes sources (mempalace, session_search) do not carry profile routing
(those have their own ID-space / retrieval mechanism) — no ``--profile`` flag
on those directives.

This file does NOT re-test the ID-space annotation (test_c13b) or the low-signal
gate (test_207_hot_injection) — only the *store-routing* suffix.
"""

import sys
import unittest

# ---------------------------------------------------------------------------
# Ensure src/ is importable regardless of pytest runner cwd
# ---------------------------------------------------------------------------
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.hooks import (
    _format_context_block,
    _profile_from_source_marker,
    _clear_suppression_windows,
)

# ---------------------------------------------------------------------------
# Keys from the live Oct-23 cycle (same set used by test_c13b).
# Each carries source=hermes_default — the canonical source-marker case.
# ---------------------------------------------------------------------------
HERMES_KEY = "mistake-ac5ab347a4211a3f"
MEMPALACE_KEY = "mempalace-abcdef12345678"

# ---------------------------------------------------------------------------
# Helper: render one low-signal entry with a given source marker
# ---------------------------------------------------------------------------
def _render(source: str, key: str, content: str = '{"ok": true, "n": 1, "v": 2}'):
    """Build one recall item and run it through _format_context_block."""
    items = [{"key": key, "content": content, "score": 0.9, "source": source}]
    return _format_context_block(items)


# ===========================================================================
# SECTION 1 — AC1: hermes_default source → --profile default in directive
# ===========================================================================
class TestAC1_HermesDefaultSourceMarker(unittest.TestCase):
    """AC1: source=hermes_default → directive carries --profile default.

    This is the canonical cross-profile case from GH-259:
      - Reader: cthugha (HERMES_PROFILE=cthugha set by dispatcher)
      - Key:    learning/mistake keys written by Bubo (default profile)
      - Leaf:   ~/.hermes/memories (default)
      - Without fix: agent runs `retrieve <key>` in cthugha leaf → cold miss
      - With fix:    agent runs `retrieve <key> --profile default` → default leaf → hit
    """

    def setUp(self):
        _clear_suppression_windows()

    def tearDown(self):
        _clear_suppression_windows()

    def test_directive_carries_profile_default_for_hermes_default_source(self):
        """GREEN: source=hermes_default → `--profile default` in the directive.

        RED: the pre-fix directive has no --profile at all.
        """
        out = _render(source="hermes_default", key=HERMES_KEY)
        self.assertIn(
            f"memchorus-recall retrieve {HERMES_KEY} --profile default",
            out,
            "GH-259 AC1 RED: directive missing `--profile default`. "
            "An agent in cthugha shell-executing this resolves against the "
            "cthugha leaf (~/.hermes/profiles/cthugha/memories), cold-misses, "
            "then re-fetches the live source — the round-trip bug.\n"
            f"rendered: {out[:400]}",
        )

    def test_source_marker_derives_profile_default(self):
        """AC1: `_profile_from_source_marker('hermes_default')` → 'default'."""
        self.assertEqual(
            _profile_from_source_marker("hermes_default"),
            "default",
            "AC1: hermes_default → default (strip 'hermes_' prefix).",
        )

    def test_source_marker_none_for_non_hermes_source(self):
        """No --profile for mempalace sources (different ID-space, own resolver)."""
        self.assertIsNone(
            _profile_from_source_marker("mempalace"),
            "Non-hermes sources must not get --profile — they use a different "
            "retrieval mechanism (mempalace_search, not retrieve).",
        )

    def test_source_marker_none_for_none_source(self):
        """No source field → no --profile (defensive)."""
        self.assertIsNone(_profile_from_source_marker(None))

    def test_directive_is_single_copyable_command(self):
        """The directive must be a single, paste-able command — no backticks,
        no trailing comma.
        """
        out = _render(source="hermes_default", key=HERMES_KEY)
        expected_cmd = f"memchorus-recall retrieve {HERMES_KEY} --profile default"
        self.assertIn(expected_cmd, out)
        self.assertNotIn("`", expected_cmd)
        self.assertNotIn(",--profile", out)   # no trailing comma before the flag
        self.assertNotIn("--profile,", out)   # no trailing comma after the flag value

    def test_source_marker_still_visible_to_agent(self):
        """The source=hermes_default marker is still shown (human-readable context
        for the agent to understand which store the key originates from).
        """
        out = _render(source="hermes_default", key=HERMES_KEY)
        self.assertIn("source=hermes_default", out,
                      "The source marker must remain visible for human/agent context.")


# ===========================================================================
# SECTION 2 — Regression: mempalace / non-hermes items omit --profile
# ===========================================================================
class TestNonHermesSourcesNoProfileFlag(unittest.TestCase):
    """Non-hermes sources must NOT carry --profile (different retrieval path)."""

    def setUp(self):
        _clear_suppression_windows()

    def tearDown(self):
        _clear_suppression_windows()

    def test_mempalace_source_omits_profile_flag(self):
        out = _render(source="mempalace", key=MEMPALACE_KEY)
        # Retrieve directive present (for pointer keys), but no --profile.
        self.assertIn(f"memchorus-recall retrieve {MEMPALACE_KEY}", out)
        self.assertNotIn("--profile", out,
                         f"mempalace source must NOT carry --profile flag.\n"
                         f"rendered: {out[:400]}")


# ===========================================================================
# SECTION 3 — Regression: existing #234/#256 pins survive
# ===========================================================================
class TestExistingDirectivePinsSurvive(unittest.TestCase):
    """The #234 / #256 ID-space note, low-signal collapse, source-marker shape,
    and `read it:` phrasing must all still render correctly after GH-259.
    """

    def setUp(self):
        _clear_suppression_windows()

    def tearDown(self):
        _clear_suppression_windows()

    def test_read_it_phrasing_preserved(self):
        out = _render(source="hermes_default", key=HERMES_KEY)
        self.assertIn("read it:", out)

    def test_id_space_note_preserved(self):
        out = _render(source="hermes_default", key=HERMES_KEY)
        # #234 C13b note must still be present.
        self.assertIn("ID-space mismatch", out,
                      "#234 ID-space note must survive GH-259.\n" + out[:400])

    def test_cold_cache_note_still_present(self):
        out = _render(source="hermes_default", key=HERMES_KEY)
        self.assertTrue(
            ("cold" in out.lower()) or ("re-fetch" in out.lower()),
            "cold-cache / re-fetch note must still be present.\n" + out[:400],
        )

    def test_hermes_key_pattern_still_collapsed_to_pointer(self):
        """A hermes-default low-signal entry must still collapse to pointer key
        (the `gist` collapse path #217) — the pointer key appears in the output.
        """
        out = _render(source="hermes_default", key=HERMES_KEY)
        self.assertIn(HERMES_KEY, out,
                      "The pointer key must appear in the collapsed directive.")


# ===========================================================================
# Section 4 — _profile_from_source_marker edge cases
# ===========================================================================
class TestProfileFromSourceMarkerEdgeCases(unittest.TestCase):
    def test_hermes_lowercase_only_stripped(self):
        # The prefix match should be case-insensitive or handle mixed case
        self.assertEqual(_profile_from_source_marker("hermes_default"), "default")

    def test_empty_string_returns_none(self):
        self.assertIsNone(_profile_from_source_marker(""))

    def test_non_hermes_prefix_not_matched(self):
        # A source like 'mempalace_x' should not be confused with 'hermes_'
        self.assertIsNone(_profile_from_source_marker("mempalace_x"))

    def test_hermes_only_no_suffix(self):
        # 'hermes' with no suffix — should return None or empty, not crash
        result = _profile_from_source_marker("hermes")
        self.assertIn(result, (None, ""),
                      f"'hermes' (no suffix) should yield None or '', got {result!r}")


if __name__ == "__main__":
    unittest.main()
