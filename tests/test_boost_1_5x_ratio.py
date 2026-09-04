"""Smoke test: AC-RTB acceptance — boosted key scores exceed neutral by >= 1.5x.

The broader test suite lives in tests/test_memory_scorer_boost.py;
this file focuses solely on the 1.5x ratio threshold."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memchorus.relevance_engine import RelevanceScorer
from memchorus.hit_rate_tracker import HitRateTracker


TS = "2025-06-01T12:00:00+00:00"


def _r(key, content):
    return {"key": key, "content": content, "source": "manual", "timestamp": TS}


class TestBoostRatio(unittest.TestCase):

    def test_boosted_exceeds_neutral_by_1_5x(self):
        """Acceptance criterion: high-utility memory scores >= 1.5x over unproven."""
        query = "how to configure kubernetes ingress"
        content = "Configure Kubernetes ingress with annotations and paths."

        r_boosted = _r("ingress_setup", content)
        r_neutral = _r("ingress_neutral", content)

        with tempfile.TemporaryDirectory() as td:
            HitRateTracker.reset()
            tracker = HitRateTracker(td)
            # 10 useful / 1 noise -> hit_rate=0.91 -> boost ~2.45
            tracker._index = {
                "ingress_setup": {"useful_flags": 10, "noise_flags": 1},
                # neutral key has no history -> boost defaults to 1.0
            }
            HitRateTracker._instances[os.path.realpath(td)] = tracker

            # Route the no-arg get_instance() (used by calibration/scorer) to td
            with patch.object(HitRateTracker, "_default_memory_dir", return_value=td):
                scorer = RelevanceScorer()
                s_boosted = scorer.score(r_boosted, query)
                s_neutral = scorer.score(r_neutral, query)

            ratio = s_boosted / max(s_neutral, 1e-9)
            self.assertGreaterEqual(
                ratio,
                1.5,
                f"boosted/neutral ratio {ratio:.4f}x must be >= 1.5x "
                f"(boosted={s_boosted:.4f}, neutral={s_neutral:.4f})",
            )


if __name__ == "__main__":
    unittest.main()
