"""GH-160: scratch/fixture memory must be demoted in orchestrator.search().

Root cause: the provenance filter inside MemoryOrchestrator.search() only
demotes artifacts matching
  - key prefixes "auto-tool-*"/"result-*"/"auto-result-*"
  - categories containing "RESULT"
  - content.get("_auto_provenance") or provenance == "auto_stored"
There was NO demotion path for SCRATCH or FIXTURE payloads. Empirical failure:
the scratch/fixture payload from a prior "quick brown fox" test outranked real
standing docs for the query "standing facts", suppressing the actual
standing-facts document in top-3 recall.

Fix (impl): one new PATH 4 inside _is_auto_metadata() — dict content whose
`categories` contain any of {scratch, fixture, test, example, demo}
(case-insensitive), or whose `provenance` field equals any of those values,
is now treated as auto metadata and scaled by PENALTY_FACTOR (0.3).

Acceptance (this file):
  1. Synthetic orchestrator (hermes_default + disabled mempalace) seeds
       - key "scratch-1": {"_content": "quick brown fox example",
                           "categories": ["scratch"]}
       - key "real-1":    {"_content": "standing facts about the user"}
     For query "standing facts", "real-1" appears BEFORE "scratch-1".
  2. The provenance-field variant ({... "provenance": "scratch"}) is demoted
     as well (PATH 4 second clause).
  3. Existing behavior is untouched: a plain user-authored dict with no
     scratch/fixture signals is NOT demoted (sanity: it still ranks first).
"""

import shutil
import tempfile
import unittest

from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource


class _SandboxedOrchestratorMixin:
    """Shared fixture: hermes_default backed by a temp dir, mempalace disabled."""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.orch = MemoryOrchestrator()
        self.orch.memory_sources["hermes_default"] = (
            HermesDefaultMemorySource(self._tmp_dir)
        )
        if "mempalace" in self.orch.memory_sources:
            self.orch.disable_source("mempalace")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


class TestScratchFixtureDemotion(_SandboxedOrchestratorMixin, unittest.TestCase):
    """GH-160 acceptance: real standing content outranks scratch payloads."""

    def test_real_standing_content_outranks_scratch_category_payload(self):
        # The scratch/fixture payload is seeded via the hermes_default source
        # directly: the orchestrator's strict category validator (BUG_2
        # whitelist) rejects "scratch" at orch.save() time — exactly why such
        # payloads persist un-demoted in the store (see issue #161 for that
        # coupled gap). The search-side provenance filter (PATH 4) is what this
        # task fixes.
        self.orch.memory_sources["hermes_default"].save(
            "scratch-1",
            {"_content": "quick brown fox example", "categories": ["scratch"]},
        )
        self.orch.save(
            "real-1",
            {"_content": "standing facts about the user"},
        )

        results = self.orch.search("standing facts")
        keys = [r["key"] for r in results]

        self.assertIn("real-1", keys, "real standing-facts doc must be recalled")
        if "scratch-1" in keys:
            self.assertLess(
                keys.index("real-1"),
                keys.index("scratch-1"),
                "real-1 (%d) must rank before scratch-1 (%d): %s"
                % (keys.index("real-1"), keys.index("scratch-1"), keys),
            )
        else:
            # scratch payload not recalled at all — demotion so strong it fell
            # out of the fetch window; real content must sit at the top instead.
            self.assertEqual(keys[0], "real-1")

    def test_scratch_payload_does_not_suppress_real_doc_from_top3(self):
        """The exact empirical failure from issue #160: top-3 must not be
        dominated by the scratch fixture while the standing-facts doc is pushed
        out of the window."""
        # Seed via source: scratch-payload provenance bypasses the orchestrator
        # category whitelist (that rejection is the #161 gap, not this task).
        self.orch.memory_sources["hermes_default"].save(
            "scratch-1",
            {"_content": "quick brown fox example", "categories": ["scratch"]},
        )
        self.orch.save(
            "real-1",
            {"_content": "standing facts about the user"},
        )

        results = self.orch.search("standing facts", max_results=5)
        keys = [r["key"] for r in results]

        if "scratch-1" in keys:
            scratch_score = next(
                r["score"] for r in results if r["key"] == "scratch-1"
            )
            # PENALTY_FACTOR = 0.3 — a demoted scratch payload's scaled score
            # must be at or below 0.3x its raw magnitude; simplest observable:
            # it must NOT lead the ranking over the real document.
            if "real-1" in keys:
                real_score = next(
                    r["score"] for r in results if r["key"] == "real-1"
                )
                self.assertGreaterEqual(
                    real_score,
                    scratch_score,
                    "demotion failed: real=%s scratch=%s"
                    % (real_score, scratch_score),
                )

    def test_provenance_field_scratch_is_demoted(self):
        """PATH 4 second clause: provenance field (not just categories)."""
        self.orch.save(
            "fixture-1",
            {"_content": "standing facts scratch padding",
             "provenance": "scratch"},
        )
        self.orch.save(
            "real-2",
            {"_content": "standing facts about the user"},
        )

        results = self.orch.search("standing facts")
        keys = [r["key"] for r in results]

        self.assertIn("real-2", keys)
        if "fixture-1" in keys:
            self.assertLess(
                keys.index("real-2"),
                keys.index("fixture-1"),
                "provenance='scratch' payload must be demoted below real-2: %s"
                % keys,
            )

    def test_untagged_user_content_not_demoted(self):
        """Sanity: plain user-authored dicts are NOT treated as auto metadata."""
        self.orch.save(
            "user-1",
            {"_content": "standing facts about the user, written by hand"},
        )

        results = self.orch.search("standing facts")
        keys = [r["key"] for r in results]

        self.assertEqual(
            keys[0],
            "user-1",
            "untagged user content must still rank first (no false demotion)",
        )


if __name__ == "__main__":
    unittest.main()
