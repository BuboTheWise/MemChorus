"""Tests for AC-RTB-1.x: per-memory relevance score boosting (v1.9).

Verifies that HitRateTracker utility signals translate into ranking adjustments
at recall time via calibration_engine.boost_factor_for_key() through
relevance_engine.score().

Covers AC-RTB-1.1 through AC-RTB-3 and AC-RTB-4.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tempfile
import unittest

from memchorus.calibration_engine import CalibrationEngine
from memchorus.hit_rate_tracker import HitRateTracker
from memchorus.relevance_engine import RelevanceScorer


class TestBoostFactorComputation(unittest.TestCase):
    """Unit tests for _compute_boost_from_flags( useful, noise )."""

    def test_high_utility_boost(self):
        boost = CalibrationEngine._compute_boost_from_flags(10, 1)
        self.assertGreaterEqual(boost, 2.0)
        self.assertLessEqual(boost, 3.0)

    def test_low_utility_suppress(self):
        boost = CalibrationEngine._compute_boost_from_flags(1, 10)
        self.assertGreaterEqual(boost, 0.5)
        self.assertLessEqual(boost, 0.6)

    def test_zero_data_returns_baseline(self):
        self.assertEqual(CalibrationEngine._compute_boost_from_flags(0, 0), 1.0)

    def test_insufficient_history_stays_at_one(self):
        self.assertAlmostEqual(CalibrationEngine._compute_boost_from_flags(2, 0), 1.0, places=5)

    def test_neutral_zone_in_range(self):
        boost = CalibrationEngine._compute_boost_from_flags(7, 3)
        self.assertGreaterEqual(boost, 0.6)
        self.assertLess(boost, 2.0)

    def test_perfect_hit_rate_maxes_at_three(self):
        boost = CalibrationEngine._compute_boost_from_flags(100, 0)
        self.assertAlmostEqual(boost, 3.0, places=5)

    def test_worst_hit_rate_floors_at_half(self):
        boost = CalibrationEngine._compute_boost_from_flags(0, 100)
        self.assertAlmostEqual(boost, 0.5, places=5)

    def test_boundary_at_threshold(self):
        # Exactly 0.8 hit rate >= high zone
        boost = CalibrationEngine._compute_boost_from_flags(8, 2)
        self.assertGreaterEqual(boost, 2.0)

    def test_monotonic_increase(self):
        pairs = [(1, 5), (2, 5), (3, 5), (5, 5), (7, 3), (8, 2)]
        boosts = [CalibrationEngine._compute_boost_from_flags(u, n) for u, n in pairs]
        for i in range(len(boosts) - 1):
            self.assertGreaterEqual(boosts[i + 1], boosts[i])


TS = "2025-06-01T12:00:00+00:00"


def _result(key: str, content: str) -> dict:
    return {"key": key, "content": content, "source": "manual", "timestamp": TS}


class TestBoostIntegration(unittest.TestCase):

    def test_high_utility_outranks_unproven(self):
        """AC-RTB-2: identical content, different boost -> high utility wins."""
        q = "how to configure kubernetes ingress"
        c = "Configure Kubernetes ingress with annotations and paths."
        r1 = _result("ingress_setup", c)
        r2 = _result("ingress_neutral", c)

        idx = {
            "ingress_setup": {"useful_flags": 10, "noise_flags": 1},
            "ingress_neutral": {"useful_flags": 2, "noise_flags": 0},
        }

        with tempfile.TemporaryDirectory() as td:
            HitRateTracker.reset()
            tracker = HitRateTracker(td)
            tracker._index = idx
            HitRateTracker._instance = tracker

            s = RelevanceScorer()
            s1, s2 = s.score(r1, q), s.score(r2, q)
            self.assertGreater(s1, s2, f"boosted {s1:.4f} > unproven {s2:.4f}")

    def test_low_utility_downranked(self):
        """AC-RTB-3: low utility scored below fresh/untracked entry."""
        q = "docker networking for beginners"
        c = "Docker networking setup with bridges and overlays."
        r1 = _result("docker_net_broken", c)
        r2 = _result("docker_net_fresh", c)

        idx = {"docker_net_broken": {"useful_flags": 1, "noise_flags": 20}}

        with tempfile.TemporaryDirectory() as td:
            HitRateTracker.reset()
            tracker = HitRateTracker(td)
            tracker._index = idx
            HitRateTracker._instance = tracker

            s = RelevanceScorer()
            s1, s2 = s.score(r1, q), s.score(r2, q)
            self.assertLess(s1, s2, f"low-utility {s1:.4f} < fresh {s2:.4f}")

    def test_scores_remain_bounded(self):
        """AC-RTB-3: scores clamped to [0, 1] even under max boost."""
        q = "redis caching best practices"
        c = "Redis caching layer performance tuning and eviction policies."
        r = _result("redis_max", c)

        idx = {"redis_max": {"useful_flags": 100, "noise_flags": 0}}

        with tempfile.TemporaryDirectory() as td:
            HitRateTracker.reset()
            tracker = HitRateTracker(td)
            tracker._index = idx

            s = RelevanceScorer()
            score = s.score(r, q)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)


class TestGracefulDegradation(unittest.TestCase):

    def test_fallback_when_tracker_gone(self):
        """Scoring succeeds and stays bounded without tracker data."""
        s = RelevanceScorer()
        r = _result("unknown_key", "Redis performance tuning notes.")
        score = s.score(r, "redis")
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()