#!/usr/bin/env python3
"""Unit tests for GH-#304: {PROFILE}_{CATEGORY} wings in expected_wings().

These three tests verify:
  1. own-profile {PROFILE}_{CATEGORY} → native (not foreign);
  2. a genuinely foreign profile's wing → still flagged;
  3. regression guard that #286 cross-profile detection is intact.
"""
import sys
sys.path.insert(0, "/home/stefan/Projects/MemChorus/src")

import unittest
from memchorus.wing_isolation import (
    LEGACY_CATEGORY_WINGS,
    expected_wings,
    audit_palace_wings,
    classify_wing,
)


class TestProfilePrefixedCategoriesGH304(unittest.TestCase):
    """GH-#304: the #297 writer produces {PROFILE}_{CATEGORY} wings.

    The auditor (wing_isolation) must treat those as native for the owning
    profile, while continuing to flag genuinely foreign wings.
    """

    KNOWN = ("default", "cthugha", "grok")

    # ------------------------------------------------------------------
    # 1. Own-profile {PROFILE}_{CATEGORY} → native (not foreign)
    # ------------------------------------------------------------------
    def test_profile_prefixed_categories_are_native(self):
        """Profile-prefixed legacy keys from #297 writer are in-namespace."""
        # Verify the expected set actually contains the prefixed keys
        exp = expected_wings("cthugha")
        for cat in sorted(LEGACY_CATEGORY_WINGS):
            self.assertIn(f"cthugha_{cat}", exp,
                          f"{cat} prefixed should be in expected_wings('cthugha')")

        # Now drive the auditor with those wings present: cthugha should be clean
        audit = audit_palace_wings(
            wings=["memchorus_decisions",
                   "cthugha_DECISION", "cthugha_LEARNING",
                   "wing_cthugha"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertTrue(audit.is_clean,
                        f"cthugha palace with profile-prefixed wings should be clean — "
                        f"issues: {[i.wing for i in audit.issues]}")
        self.assertEqual(audit.cross_profile_count, 0)
        self.assertEqual(audit.out_of_namespace_count, 0)

    # ------------------------------------------------------------------
    # 2. Genuinely foreign profile's wing → still flagged
    # ------------------------------------------------------------------
    def test_foreign_profile_wing_still_flagged(self):
        """A wing belonging to a sibling profile is cross-profile."""
        audit = audit_palace_wings(
            wings=["memchorus_decisions", "wing_grok"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertTrue(audit.has_leaks)
        self.assertEqual(audit.cross_profile_count, 1)
        self.assertEqual(audit.out_of_namespace_count, 0)
        self.assertEqual(audit.issues[0].wing, "wing_grok")
        self.assertEqual(audit.issues[0].owner, "grok")

    # ------------------------------------------------------------------
    # 3. Regression guard: #286 cross-profile detection intact
    # ------------------------------------------------------------------
    def test_cross_profile_regression(self):
        """Ensure #286 cross-profile detection has not regressed.

        Before #304 fix, cross-profile detection already worked; this
        confirms it still does.
        """
        audit = audit_palace_wings(
            wings=["memchorus_decisions", "wing_grok", "wing_cthugha"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        # wing_cthugha must NOT be flagged (it's own)
        self.assertEqual(audit.cross_profile_count, 1)
        self.assertEqual(audit.issues[0].wing, "wing_grok")

        # Verify the auditor's own_wings include both own wing forms
        self.assertIn("wing_cthugha", audit.own_wings)

    # ------------------------------------------------------------------
    # 4. Mixed case: own-profile prefixed + truly foreign → foreign only
    # ------------------------------------------------------------------
    def test_mixed_own_prefixed_and_foreign(self):
        """Own prefixed wings are native; only the truly foreign one is flagged."""
        audit = audit_palace_wings(
            wings=["memchorus_decisions",
                   "cthugha_DECISION", "cthugha_LEARNING",
                   "wing_cthugha",
                   "aletheia"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        # Only aletheia should be flagged
        self.assertEqual(len(audit.issues), 1)
        self.assertEqual(audit.issues[0].wing, "aletheia")
        self.assertEqual(audit.issues[0].kind, "out-of-namespace")

    # ------------------------------------------------------------------
    # 5. classify_wing companion
    # ------------------------------------------------------------------
    def test_classify_wing_profile_prefixed_clean(self):
        """classify_wing returns None for own-profile {PROFILE}_{CATEGORY}.

        The write-side guard should not warn when the writer emits its own
        prefixed key.
        """
        issue = classify_wing(
            "cthugha_DECISION",
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertIsNone(issue,
                          "classify_wing should return None for own prefixed key")

    def test_classify_wing_cross_profile_warns(self):
        """classify_wing flags a sibling profile's own wing."""
        issue = classify_wing(
            "wing_grok",
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertIsNotNone(issue)
        self.assertEqual(issue.kind, "cross-profile")
        self.assertEqual(issue.owner, "grok")

    # ------------------------------------------------------------------
    # 6. Bare profile name (GH-304 residual): own → in-namespace,
    #    foreign → cross-profile
    # ------------------------------------------------------------------
    def test_bare_own_profile_name_is_in_namespace(self):
        """GH-304: a wing named exactly after the owning profile (bare, no
        prefix/suffix) is that profile's own wing and must not be flagged.

        Older installs may have written the bare profile name directly. All
        three canonical own-wing forms (bare name, wing_ prefix,
        profile-prefixed) must be in-namespace together.
        """
        audit = audit_palace_wings(
            wings=["memchorus_decisions",
                   "cthugha",                    # bare own name (GH-304)
                   "cthugha_DECISION", "cthugha_LEARNING",
                   "wing_cthugha"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertTrue(audit.is_clean,
                        f"cthugha palace with bare own wing should be clean — "
                        f"issues: {[i.wing for i in audit.issues]}")
        self.assertEqual(audit.cross_profile_count, 0)
        self.assertEqual(audit.out_of_namespace_count, 0)
        self.assertIn("cthugha", audit.own_wings)

    def test_bare_foreign_profile_name_still_cross_profile(self):
        """GH-304: a bare wing named after a *sibling* profile is that
        sibling's own wing — a cross-profile misroute in this profile's store."""
        audit = audit_palace_wings(
            wings=["memchorus_decisions", "wing_cthugha", "grok"],
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertTrue(audit.has_leaks)
        self.assertEqual(audit.cross_profile_count, 1)
        self.assertEqual(audit.out_of_namespace_count, 0)
        self.assertEqual(audit.issues[0].wing, "grok")
        self.assertEqual(audit.issues[0].owner, "grok")

    def test_classify_wing_bare_own_profile_clean(self):
        """classify_wing (write-side guard) returns None for the bare own
        profile name — the writer emitting its own bare name is expected."""
        issue = classify_wing(
            "cthugha",
            active_profile="cthugha",
            known_profiles=self.KNOWN,
        )
        self.assertIsNone(issue,
                          "classify_wing should return None for bare own name")


if __name__ == "__main__":
    unittest.main(verbosity=True)
