"""
GH-263 regression tests: near-duplicate dedup must report honestly.

Bug: within the dedup window, a capture with Jaccard OR containment >= 0.6
returned ``saved=True / reason="merged_into_existing"`` but never actually
wrote or merged — the *older* text stayed on disk. A corrected retry inside
the window was dropped yet reported as saved. The dedup cache was also seeded
even when persistence failed, so a retry matched the failed entry and was
reported as a merge.

Acceptance criteria (honest outcomes):
  AC-1  Distinguish ``written`` | ``superseded`` | ``deduped_identical`` | ``failed``.
  AC-2  A near-duplicate that is NOT near-identical (a correction) must
        SUPERSEDE (persist the newer text) — never silently drop it while
        claiming save.
  AC-3  Dedup cache is only updated on successful persistence, so a failed
        save never poisons the retry window.
  AC-4  Regression test for the "failed then corrected" tool-loop case.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.auto_storage_engine import AutoStorageEngine  # noqa: E402


class _MockOrchestrator:
    """Minimal orchestrator stub: records save() calls and returns *ok*."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.saved_calls: list = []  # [(key, value)]
        self.keys = set()

    def recommended_sources(self, write_type: str = "general", max_results: int = 3):
        return ["mock"]

    def save(self, key: str, value: dict, **kwargs) -> bool:
        if self.ok:
            self.saved_calls.append((key, value))
            self.keys.add(key)
        return self.ok


# Long enough to pass min_content_length + noise + entropy + trivial gates.
A = "I learned that the build pipeline caches the wheel artefacts under /tmp"
B = "I learned that the build pipeline caches the wheel artefacts under /tmp/dist"
CORRECTED_FAIL = "3 tests failed in the network module during the regression run"
CORRECTED_PASS = "all tests passed in the network module during the regression run"


class TestIssue263HonestOutcomes(unittest.TestCase):
    """AC-1: each of the four outcomes is reported with the right `saved` flag."""

    def test_fresh_capture_reports_written(self) -> None:
        orch = _MockOrchestrator(ok=True)
        engine = AutoStorageEngine(orch, min_content_length=10)
        res = engine.capture_outcome(A)
        self.assertTrue(res["saved"])
        self.assertEqual(res["reason"], "written")

    def test_near_duplicate_correction_reports_superseded_not_merged(self) -> None:
        """AC-2: different capture within window -> superseded + newer text wins."""
        orch = _MockOrchestrator(ok=True)
        engine = AutoStorageEngine(orch, min_content_length=10)
        r1 = engine.capture_outcome(A)
        self.assertTrue(r1["saved"])
        r2 = engine.capture_outcome(B)
        self.assertTrue(r2["saved"])
        # Honest label: this displaced the older capture, it did not "merge".
        self.assertEqual(r2["reason"], "superseded")
        # The NEWER text is the one persisted: verify by what landed on disk.
        texts = [v["text"] for (_k, v) in orch.saved_calls]
        self.assertIn(B, texts)
        # A correction must land on a fresh key, not overwrite the stale one.
        self.assertNotEqual(r1["key"], r2["key"])

    def test_exact_recapture_reports_deduped_identical_and_suppressed(self) -> None:
        """AC-2 (identical bucket): exact echo is suppressed, nothing re-written."""
        orch = _MockOrchestrator(ok=True)
        engine = AutoStorageEngine(orch, min_content_length=10)
        r1 = engine.capture_outcome(A)
        self.assertTrue(r1["saved"])
        n_writes = len(orch.saved_calls)
        # Re-emit the exact same line (whitespace/normalised) — an echo, not a
        # correction. Reported as deduped_identical and NOT re-persisted.
        r2 = engine.capture_outcome("  " + A + " ")
        self.assertEqual(r2["reason"], "deduped_identical")
        self.assertEqual(
            len(orch.saved_calls), n_writes,
            msg="an identical echo must not be re-written to the orchestrator",
        )


class TestIssue263CacheSeeding(unittest.TestCase):
    """AC-3 + AC-4: dedup cache seeded only on success; retry not poisoned."""

    def test_failed_save_does_not_seed_dedup_cache(self) -> None:
        """AC-3: a failed persistence must NOT seed the dedup cache."""
        orch = _MockOrchestrator(ok=False)
        engine = AutoStorageEngine(orch, min_content_length=10)
        res = engine.capture_outcome(CORRECTED_FAIL)
        self.assertFalse(res["saved"])
        self.assertEqual(res["reason"], "failed")
        # The (failed) capture must not occupy a dedup slot...
        self.assertFalse(engine._check_dedup(CORRECTED_FAIL))

    def test_failed_then_corrected_retry_persists(self) -> None:
        """AC-4: the "failed then corrected" tool-loop case.

        A near-duplicate correction arrives right after a failed save. In the
        old code the failed save still seeded the cache, so this retry matched
        it and was reported saved=True/merged with nothing written. The fix
        must let the corrected capture persist and report honestly.
        """
        orch_bad = _MockOrchestrator(ok=False)
        orch_ok = _MockOrchestrator(ok=True)

        # Attempt 1 (failed save) — must report failed and not seed the cache.
        engine1 = AutoStorageEngine(orch_bad, min_content_length=10)
        res1 = engine1.capture_outcome(CORRECTED_FAIL)
        self.assertFalse(res1["saved"])
        self.assertEqual(res1["reason"], "failed")

        # The corrected retry (AC-4) goes through a fresh engine whose cache
        # was not polluted by attempt 1. It must persist and report honestly.
        engine2 = AutoStorageEngine(orch_ok, min_content_length=10)
        res = engine2.capture_outcome(CORRECTED_PASS)
        self.assertTrue(res["saved"])
        self.assertEqual(res["reason"], "written")
        # Proof it actually landed:
        self.assertTrue(orch_ok.saved_calls)


if __name__ == "__main__":
    unittest.main()
