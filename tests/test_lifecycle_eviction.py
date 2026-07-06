"""Tests for memchorus.lifecycle_eviction — EvictionEngine (§4).

Covers:
  - EvictionCandidate dataclass required fields
  - PurgeReason enum members
  - evaluate_all() pipeline with archive/purge callbacks
  - AND-logic triggers (age + importance)
  - High-importance skip logic
  - Callback invocation with correct arguments
  - Callback failure graceful degradation
  - Archive penalty application
  - Duplicate content clustering
"""

import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestEvictionCandidate(unittest.TestCase):
    """EvictionCandidate dataclass required fields."""

    def test_candidate_creation(self):
        from memchorus.lifecycle_eviction import EvictionCandidate, PurgeReason

        c = EvictionCandidate(
            memory_id="test_1",
            source="hermes_default",
            profile="ephemeral",
            content="some observation",
            prev_score=0.3,
            reason=PurgeReason.AGE,
        )
        self.assertEqual(c.memory_id, "test_1")
        self.assertEqual(c.source, "hermes_default")
        self.assertEqual(c.profile, "ephemeral")
        self.assertEqual(c.prev_score, 0.3)


class TestPurgeReason(unittest.TestCase):
    """PurgeReason enum members."""

    def test_all_reasons_exist(self):
        from memchorus.lifecycle_eviction import PurgeReason

        values = [r.value for r in PurgeReason]
        self.assertIn("AGE", values)
        self.assertIn("IMPORTANCE", values)
        self.assertIn("DUPE_MERGE", values)
        self.assertIn("USER_PURGE", values)
        self.assertIn("STRUCTURAL", values)


class TestEvictionEngineBasic(unittest.TestCase):
    """Core evaluate_all pipeline."""

    def setUp(self):
        from memchorus.lifecycle_eviction import (
            EvictionCandidate,
            EvictionEngine,
            PurgeReason,
        )

        self.engine = EvictionEngine(
            importance_min=0.15,
            duplicate_cluster_max=3,
            similarity_min=0.75,
            archive_grace_days=30,
            archive_score_penalty=-0.7,
        )
        self.now = datetime.now(timezone.utc)
        self.old_ts = (self.now - timedelta(days=120)).isoformat()

    def _make_candidate(self, idx, prev_score=0.1):
        from memchorus.lifecycle_eviction import EvictionCandidate, PurgeReason

        return EvictionCandidate(
            memory_id=f"cand_{idx}",
            source="hermes_default",
            profile="ephemeral",
            content=f"test observation number {idx}",
            prev_score=prev_score,
            reason=PurgeReason.AGE,
            timestamp=self.old_ts,
        )

    def _make_callbacks(self):
        archived = {}
        purged_ids = []
        audit_entries = []

        def arch_fn(memory_id, content, source, prev_score):
            archived[memory_id] = {"source": source}
            return True

        def purg_fn(memory_id, source):
            purged_ids.append(memory_id)
            return True

        def audit_fn(**k):
            audit_entries.append(k)

        return arch_fn, purg_fn, audit_fn, archived, purged_ids, audit_entries

    def test_evaluate_empty_list(self):
        """Empty candidate list returns zeroed result."""
        result = self.engine.evaluate_all(
            [],
            archive_fn=lambda *a: True,
            purge_fn=lambda *a: True,
            audit_log=lambda **k: None,
        )
        self.assertEqual(result.archived_count, 0)
        self.assertEqual(result.purged_count, 0)

    def test_candidates_passed_through_pipeline(self):
        """Candidates flow through the pipeline."""
        candidates = [self._make_candidate(i) for i in range(3)]

        arch_fn, purg_fn, audit_fn, *_ = self._make_callbacks()
        result = self.engine.evaluate_all(
            candidates, archive_fn=arch_fn, purge_fn=purg_fn, audit_log=audit_fn
        )
        # All 3 should be handled through the pipeline
        total_handled = result.archived_count + result.purged_count + result.skipped_count
        self.assertEqual(total_handled, 3)

    def test_high_importance_skipped(self):
        """Memories with high score above threshold get skipped."""
        from memchorus.lifecycle_eviction import EvictionCandidate, PurgeReason

        high_imp = EvictionCandidate(
            memory_id="important_1",
            source="hermes_default",
            profile="long_lived_knowledge",
            content="critical knowledge block",
            prev_score=0.9,  # Above threshold of 0.15
            reason=PurgeReason.IMPORTANCE,
            timestamp=self.old_ts,
        )

        result = self.engine.evaluate_all(
            [high_imp],
            archive_fn=lambda *a: True,
            purge_fn=lambda *a: True,
            audit_log=lambda **k: None,
        )
        self.assertEqual(result.skipped_count, 1)


class TestEvictionEngineCallbacks(unittest.TestCase):
    """archive_fn / purge_fn are invoked with correct arguments."""

    def test_archive_callback_receives_memory_id(self):
        from memchorus.lifecycle_eviction import (
            EvictionCandidate,
            EvictionEngine,
            PurgeReason,
        )

        engine = EvictionEngine(
            importance_min=0.7,  # High threshold so low-score items pass through
            duplicate_cluster_max=3,
            similarity_min=0.75,
            archive_grace_days=30,
            archive_score_penalty=-0.7,
        )

        candidates = [
            EvictionCandidate(
                memory_id="cb_test_1",
                source="hermes_default",
                profile="ephemeral",
                content="low value ephemeral data",
                prev_score=0.1,
                reason=PurgeReason.AGE,
            ),
        ]

        called_args = {}

        def archive_fn(memory_id, content, source, prev_score):
            called_args["memory_id"] = memory_id
            return True

        result = engine.evaluate_all(
            candidates,
            archive_fn=archive_fn,
            purge_fn=lambda *a: True,
            audit_log=lambda **k: None,
        )

        # Archive callback should have been called with correct memory_id
        self.assertEqual(called_args.get("memory_id"), "cb_test_1")

    def test_callback_failure_does_not_crash(self):
        """A failing archive callback should not crash the pipeline."""
        from memchorus.lifecycle_eviction import (
            EvictionCandidate,
            EvictionEngine,
            PurgeReason,
        )

        engine = EvictionEngine(
            importance_min=0.7,
            duplicate_cluster_max=3,
            similarity_min=0.75,
            archive_grace_days=30,
            archive_score_penalty=-0.7,
        )

        candidates = [
            EvictionCandidate(
                memory_id="fail_test",
                source="hermes_default",
                profile="ephemeral",
                content="test data for failing callback",
                prev_score=0.1,
                reason=PurgeReason.AGE,
            ),
        ]

        def failing_archive(*a):
            raise RuntimeError("Archive backend down")

        # Should not raise — graceful degradation
        result = engine.evaluate_all(
            candidates,
            archive_fn=failing_archive,
            purge_fn=lambda *a: True,
            audit_log=lambda **k: None,
        )
        self.assertEqual(result.archived_count, 0)


class TestEvictionResultDataclass(unittest.TestCase):
    """EvictionResult defaults."""

    def test_defaults(self):
        from memchorus.lifecycle_eviction import EvictionResult

        r = EvictionResult()
        self.assertEqual(r.archived_count, 0)
        self.assertEqual(r.purged_count, 0)
        self.assertEqual(r.skipped_count, 0)
        self.assertEqual(r.structural_cleanups, 0)


class TestEvictionScorePenalty(unittest.TestCase):
    """Archive score penalty application."""

    def test_penalty_lowers_score(self):
        from memchorus.lifecycle_eviction import EvictionEngine

        engine = EvictionEngine(
            importance_min=0.15,
            duplicate_cluster_max=3,
            similarity_min=0.75,
            archive_grace_days=30,
            archive_score_penalty=-0.7,
        )

        original = 0.8
        penalized = original + engine._archive_penalty
        self.assertAlmostEqual(penalized, 0.1, delta=0.05)


class TestEvictionDuplicateDetection(unittest.TestCase):
    """§4.4 — Duplicate content clustering."""

    def setUp(self):
        from memchorus.lifecycle_eviction import EvictionEngine

        self.engine = EvictionEngine(
            importance_min=0.15,
            duplicate_cluster_max=3,
            similarity_min=0.75,
            archive_grace_days=30,
            archive_score_penalty=-0.7,
        )

    def test_identical_content_flows_through_pipeline(self):
        """Identical content should still flow through the pipeline."""
        from memchorus.lifecycle_eviction import EvictionCandidate, PurgeReason

        candidates = [
            EvictionCandidate(
                memory_id=f"dup_{i}",
                source="hermes_default",
                profile="ephemeral",
                content="the meaning of life is 42",
                prev_score=0.3,
                reason=PurgeReason.AGE,
            )
            for i in range(5)
        ]

        result = self.engine.evaluate_all(
            candidates,
            archive_fn=lambda *a: True,
            purge_fn=lambda *a: True,
            audit_log=lambda **k: None,
        )
        # All 5 should be handled through the pipeline
        total_handled = result.archived_count + result.purged_count + result.skipped_count
        self.assertEqual(total_handled, 5)


class TestEvictionArchiveState(unittest.TestCase):
    """Archive state tracking across phases."""

    def test_archive_state_tracks_archived_memories(self):
        from memchorus.lifecycle_eviction import EvictionCandidate, EvictionEngine, PurgeReason

        engine = EvictionEngine(
            importance_min=0.7,
            duplicate_cluster_max=3,
            similarity_min=0.75,
            archive_grace_days=30,
            archive_score_penalty=-0.7,
        )

        candidates = [
            EvictionCandidate(
                memory_id="as_test_1",
                source="hermes_default",
                profile="ephemeral",
                content="archive state test data",
                prev_score=0.1,
                reason=PurgeReason.AGE,
            ),
        ]

        engine.evaluate_all(
            candidates,
            archive_fn=lambda *a: True,
            purge_fn=lambda *a: True,
            audit_log=lambda **k: None,
        )

        # The memory should be tracked in archive state
        archive_state = engine.archive_state
        self.assertIn("as_test_1", archive_state)


if __name__ == "__main__":
    unittest.main()
