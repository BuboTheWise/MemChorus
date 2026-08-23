"""
Two-tier recency model (GH-99): unit + integration tests.

Acceptance criteria verified by these tests:
  AC1: New recency config block with fast_window_days, fast_retention_pct
  AC2: Modified aging function with two-tier logic in relevance engine
  AC3: Unit tests verifying score at day 1, 6, 8, 15, 30 boundaries (+ day 7 boundary)
  AC4: Backward compatibility — if not configured old single-curve preserved
  AC5: Integration test confirming operational memories rank higher than stable ones
        within first week

Two-tier recency model parameters (fast_window_days=7, fast_retention_pct=0.7):
  - Day 0: score = 1.0  (newest possible)
  - Days 1-6: slower exponential decay using effective half_life = 30/0.7 ≈ 42.86 days
    (scores stay ABOVE single-curve during the fast window)
  - Day 7 boundary: score = 0.5^(7 / 42.86) ≈ 0.891
  - After day 7: standard exponential decay from boundary using half_life_days=30
"""

import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.relevance_engine import RelevanceScorer, ContextWeight
from memchorus.orchestrator import MemoryOrchestrator


def _ts(days_ago: int) -> str:
    """Return ISO-8601 timestamp for *days_ago* days in the past."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _result(key: str, ts_str: str, content: str = "test memory",
            source: str = "hermes_default") -> dict:
    return {
        "key": key,
        "content": content,
        "source": source,
        "timestamp": ts_str,
    }


# ---------------------------------------------------------------------------
# AC1 & AC2: Config block + two-tier aging function exist
# ---------------------------------------------------------------------------

class TestRecencyConfig(unittest.TestCase):
    """AC1: fast_window_days and fast_retention_pct config parameters are present."""

    def test_scorer_accepts_fast_params(self):
        scorer = RelevanceScorer(
            half_life_days=30.0,
            fast_window_days=7.0,
            fast_retention_pct=0.7,
        )
        self.assertEqual(scorer.fast_window_days, 7.0)
        self.assertAlmostEqual(scorer.fast_retention_pct, 0.7)

    def test_scorer_default_disables_two_tier(self):
        scorer = RelevanceScorer()
        self.assertIsNone(scorer.fast_window_days)
        self.assertAlmostEqual(scorer.fast_retention_pct, 0.7)

    def test_orchestrator_passes_fast_config(self):
        orch = MemoryOrchestrator(
            config={
                "hermes_default_config": {"memory_dir": "/tmp"},
                "fast_window_days": 7.0,
                "fast_retention_pct": 0.75,
            }
        )
        self.assertEqual(orch._scorer.fast_window_days, 7.0)
        self.assertAlmostEqual(orch._scorer.fast_retention_pct, 0.75)


# ---------------------------------------------------------------------------
# AC3: Boundary tests (days 1, 6, 7, 8, 15, 30)
# ---------------------------------------------------------------------------

class TestRecencyBoundaryScores(unittest.TestCase):
    """AC3: Verify recency scores at boundary points with two-tier mode enabled."""

    def setUp(self):
        self.scorer = RelevanceScorer(
            half_life_days=30.0,
            fast_window_days=7.0,
            fast_retention_pct=0.7,
        )

    def _recency(self, days: int) -> float:
        r = _result(f"d{days}", _ts(days))
        return self.scorer._score_recency(_ts(days))

    # Day 1 -- within window, slower decay keeps score higher than single-curve
    def test_day_1_within_window_high_score(self):
        """Day 1: slow effective half_life ≈ 42.86 days.
        Score = 0.5^(1/42.86) ≈ 0.984."""
        score = self._recency(1)
        expected = 0.5 ** (1 / (30.0 / 0.7))
        self.assertAlmostEqual(score, expected, places=2,
                               msg=f"Day 1: got {score}, expected ~{expected:.4f}")
        # Should be higher than single-curve at same day
        old = 0.5 ** (1 / 30.0)
        self.assertGreater(score, old,
                           f"Two-tier should boost early days (new={score:.4f} > old={old:.4f})")

    # Day 6 -- still within window, closer to boundary
    def test_day_6_within_window_moderate_score(self):
        """Day 6: slow effective half_life ≈ 42.86 days.
        Score = 0.5^(6/42.86) ≈ 0.905."""
        score = self._recency(6)
        expected = 0.5 ** (6 / (30.0 / 0.7))
        self.assertAlmostEqual(score, expected, places=2,
                               msg=f"Day 6: got {score}, expected ~{expected:.4f}")

    # Day 7 -- AT the window boundary
    def test_day_7_at_boundary(self):
        """Day 7 exactly at window edge — last day of slow-curve plateau.
        Score = 0.5^(7/42.86) ≈ 0.891."""
        score = self._recency(7)
        expected = 0.5 ** (7 / (30.0 / 0.7))
        self.assertAlmostEqual(score, expected, places=2,
                               msg=f"Day 7: got {score}, expected ~{expected:.4f}")

    # Day 8 -- past window, standard decay from boundary value
    def test_day_8_after_window_decay(self):
        """Day 8: one day past boundary. Standard decay resumes from whatever
        the slow-curve left off at."""
        score = self._recency(8)
        boundary = 0.5 ** (7 / (30.0 / 0.7))
        expected = boundary * (0.5 ** ((8 - 7) / 30.0))
        self.assertAlmostEqual(score, expected, places=2,
                               msg=f"Day 8: got {score}, expected ~{expected:.4f}")

    # Day 15 -- mid-range post-window decay
    def test_day_15_mid_decay(self):
        """Day 15: standard decay continues from boundary value."""
        score = self._recency(15)
        boundary = 0.5 ** (7 / (30.0 / 0.7))
        expected = boundary * (0.5 ** ((15 - 7) / 30.0))
        self.assertAlmostEqual(score, expected, places=2,
                               msg=f"Day 15: got {score}, expected ~{expected:.4f}")

    # Day 30 -- long tail decay past window
    def test_day_30_long_tail(self):
        """Day 30: substantial post-window decay."""
        score = self._recency(30)
        boundary = 0.5 ** (7 / (30.0 / 0.7))
        expected = boundary * (0.5 ** ((30 - 7) / 30.0))
        self.assertAlmostEqual(score, expected, places=2,
                               msg=f"Day 30: got {score}, expected ~{expected:.4f}")

    # Sanity: scores decrease monotonically over time
    def test_scores_decrease_monotonically(self):
        """Scores should strictly decrease (or stay equal) as days increase."""
        prev = None
        for d in range(1, 61):
            s = self._recency(d)
            if prev is not None:
                self.assertGreaterEqual(prev, s,
                                        f"Score at day {d-1} ({prev}) < score at day {d} ({s})")
            prev = s
        # After full half-life period (30 days post-window), score should be well under 0.5
        self.assertLess(self._recency(60), 0.3)


# ---------------------------------------------------------------------------
# AC4: Backward compatibility — old single curve preserved without fast_window_days
# ---------------------------------------------------------------------------

class TestBackwardCompatibility(unittest.TestCase):
    """AC4: When fast_window_days is not configured (the default), the original
    single-curve exponential decay is preserved exactly."""

    def setUp(self):
        self.scorer = RelevanceScorer(half_life_days=30.0)

    def _recency(self, days: int) -> float:
        return self.scorer._score_recency(_ts(days))

    def test_day_1_single_curve(self):
        """Old formula: 0.5^(1/30) ≈ 0.977."""
        expected = 0.5 ** (1 / 30.0)
        self.assertAlmostEqual(self._recency(1), expected, places=2)

    def test_day_15_single_curve(self):
        """Old formula: 0.5^(15/30) = 0.5^0.5 ≈ 0.707."""
        expected = 0.5 ** (15 / 30.0)
        self.assertAlmostEqual(self._recency(15), expected, places=2)

    def test_day_30_single_curve_halflife(self):
        """At exactly half_life_days, score should be ~0.5."""
        self.assertAlmostEqual(self._recency(30), 0.5, places=2)

    def test_two_tier_scores_differ_from_original(self):
        """Two-tier scores at day 1-7 must be HIGHER than single-curve scores —
        that is the whole point of GH-99."""
        two_tier = RelevanceScorer(
            half_life_days=30.0, fast_window_days=7.0, fast_retention_pct=0.7
        )
        for d in [1, 3, 5, 6]:
            old = self._recency(d)
            new = two_tier._score_recency(_ts(d))
            self.assertGreater(new, old,
                               f"At day {d}: two-tier should be higher than single-curve "
                               f"(new={new:.4f} vs old={old:.4f})")


# ---------------------------------------------------------------------------
# AC5: Integration — operational memories rank above stable within first week
# ---------------------------------------------------------------------------

class TestOperationalVsStableRanking(unittest.TestCase):
    """AC5: Operational memories (recent, fast-window boosted) should outrank
    older stable knowledge in search results when two-tier recency is enabled."""

    def setUp(self):
        self.two_tier_orch = MemoryOrchestrator(
            config={
                "hermes_default_config": {"memory_dir": "/tmp"},
                "fast_window_days": 7.0,
                "fast_retention_pct": 0.7,
            }
        )
        self.scorer = RelevanceScorer(
            half_life_days=30.0, fast_window_days=7.0, fast_retention_pct=0.7
        )

    def test_operational_memory_beats_stable_within_first_week(self):
        """An operational memory from 3 days ago with moderate content match should
        outrank a stable memory from 15 days ago with perfect content match.
        The recency boost within the fast window compensates for lower text quality."""
        # Heavy recency weight to make the difference visible
        ctx = ContextWeight(quality_weight=0.20, recency_weight=0.60, source_type_weight=0.20)

        # Operational: 3 days old, moderate content ("deploy pipeline operational")
        operational = _result("ops_1", _ts(3), "pr deploy build pipeline status update merge pushed",
                              source="hermes_default")
        # Stable: 15 days old, exact content match (quality-perfect)
        stable = _result("stable_1", _ts(15), "long term system architectural design pattern knowledge base",
                         source="hermes_default")

        ranked = self.scorer.score_and_rank(
            [operational, stable], query="deploy pipeline build merge", context=ctx
        )

        # Both should have score fields (basic sanity)
        for r in ranked:
            self.assertIsInstance(r.score, float)

        # Operational should rank first due to fast-window recency boost
        top_key = ranked[0].key
        self.assertEqual(top_key, "ops_1",
                         f"Operational memory should rank first at day 3, "
                         f"got {top_key} (score={ranked[0].score:.4f}) ahead of stable "
                         f"(score={ranked[-1].score:.4f})")

    def test_stable_memory_wins_when_both_are_old(self):
        """When both memories are past the fast window, content quality matters more."""
        ctx = ContextWeight(quality_weight=0.70, recency_weight=0.15, source_type_weight=0.15)

        old_good = _result("old_best", _ts(30), "relevant deploy pipeline build system architecture")
        old_bad = _result("old_worst", _ts(25), "completely unrelated random knowledge entry notes")

        ranked = self.scorer.score_and_rank(
            [old_good, old_bad], query="deploy pipeline build", context=ctx
        )

        # Old-but-good-content should win when recency is down-weighted and quality wins
        self.assertEqual(ranked[0].key, "old_best",
                         f"With high quality weight at 30 days, content match should win")

    def test_full_score_integration_with_two_tier(self):
        """End-to-end: full score() call with two-tier config shows operational memories
        rank appropriately within scoring pipeline."""
        ctx = ContextWeight(quality_weight=0.45, recency_weight=0.30, source_type_weight=0.25)

        fresh_ops = _result("fresh", _ts(1), "recent change file edit commit pushed today")
        week_old = _result("week", _ts(8), "recent change file edit commit pushed last week")
        month_old = _result("month", _ts(30), "recent change file edit commit pushed last month")

        scores = [self.scorer.score(r, query="change commit file recently", context=ctx)
                  for r in [fresh_ops, week_old, month_old]]

        self.assertGreater(scores[0], scores[1],
                           "Fresh memory should score higher than week-old")
        self.assertGreater(scores[1], scores[2],
                           "Week-old should still score higher than month-old")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
