"""GH-161: the standing-facts document MUST reach top-3 recall for its query.

This is the PROMOTION-side lock that pairs with GH-160's demotion lock
(``test_scratch_fixture_demotion.py``).  They protect the same coupled
failure from opposite angles:

  * #160  — demotion  : a scratch/fixture payload must NOT get to outrank the
                        real standing-facts document.
  * #161  — promotion : the standing-facts document ITSELF must survive into
                        the top-3 window, i.e. it is actually recalled, not just
                        "not below one competitor".

Empirical failure this guards (2026-08-31 session): the user's standing-facts
document is a user-authored, non-scratch memory — yet for the canonical query
"standing facts" that document did not reach top-3; instead a scratch "quick
brown fox" fixture outranked it.  #160 demotes that fixture; this test asserts
the standing-facts doc is actually recalled into top-3 (the promotion side), so
the two coupled failures cannot re-open in sequence.

Design notes (mirrors the #160 sandbox fixture):
  * The orchestrator's auto-registered ``hermes_default`` source (which points
    at the live profile memories dir) is OVERWRITTEN with a
    ``HermesDefaultMemorySource`` backed by a ``tempfile.mkdtemp()`` store, so
    the test is hermetic, xdist-safe, and never mutates global memory (the
    #160 card applies the same convention).
  * The seeded store is the *real* contention from the bug, not an artificial
    pile: the standing-facts document plus the empirically-proven outranker —
    a scratch/fixture payload whose content *echoes* "standing facts" — plus
    two genuinely-weaker single-term noise entries.  That keeps "standing-facts
    is in top-3" a meaningful, non-vacuous assertion (data-dependent on the
    recall path, not a self-evident truth) without requiring the standing doc to
    out-rank two equally-relevant user documents (a separate ranking-quality
    concern, not the #160/#161 bug).
  * The standing-facts document is rich and natural (not literally the query),
    so it scores ~1.0 on content matching WITHOUT triggering the self-match
    (query-echo) halving in ``_content_matches`` — the realistic shape of the
    user's real standing-facts doc. The scratch fixture carries
    ``categories: ["scratch"]`` and is seeded through the source directly so
    that tag persists — the orchestrator's strict category whitelist (BUG_2)
    would reject "scratch" at ``orch.save()`` time; that rejection is precisely
    why the real scratch payload survives un-demoted in the store.

Acceptance (this file):
  1. ``tests/test_standing_facts_recorder.py`` exists and seeds a standing-facts
     doc + the contesting scratch/fixture payload + weaker noise.
  2. ``orch.search("standing facts", max_results=3)`` returns the standing-facts
     document's key inside the top-3.
  3. The standing-facts document ranks ahead of the query-echoing scratch
     fixture (the #160 demotion / #161 promotion hand-off).
"""

import shutil
import tempfile
import unittest

from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource


# The key under which the standing-facts document is stored.  ``HermesDefaultMemorySource.save``
# runs it through ``_safe_key`` which normalises to alphanumerics + hyphens, so the
# on-disk / returned key is guaranteed to be "standing-facts".
STANDING_FACTS_KEY = "standing-facts"

# A realistic standing-facts document: rich, natural, containing "standing facts" several
# times so it scores strongly on content matching while staying far from the literal
# query (avoids the self-match/query-echo halve), and clearly user-authored (no scratch/
# fixture provenance markers).
STANDING_FACTS_CONTENT = {
    "_content": (
        "Standing facts about the user and their environment. "
        "These standing facts are the durable baseline that recall should always "
        "surface first when asked for standing facts: the user trusts autonomous "
        "completion, wants bottom-line verdicts first, treats truth and runtime "
        "proof as core, and keeps OPSEC mission-critical. Standing facts also record "
        "that GitHub operations go through the default profile, code lives in the "
        "canonical workspace, and every merge requires a patch increment."
    ),
    "categories": ["standing-facts", "user-profile"],
    "provenance": "user-authored",
}


class _SandboxedStandingFactsOrchestratorMixin:
    """Shared fixture: hermes_default backed by a temp dir, mempalace disabled.

    Mirrors the #160 ``_SandboxedOrchestratorMixin`` exactly so the promotion
    side and the demotion side exercise the same isolated recall path.
    """

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.orch = MemoryOrchestrator()
        # Overwrite the auto-registered hermes_default (live dir) with a hermetic one.
        self.orch.memory_sources["hermes_default"] = (
            HermesDefaultMemorySource(self._tmp_dir)
        )
        if "mempalace" in self.orch.memory_sources:
            self.orch.disable_source("mempalace")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _seed_store(self):
        """Seed the store with the standing-facts document, the empirically-
        proven query-echoing scratch/fixture outranker, and two genuinely-weaker
        single-term noise entries.

        The scratch fixture is seeded through the source directly so its
        ``categories: ["scratch"]`` tag persists — the orchestrator's strict
        category whitelist (BUG_2) would reject "scratch" at ``orch.save()``
        time; that rejection is precisely why the real scratch payload survives
        un-demoted in the store (see issue #161 for that coupled gap).

        The two noise entries match only ONE of the two query terms, so they sit
        below the standing-facts document's two-term (full-coverage) score —
        realistic background noise, not competing relevance.
        """
        src = self.orch.memory_sources["hermes_default"]
        src.save(STANDING_FACTS_KEY, STANDING_FACTS_CONTENT)
        # The classic empirical outranker: a scratch fixture whose content
        # echoes "standing facts" (that echo is how it outranked the real doc).
        src.save(
            "quick-brown-fox",
            {"_content": "standing facts quick brown fox example",
             "categories": ["scratch"]},
        )
        # Weaker single-term noise: each matches only one query term, so it
        # scores ~0.5 — clearly below the standing-facts doc's full-coverage
        # score — but still recalled, so top-3 is genuinely non-vacuous.
        src.save("note-a", {"_content": "the standing facts are kept in one note"})
        src.save("note-b", {"_content": "facts about the environment we maintain"})


class TestStandingFactsRecall(_SandboxedStandingFactsOrchestratorMixin,
                              unittest.TestCase):
    """GH-161 acceptance: the standing-facts doc reaches top-3 for its query."""

    def test_standing_facts_doc_is_in_top3(self):
        """The standing-facts document must appear inside the top-3 window for
        its canonical query, even with a contesting scratch fixture + decoys."""
        self._seed_store()

        results = self.orch.search("standing facts", max_results=3)
        keys = [r["key"] for r in results]

        self.assertIn(
            STANDING_FACTS_KEY,
            keys,
            "standing-facts doc must be recalled into top-3; got: %s" % keys,
        )
        # It must not merely be present *somewhere* behind the window — it is in
        # the top-3 precisely because the limit was 3.
        self.assertLessEqual(
            keys.index(STANDING_FACTS_KEY),
            2,
            "standing-facts doc must rank within position 0..2; got: %s" % keys,
        )

    def test_standing_facts_doc_outranks_query_echo_scratch_fixture(self):
        """The #160-/#161 hand-off: the standing-facts doc must sit ahead of the
        query-echoing scratch fixture that empirically outranked it.  If the
        scratch fixture is demoted out of the window entirely, that is even
        stronger — the standing doc must then lead the recalled set."""
        self._seed_store()

        results = self.orch.search("standing facts", max_results=5)
        keys = [r["key"] for r in results]

        self.assertIn(STANDING_FACTS_KEY, keys)
        self.assertIn("quick-brown-fox", keys, "scratch fixture should still be recalled, just demoted")

        self.assertLess(
            keys.index(STANDING_FACTS_KEY),
            keys.index("quick-brown-fox"),
            "standing-facts doc (%d) must outrank the query-echo scratch fixture (%d): %s"
            % (keys.index(STANDING_FACTS_KEY), keys.index("quick-brown-fox"), keys),
        )


if __name__ == "__main__":
    unittest.main()
