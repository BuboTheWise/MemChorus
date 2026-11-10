"""Regression suite for issue #286 — cross-profile wing isolation.

A *named* profile's MemPalace palace should contain only the wings its own
MemChorus instance writes.  #286 was a leak: a row whose ``wing`` was a bare
project slug (``aletheia``) — a value belonging to *no* profile's namespace —
landed inside a profile-scoped palace.  This suite pins the three surfaces we
added so the bug stays caught:

  1. **Pure classifier** — ``memchorus.wing_isolation.audit_palace_wings`` and
     its write-side companion ``classify_wing``.  A cross-profile misroute and
     a bare-slug wing are both reported as foreign; in-namespace wings and the
     shared ``default`` profile are clean.  This is deterministic and drives the
     rest.

  2. **Write-boundary guard** — ``_McpClient.add_drawer``.  When a *named*
     profile writes a foreign wing, a ``logger.warning`` fires *at the moment of
     the write* (detection only — the write is never blocked).  The shared
     ``default`` profile stays silent for any wing.

  3. **Install-doctor check** — ``install_doctor.check_wing_isolation``.
     Translates the audit into PASS / WARN / FAIL.  Foreign wings default to WARN
     (human review); under ``--strict`` a *cross-profile* misroute is promoted to
     FAIL, while an out-of-namespace bare slug stays WARN (it cannot be told from
     a legitimate shared wing by string shape alone — issue #286 locked scope).

All tests are hermetic: the environment seams (active profile, known sibling
profiles, palace wing listing) are patched, and the pure classifier is exercised
for real so the seam stubs only feed it inputs.
"""

from __future__ import annotations

import logging
import unittest
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

from memchorus.wing_isolation import (
    NATIVE_WINGS,
    audit_palace_wings,
    classify_wing,
    expected_wings,
)


# ---------------------------------------------------------------------------
# 1. Pure classifier
# ---------------------------------------------------------------------------
class TestPureClassifier(unittest.TestCase):
    """Drives ``audit_palace_wings`` / ``classify_wing`` with synthetic wings.

    These never touch the filesystem — the classifier is a pure function of the
    wing set, the active profile, and the set of known sibling profiles.
    """

    KNOWN = ("default", "cthugha", "grok")

    # -- cross-profile leak -------------------------------------------------
    def test_cross_profile_wing_is_detected(self):
        """A wing owned by a known sibling profile is a cross-profile misroute."""
        audit = audit_palace_wings(
            wings=["memchorus_decisions", "wing_grok"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertTrue(audit.scoped)
        self.assertTrue(audit.has_leaks)
        self.assertEqual(audit.cross_profile_count, 1)
        issue = audit.issues[0]
        self.assertEqual(issue.wing, "wing_grok")
        self.assertEqual(issue.kind, "cross-profile")
        self.assertEqual(issue.owner, "grok")

    def test_own_wing_is_not_flagged(self):
        """A named profile's own ``wing_<name>`` is in-namespace, not a leak."""
        audit = audit_palace_wings(
            wings=["memchorus_decisions", "wing_cthugha"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertTrue(audit.is_clean)
        self.assertIn("wing_cthugha", audit.own_wings)

    # -- out-of-namespace (bare slug) ---------------------------------------
    def test_bare_slug_wing_is_out_of_namespace(self):
        """The #286 leak shape: a bare project slug no profile owns."""
        audit = audit_palace_wings(
            wings=["memchorus_decisions", "aletheia"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertTrue(audit.has_leaks)
        self.assertEqual(audit.out_of_namespace_count, 1)
        self.assertEqual(audit.cross_profile_count, 0)
        issue = audit.issues[0]
        self.assertEqual(issue.wing, "aletheia")
        self.assertEqual(issue.kind, "out-of-namespace")
        self.assertIsNone(issue.owner)

    def test_classify_wing_flags_the_leak(self):
        """Write-side companion: a single foreign wing resolves to a WingIssue."""
        issue = classify_wing(
            "aletheia",
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertIsNotNone(issue)
        if issue is None:  # pragma: no cover - satisfies the type checker
            self.fail("classify_wing should flag the aletheia bare slug")
        self.assertEqual(issue.kind, "out-of-namespace")

    # -- non-leak passes ----------------------------------------------------
    def test_clean_named_profile_passes(self):
        """A named profile with only in-namespace wings is clean."""
        clean_wings = ["memchorus_decisions", "memchorus_learning",
                       "memchorus_general", "self-improvement", "wing_cthugha"]
        audit = audit_palace_wings(
            wings=clean_wings,
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertTrue(audit.scoped)
        self.assertTrue(audit.is_clean)
        self.assertEqual(audit.issues, [])
        self.assertEqual(audit.cross_profile_count, 0)
        self.assertEqual(audit.out_of_namespace_count, 0)

    def test_classify_wing_silent_on_clean_write(self):
        issue = classify_wing("memchorus_decisions", active_profile="cthugha")
        self.assertIsNone(issue)

    # -- default is the shared profile --------------------------------------
    def test_default_profile_is_unscoped_and_clean(self):
        """The shared ``default`` palace legitimately holds any wings."""
        audit = audit_palace_wings(
            wings=["memchorus_decisions", "aletheia", "wing_grok",
                   "self-improvement", "research"],
            active_profile="default",
            known_profiles=self.KNOWN,
        )
        self.assertFalse(audit.scoped)
        self.assertTrue(audit.is_clean)  # foreign wings expected here
        self.assertEqual(audit.issues, [])

    def test_classify_wing_silent_on_default(self):
        """classify_wing returns None (no boundary concern) on the shared profile."""
        issue = classify_wing("aletheia", active_profile="default")
        self.assertIsNone(issue)

    # -- namespace bookkeeping ---------------------------------------------
    def test_expected_wings_includes_native_and_own(self):
        exp = expected_wings("cthugha")
        self.assertTrue(NATIVE_WINGS.issubset(exp))
        self.assertIn("wing_cthugha", exp)
        # default owns no profile-prefixed wing
        self.assertNotIn("wing_default", expected_wings("default"))

    def test_summary_mentions_foreign_categories(self):
        audit = audit_palace_wings(
            wings=["aletheia", "wing_grok"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertIn("cross-profile", audit.summary)
        self.assertIn("out-of-namespace", audit.summary)
        self.assertTrue(audit.suggested_fix)  # actionable when leaks exist

    def test_as_dict_is_serialisable(self):
        audit = audit_palace_wings(
            wings=["aletheia", "wing_grok"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        d = audit.as_dict()
        self.assertEqual(d["profile"], "cthugha")
        self.assertEqual(len(d["issues"]), 2)
        for issue in d["issues"]:
            self.assertIn("wing", issue)
            self.assertIn("kind", issue)


# ---------------------------------------------------------------------------
# 2. Write-boundary guard in _McpClient.add_drawer
# ---------------------------------------------------------------------------
class TestAddDrawerBoundary(unittest.TestCase):
    """Exercises the *real* ``add_drawer`` body — only ``call_tool`` is stubbed.

    The guard lives in the method itself, so we cannot stub ``add_drawer``
    (that would skip the very logic under test).  We build a bare client with
    ``__new__`` (skipping the heavy transport-detecting constructor) and point
    its ``call_tool`` at a successful result, then drive the real method.
    """

    def _make_client(self) -> Any:
        from memchorus.mempalace_memory_source import _McpClient

        client = _McpClient.__new__(_McpClient)
        client.call_tool = lambda name, arguments=None: {"success": True}
        return client

    def _run(self, client, wing, profile):
        """Run add_drawer under a given active profile; return (result, logger)."""
        import memchorus.mempalace_memory_source as mms

        mock_logger = MagicMock()
        with patch.object(mms, "active_profile_name", return_value=profile), \
             patch.object(mms, "logger", mock_logger):
            result = client.add_drawer(wing=wing, room="room", content="c")
        return result, mock_logger

    def test_named_profile_foreign_wing_warns_but_writes(self):
        client = self._make_client()
        result, mock_logger = self._run(client, "aletheia", "cthugha")
        # Detection only — the write still succeeds.
        self.assertTrue(result)
        mock_logger.warning.assert_called_once()
        msg = str(mock_logger.warning.call_args[0])
        self.assertIn("aletheia", msg)
        self.assertIn("#286", msg)

    def test_named_profile_cross_profile_wing_warns(self):
        client = self._make_client()
        result, mock_logger = self._run(client, "wing_grok", "cthugha")
        self.assertTrue(result)
        mock_logger.warning.assert_called_once()

    def test_named_profile_clean_wing_is_silent(self):
        client = self._make_client()
        result, mock_logger = self._run(client, "memchorus_decisions", "cthugha")
        self.assertTrue(result)
        mock_logger.warning.assert_not_called()

    def test_default_profile_foreign_wing_is_silent(self):
        """Shared profile: any wing is legitimate, so no boundary warning."""
        client = self._make_client()
        result, mock_logger = self._run(client, "aletheia", "default")
        self.assertTrue(result)
        mock_logger.warning.assert_not_called()

    def test_guard_never_breaks_the_write_on_classifier_error(self):
        """If classify_wing raises, add_drawer must still forward the write."""
        import memchorus.mempalace_memory_source as mms

        from memchorus.mempalace_memory_source import _McpClient

        client = _McpClient.__new__(_McpClient)
        client.call_tool = lambda name, arguments=None: {"success": True}
        with patch.object(mms, "active_profile_name", return_value="cthugha"), \
             patch.object(mms, "wing_isolation",
                          MagicMock(classify_wing=MagicMock(side_effect=RuntimeError("boom")))), \
             patch.object(mms, "logger", MagicMock()):
            result = client.add_drawer(wing="aletheia", room="r", content="c")
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# 3. Install-doctor check_wing_isolation
# ---------------------------------------------------------------------------
class TestDoctorCheck(unittest.TestCase):
    """Translates the audit into PASS / WARN / FAIL via the real check body.

    We patch the four environment seams (active profile, palace dir, wing
    listing, known profiles) and let the real ``check_wing_isolation`` run, so
    the status-decision logic is exercised for real.
    """

    def _run(self, profile, wings, known, strict=False):
        import memchorus.install_doctor as doctor

        with patch.object(doctor, "active_profile_name", return_value=profile), \
             patch.object(doctor, "_active_palace_dir",
                          return_value=Path("/test/palace")), \
             patch.object(doctor, "_list_palace_wings", return_value=wings), \
             patch.object(doctor, "_known_profile_names", return_value=known):
            return doctor.check_wing_isolation(strict=strict)

    KNOWN = {"default", "cthugha", "grok"}

    def test_no_palace_data_is_pass(self):
        res = self._run("cthugha", None, self.KNOWN)
        self.assertEqual(res.status, "PASS")
        self.assertIn("fresh install", res.message)

    def test_default_profile_is_pass(self):
        res = self._run("default",
                        ["memchorus_decisions", "aletheia", "wing_grok"],
                        self.KNOWN)
        self.assertEqual(res.status, "PASS")
        self.assertIn("shared profile", res.message)

    def test_clean_named_profile_is_pass(self):
        res = self._run("cthugha",
                        ["memchorus_decisions", "memchorus_learning",
                         "self-improvement", "wing_cthugha"],
                        self.KNOWN)
        self.assertEqual(res.status, "PASS")
        self.assertIn("in-namespace", res.message)

    def test_out_of_namespace_defaults_to_warn(self):
        res = self._run("cthugha",
                        ["memchorus_decisions", "aletheia"], self.KNOWN)
        self.assertEqual(res.status, "WARN")
        self.assertIn("aletheia", res.message)

    def test_out_of_namespace_stays_warn_under_strict(self):
        """Bare slugs cannot be told from shared wings — even --strict stays WARN."""
        res = self._run("cthugha",
                        ["memchorus_decisions", "aletheia"], self.KNOWN,
                        strict=True)
        self.assertEqual(res.status, "WARN")

    def test_cross_profile_warns_by_default(self):
        res = self._run("cthugha",
                        ["memchorus_decisions", "wing_grok"], self.KNOWN)
        self.assertEqual(res.status, "WARN")
        self.assertIn("wing_grok", res.message)
        self.assertIn("grok", res.message)

    def test_cross_profile_fails_under_strict(self):
        """A cross-profile misroute is unambiguous — --strict promotes to FAIL."""
        res = self._run("cthugha",
                        ["memchorus_decisions", "wing_grok"], self.KNOWN,
                        strict=True)
        self.assertEqual(res.status, "FAIL")
        self.assertIn("--strict", res.hint)

    def test_mixed_leaks_fail_under_strict(self):
        """Any cross-profile wing in a strict run fails, alongside a bare slug."""
        res = self._run("cthugha",
                        ["memchorus_decisions", "aletheia", "wing_grok"],
                        self.KNOWN, strict=True)
        self.assertEqual(res.status, "FAIL")


if __name__ == "__main__":
    unittest.main()
