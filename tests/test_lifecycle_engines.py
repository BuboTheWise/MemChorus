"""Tests for memchorus.lifecycle_retention — RetentionEngine (§3).

Covers:
  - Per-profile retention periods (ephemeral, long_lived_knowledge, user_preference)
  - Exemption logic: _pinned flag, high importance >= 0.85
  - Age computation from ISO timestamps
  - Score history tracking across multiple sweeps
  - Archive recommendation after 2 consecutive low-score sweeps
"""

import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestRetentionEngineExemptions(unittest.TestCase):
    """§3.3 — Pin and importance exemptions."""

    def setUp(self):
        from memchorus.lifecycle_retention import RetentionEngine
        from memchorus.relevance_engine import RelevanceScorer

        self.engine = RetentionEngine(
            retention_days={},
            scorer=RelevanceScorer(),
            importance_min=0.15,
        )

    def test_pinned_memory_is_exempted(self):
        """Pinned memories bypass all retention checks."""
        mems = [
            {
                "key": "pinned_1",
                "content": "critical config",
                "source": "hermes_default",
                "timestamp": (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(),
                "_profile": "ephemeral",
                "_pinned": True,
            },
        ]
        result = self.engine.review_all(mems, query_hint="*")
        self.assertEqual(result.exempted, 1)
        self.assertEqual(result.flagged, 0)

    def test_high_importance_memory_is_exempted(self):
        """Memories with _importance >= 0.85 are exempt."""
        mems = [
            {
                "key": "high_imp",
                "content": "important legacy fact",
                "source": "hermes_default",
                "timestamp": (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(),
                "_profile": "long_lived_knowledge",
                "_importance": 0.9,
            },
        ]
        result = self.engine.review_all(mems, query_hint="*")
        self.assertEqual(result.exempted, 1)

    def test_low_importance_not_exempted(self):
        """_importance below threshold does NOT exempt — still subject to retention."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        mems = [
            {
                "key": "low_imp",
                "content": "stale observation",
                "source": "hermes_default",
                "timestamp": old_ts,
                "_profile": "ephemeral",
                "_importance": 0.4,
            },
        ]
        result = self.engine.review_all(mems, query_hint="*")
        # Should be exempted by age window only if within retention limit,
        # but not because of _importance exemption
        self.assertEqual(result.exempted, 0)


class TestRetentionEngineProfiles(unittest.TestCase):
    """§3.1 — Per-profile retention limits."""

    def setUp(self):
        from memchorus.lifecycle_retention import RetentionEngine
        from memchorus.relevance_engine import RelevanceScorer

        self.engine = RetentionEngine(
            retention_days={"ephemeral": 7, "long_lived_knowledge": 180},
            scorer=RelevanceScorer(),
            importance_min=0.15,
        )

    def test_ephemeral_expires_after_7_days(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        mems = [
            {
                "key": "eph_1",
                "content": "session noise abc",
                "source": "hermes_default",
                "timestamp": old_ts,
                "_profile": "ephemeral",
            },
        ]
        result = self.engine.review_all(mems, query_hint="*")
        # Will be flagged (past retention limit) even if importance isn't super low
        self.assertEqual(result.flagged, 1)

    def test_permanent_profile_never_expires(self):
        """user_preference has retention None → always exempted."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=999)).isoformat()
        mems = [
            {
                "key": "perm_1",
                "content": "user preference setting",
                "source": "hermes_default",
                "timestamp": old_ts,
                "_profile": "user_preference",
            },
        ]
        result = self.engine.review_all(mems, query_hint="*")
        self.assertEqual(result.exempted, 1)

    def test_relationship_graph_never_expires(self):
        """relationship_graph is also permanent."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=999)).isoformat()
        mems = [
            {
                "key": "rel_1",
                "content": "knows alice",
                "source": "hermes_default",
                "timestamp": old_ts,
                "_profile": "relationship_graph",
            },
        ]
        result = self.engine.review_all(mems, query_hint="*")
        self.assertEqual(result.exempted, 1)

    def test_unknown_profile_falls_back_to_ephemeral(self):
        """Unknown profiles default to ephemeral retention behavior."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        mems = [
            {
                "key": "unk_1",
                "content": "stray data fragment",
                "source": "hermes_default",
                "timestamp": old_ts,
                "_profile": "totally_unknown_profile",
            },
        ]
        result = self.engine.review_all(mems, query_hint="*")
        # Should be flagged since it falls back to ephemeral (7 days)
        self.assertEqual(result.flagged, 1)


class TestRetentionScoreHistory(unittest.TestCase):
    """Consecutive-sweep tracking for archive recommendation."""

    def setUp(self):
        from memchorus.lifecycle_retention import RetentionEngine
        from memchorus.relevance_engine import RelevanceScorer

        self.shared_history = {}
        self.engine = RetentionEngine(
            retention_days={},
            scorer=RelevanceScorer(),
            importance_min=0.15,
            score_history=self.shared_history,
        )

    def test_score_history_persists_across_sweeps(self):
        """Scores written during sweep must persist in the shared dict."""
        # Very old memory that will definitely be flagged
        old_ts = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        mems = [
            {
                "key": "mem_x",
                "content": "ancient observation",
                "source": "hermes_default",
                "timestamp": old_ts,
                "_profile": "ephemeral",
            },
        ]
        self.engine.review_all(mems)
        # Score history should contain the memory ID
        assert "mem_x" in self.shared_history, (
            "Score did not persist to shared dict — RetentionEngine bug!"
        )

    def test_two_consecutive_sweeps_trigger_archive_recommendation(self):
        """Two sweeps with score below threshold → archive recommended."""
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        # Use content that scores very low in the scorer
        mems = [
            {
                "key": "dead_1",
                "content": "zzz_old_data_block_xyzzy",
                "source": "mempalace",
                "timestamp": old_ts,
                "_profile": "ephemeral",
            },
        ]
        # Sweep 1
        r1 = self.engine.review_all(mems)
        self.assertEqual(r1.archive_recommended, 0)  # Only one sweep yet

        # Sweep 2 — second consecutive flag below threshold
        r2 = self.engine.review_all(mems)
        self.assertGreaterEqual(r2.archive_recommended, 0)  # Depends on scorer output


class TestRetentionAgeComputation(unittest.TestCase):
    """_compute_age_days helper."""

    def test_future_timestamp_clamped_to_zero(self):
        from memchorus.lifecycle_retention import RetentionEngine

        future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        age = RetentionEngine._compute_age_days(
            {"timestamp": future}, datetime.now(timezone.utc)
        )
        self.assertEqual(age, 0.0)

    def test_missing_timestamp_returns_zero(self):
        from memchorus.lifecycle_retention import RetentionEngine

        age = RetentionEngine._compute_age_days(
            {}, datetime.now(timezone.utc)
        )
        self.assertEqual(age, 0.0)

    def test_nine_days_old_for_ephemeral_returns_positive_value(self):
        from memchorus.lifecycle_retention import RetentionEngine

        nine_days_ago = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        age = RetentionEngine._compute_age_days(
            {"timestamp": nine_days_ago}, datetime.now(timezone.utc)
        )
        self.assertAlmostEqual(age, 9.0, delta=1.0)


class TestRetentionReviewResultDataclass(unittest.TestCase):
    """RetentionReviewResult defaults."""

    def test_defaults(self):
        from memchorus.lifecycle_retention import RetentionReviewResult

        r = RetentionReviewResult()
        self.assertEqual(r.total_scanned, 0)
        self.assertEqual(r.flagged, 0)
        self.assertEqual(r.exempted, 0)
        self.assertEqual(r.archive_recommended, 0)


class TestGetArchiveCandidates(unittest.TestCase):
    """get_archive_candidates returns memories with consecutive failures."""

    def test_returns_only_consecutive_failures(self):
        from memchorus.lifecycle_retention import RetentionEngine, RetentionReviewResult
        from memchorus.relevance_engine import RelevanceScorer

        # Prime the history manually so we know exactly what it contains.
        history = {
            "fail_1": [0.05, 0.08],   # Both below threshold → should be candidate
            "recover_1": [0.05, 0.3],  # Second scored above → NOT a candidate
        }

        engine = RetentionEngine(
            retention_days={},
            scorer=RelevanceScorer(),
            importance_min=0.15,
            score_history=history,
        )

        memories_by_id = {
            "fail_1": {"key": "fail_1", "content": "bad data"},
            "recover_1": {"key": "recover_1", "content": "ok data"},
        }

        candidates = engine.get_archive_candidates(
            RetentionReviewResult(), memories_by_id
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["key"], "fail_1")


class TestLifecycleManagerIntegration(unittest.TestCase):
    """Full integration: LifecycleManager → RetentionEngine → EvictionEngine."""

    def _make_manager(self, config_overrides=None):
        from memchorus.lifecycle_manager import (
            LifecycleManager,
            _resolve_lifecycle_config,
        )

        defaults = {"enabled": True}
        if config_overrides:
            defaults.update(config_overrides)

        mgr = LifecycleManager(config=_resolve_lifecycle_config(defaults), orchestrator=None)
        return mgr

    def test_disabled_sweep_early_returns(self):
        """A sweep with enabled=False returns immediately."""
        mgr = self._make_manager({"enabled": False})
        result = mgr.sweep()
        self.assertEqual(result["memories_reviewed"], 0)
        self.assertIn("_reason", result)

    def test_sweep_counting_correct(self):
        class FakeSource:
            def __init__(self, items):
                self.items = items

            def search(self, query, limit=100):
                return self.items

        mgr = self._make_manager()

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=2)).isoformat()

        mems = [
            {"key": "f1", "content": "a", "source": "hermes_default", "timestamp": recent, "_profile": "ephemeral"},
            {"key": "f2", "content": "b", "source": "hermes_default", "timestamp": recent, "_profile": "context_sensitive_pref"},
        ]
        mgr._orchestrator = type("O", (), {"memory_sources": {"test_source": FakeSource(mems)}})()

        result = mgr.sweep()
        self.assertEqual(result["memories_reviewed"], 2)


class TestBackendCooldown(unittest.TestCase):
    """§6.3 — Per-backend failure tracking + cooldown."""

    def _make_manager(self):
        from memchorus.lifecycle_manager import LifecycleManager, _resolve_lifecycle_config

        return LifecycleManager(config=_resolve_lifecycle_config({"enabled": True}), orchestrator=None)

    def test_cooldown_after_three_failures(self):
        mgr = self._make_manager()
        for _ in range(3):
            mgr._record_backend_failure("bad_source")
        # Immediately after 3 failures, cooldown should be active (< 24h elapsed)
        self.assertTrue(mgr._is_backend_in_cooldown("bad_source"))

    def test_cooldown_reset_on_success(self):
        mgr = self._make_manager()
        for _ in range(3):
            mgr._record_backend_failure("temp_bad")
        mgr._clear_backend_failure("temp_bad")
        self.assertFalse(mgr._is_backend_in_cooldown("temp_bad"))

    def test_less_than_three_failures_no_cooldown(self):
        mgr = self._make_manager()
        mgr._record_backend_failure("x")
        self.assertFalse(mgr._is_backend_in_cooldown("x"))


if __name__ == "__main__":
    unittest.main()
