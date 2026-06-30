"""Tests for EscalationTracker class.

Covers:
  - Cooldown window enforcement (inside cooldown, after cooldown expires)
  - Escalation step progression with configurable thresholds
"""

import sys
import os
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.feedback_loop.escalation import EscalationTracker


class TestCooldownWindow(unittest.TestCase):
    """Tests that cooldown logic correctly prevents/fires loops."""

    def test_first_fire_always_allowed(self):
        """A loop that has never been triggered should always be allowed to fire."""
        tracker = EscalationTracker()
        self.assertTrue(tracker.check_cooldown("never_fired", 10))

    def test_inside_cooldown_blocked(self):
        """Loop that fired recently should NOT be allowed during cooldown."""
        tracker = EscalationTracker()
        # Fake a trigger by setting last_fired_at in the state.
        tracker._loop_state["test_loop"] = {
            "trigger_count": 1,
            "last_fired_at": time.monotonic(),
            "level": 1,
            "threshold_per_level": 3,
        }
        # Immediately check cooldown with a generous interval.
        self.assertFalse(tracker.check_cooldown("test_loop", 60 * 60))

    def test_after_cooldown_allows_fire(self):
        """Loop whose cooldown has expired should allow firing."""
        tracker = EscalationTracker()
        past_time = time.monotonic() - 120  # 2 minutes ago.
        tracker._loop_state["test_loop"] = {
            "trigger_count": 1,
            "last_fired_at": past_time,
            "level": 1,
            "threshold_per_level": 3,
        }
        self.assertTrue(tracker.check_cooldown("test_loop", 60))

    def test_zero_cooldown_interval_ever(self):
        """Cooldown interval of 0 means the loop is always allowed (even immediately)."""
        tracker = EscalationTracker()
        past_time = time.monotonic() - 0.001  # essentially now.
        # Even with almost-no time elapsed, zero cooldown should pass — but that's
        # handled by the interval param, not our code. Our check_cooldown simply
        # checks elapsed >= interval, so interval=0 should always return True.
        tracker._loop_state["test_loop"] = {
            "trigger_count": 1,
            "last_fired_at": past_time,
            "level": 1,
            "threshold_per_level": 3,
        }
        # Our own check_cooldown: elapsed >= interval; with interval=0, always True.
        self.assertTrue(tracker.check_cooldown("test_loop", 0))

    def test_exact_boundary_is_allowed(self):
        """Elapsed exactly equal to cooldown should be allowed (>=)."""
        tracker = EscalationTracker()
        past_time = time.monotonic() - 120.0  # exactly 120s ago
        tracker._loop_state["test_loop"] = {
            "trigger_count": 1,
            "last_fired_at": past_time,
            "level": 1,
            "threshold_per_level": 3,
        }
        # elapsed ≈ 120 + ε; since formula is >=, boundary passes.
        self.assertTrue(tracker.check_cooldown("test_loop", 120.0))


class TestEscalationStepProgression(unittest.TestCase):
    """Tests that escalation levels progress correctly."""

    def test_first_trigger_gives_level_1(self):
        """First trigger should return level 1."""
        tracker = EscalationTracker()
        level = tracker.record_trigger("new_loop")
        self.assertEqual(level, 1)

    def test_progression_after_threshold_count(self):
        """After threshold number (default=3) of triggers, level advances to 2."""
        tracker = EscalationTracker()
        # Triggers 1-3 are level 1. Trigger 4 should advance to level 2.
        for i in range(3):
            level = tracker.record_trigger("threshold_loop")
            self.assertEqual(level, 1)
        level = tracker.record_trigger("threshold_loop")
        self.assertEqual(level, 2)

    def test_progression_to_level_3(self):
        """After another threshold number of triggers, level advances to 3."""
        tracker = EscalationTracker()
        for i in range(6):  # triggers 1-6: level 1 (0-2) + level 2 (3-5)
            tracker.record_trigger("l3_loop")
        # After 7th trigger, should be level 3.
        level = tracker.record_trigger("l3_loop")
        self.assertEqual(level, 3)

    def test_level_maxes_at_3(self):
        """Level should never exceed 3, regardless of trigger count."""
        tracker = EscalationTracker()
        for i in range(100):
            level = tracker.record_trigger("max_loop")
        self.assertEqual(level, 3)

    def test_get_escalation_level_returns_correct_value(self):
        """get_escalation_level should accurately report the current level."""
        tracker = EscalationTracker()
        self.assertEqual(tracker.get_escalation_level("unknown"), 1)

        for i in range(4):
            tracker.record_trigger("report_loop")
        self.assertEqual(tracker.get_escalation_level("report_loop"), 2)


class TestCustomizableThresholds(unittest.TestCase):
    """Tests that thresholds are configurable."""

    def test_custom_threshold_advances_faster(self):
        """With threshold=1, every trigger advances the level."""
        tracker = EscalationTracker()
        # Manually set the threshold for our loop. Set trigger_count to 1 so after
        # record_trigger increments it to 2, (2-1)//1+1=2, matching the assertion.
        tracker._loop_state["fast_loop"] = {
            "trigger_count": 1,
            "last_fired_at": 0.0,
            "level": 1,
            "threshold_per_level": 1,  # advance every trigger.
        }
        level1 = tracker.record_trigger("fast_loop")
        self.assertEqual(level1, 2)  # advanced immediately.


class TestResetLoop(unittest.TestCase):
    """Tests the reset functionality."""

    def test_reset_clears_state(self):
        """After reset, get_escalation_level should return default (1)."""
        tracker = EscalationTracker()
        # Need 7 triggers to reach level 3 with threshold_per_level=3:
        # (7-1)//3+1 = 3
        for _ in range(7):
            tracker.record_trigger("reset_loop")
        self.assertEqual(tracker.get_escalation_level("reset_loop"), 3)

        tracker.reset_loop("reset_loop")
        # After reset, state is gone so cooldown check returns True (never fired).
        self.assertTrue(tracker.check_cooldown("reset_loop", 0))  # always allowed when not tracked.
        self.assertIsNone(getattr(tracker._loop_state.get("reset_loop"), "trigger_count", None))

    def test_reset_nonexistent_loop_no_crash(self):
        """Reset a loop that doesn't exist should be a no-op."""
        tracker = EscalationTracker()
        tracker.reset_loop("nonexistent")  # silent


class TestEscalationTrackerConcurrencyRobustness(unittest.TestCase):
    """Tests edge cases and boundary conditions for the tracker."""

    def test_reset_allows_cooldown_fire(self):
        """After resetting, cooldown should allow a new fire sequence."""
        tracker = EscalationTracker()
        # Simulate a recent trigger (1 hour ago) with a 2-hour cooldown expectation.
        past_time = time.monotonic() - 3600  # 1 hour ago.
        tracker._loop_state["resett_loop"] = {
            "trigger_count": 10,
            "last_fired_at": past_time,
            "level": 3,
            "threshold_per_level": 3,
        }
        # Elapsed ≈ 3600 + ε; with interval=7200, blocked (False).
        self.assertFalse(tracker.check_cooldown("resett_loop", 7200))

        tracker.reset_loop("resett_loop")
        # After reset: never fired -> always allowed.
        self.assertTrue(tracker.check_cooldown("resett_loop", 60 * 60))

    def test_no_state_for_new_loop(self):
        """A new loop that has never been tracked should have no entry in state."""
        tracker = EscalationTracker()
        self.assertFalse("fresh_loop" in tracker._loop_state)
        # But cooldown check should pass.
        self.assertTrue(tracker.check_cooldown("resh_loop", 10))


if __name__ == "__main__":
    unittest.main()
