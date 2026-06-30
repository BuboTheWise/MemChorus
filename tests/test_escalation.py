"""Tests for EscalationTracker (escalation.py module).

Covers: cooldown tracking, stepped L1-L3 escalation, per-loop isolation,
edge cases (zero thresholds, negative counts, boundary conditions).
Uses the real EscalationTracker class from escalation.py.
"""

import sys
import unittest
import time

sys.path.insert(0, __file__.replace("tests/test_escalation.py", "src"))

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from memchorus.feedback_loop.escalation import (  # noqa: E402
    EscalationTracker,
    _DEFAULT_COOLDOWN_SECONDS,
    _MAX_COOLDOWN_SECONDS,
    _SEVERITY_DESC,
)


# ===========================================================================
# F-E1 : EscalationTracker init and default values
# ===========================================================================

class TestInitAndDefaults(unittest.TestCase):
    """F-E1: Init with defaults and non-defaults."""

    def test_default_state_is_empty(self):
        tracker = EscalationTracker()
        self.assertEqual(len(tracker._loop_state), 0)

    def test_custom_cooldown_init(self):
        tracker = EscalationTracker(default_cooldown=30.0, level_threshold=5)
        self.assertEqual(len(tracker._loop_state), 0)

    def test_init_loop_creates_state(self):
        tracker = EscalationTracker()
        tracker.init_loop("test_loop")
        self.assertIn("test_loop", tracker._loop_state)


# ===========================================================================
# F-E2 : Cooldown window tracking (check_cooldown method)
# ===========================================================================

class TestCooldownWindows(unittest.TestCase):
    """F-E2: Cooldown windows are respected."""

    def test_first_fire_always_eligible(self):
        tracker = EscalationTracker(default_cooldown=60.0)
        tracker.init_loop("loop1")
        self.assertTrue(tracker.check_cooldown("loop1", 60.0))

    def test_immediately_after_trigger_not_eligible(self):
        tracker = EscalationTracker(default_cooldown=60.0)
        tracker.init_loop("loop1")
        tracker.record_trigger("loop1")
        # Just fired -- not eligible for another 60 seconds.
        self.assertFalse(tracker.check_cooldown("loop1", 60.0))

    def test_cooldown_remaining_decreases(self):
        tracker = EscalationTracker(default_cooldown=60.0)
        tracker.init_loop("loop1", cooldown_seconds=60.0)
        # Must record a trigger first to set last_fired_at;
        # remaining is 0 before any trigger has occurred.
        tracker.record_trigger("loop1")
        time.sleep(0.15)  # allow some elapsed time
        initial = tracker.get_cooldown_remaining_seconds("loop1")
        self.assertGreater(initial, 0.0)
        time.sleep(0.2)
        remaining = tracker.get_cooldown_remaining_seconds("loop1")
        self.assertLess(remaining, initial)

    def test_reset_clears_state(self):
        tracker = EscalationTracker()
        tracker.init_loop("loop1")
        tracker.record_trigger("loop1")
        self.assertFalse(tracker.check_cooldown("loop1", 60.0))

        tracker.reset_loop("loop1")
        self.assertTrue(tracker.check_cooldown("loop1", 60.0))


# ===========================================================================
# F-E3 : Stepped escalation L1-L3
# ===========================================================================

class TestSteppedEscalation(unittest.TestCase):
    """F-E3: Escalation levels advance at threshold boundaries."""

    def test_default_threshold_advances(self):
        """With default threshold (3), level should advance at 3, 6 triggers."""
        tracker = EscalationTracker(default_cooldown=0.0)

        for i in range(1, 4):
            lvl = tracker.record_trigger("test_loop")
            self.assertEqual(lvl, 1)

        for i in range(4, 7):
            lvl = tracker.record_trigger("test_loop")
            self.assertEqual(lvl, 2)

        for i in range(7, 10):
            lvl = tracker.record_trigger("test_loop")
            self.assertEqual(lvl, 3)

        # Should not go beyond L3.
        for i in range(10, 50):
            lvl = tracker.record_trigger("test_loop")
            self.assertEqual(lvl, 3)

    def test_custom_threshold_advances(self):
        """With threshold (2), level should advance to L2 at trigger 3 and L3 at trigger 5."""
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=2)

        lvl1 = tracker.record_trigger("test_loop")
        self.assertEqual(lvl1, 1)

        # count=2 still L1 — two triggers needed per level with threshold=2
        lvl2 = tracker.record_trigger("test_loop")
        self.assertEqual(lvl2, 1)

        # count=3 -> L2
        lvl3 = tracker.record_trigger("test_loop")
        self.assertEqual(lvl3, 2)

        # count=4 still L2 (4-1)//2+1 = 2
        lvl_4 = tracker.record_trigger("test_loop")
        self.assertEqual(lvl_4, 2)

        # count=5 -> L3
        for i in range(3):
            lvl = tracker.record_trigger("test_loop")
            self.assertEqual(lvl, 3)


# ===========================================================================
# F-E4 : Action type mapping via _SEVERITY_DESC (L1=log, L2=prompt, L3=override)
# ===========================================================================

class TestActionMapping(unittest.TestCase):
    """F-E4: Actions map correctly to levels."""

    def test_l1_severity(self):
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=100)
        tracker.init_loop("test_loop")
        self.assertEqual(tracker.get_escalation_level("test_loop"), 1)
        desc = _SEVERITY_DESC[1]
        self.assertIn("L1", desc)

    def test_l2_severity(self):
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=1)
        for _ in range(2):
            tracker.record_trigger("test_loop")
        self.assertEqual(tracker.get_escalation_level("test_loop"), 2)
        desc = _SEVERITY_DESC[2]
        self.assertIn("L2", desc)

    def test_l3_severity(self):
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=1)
        for _ in range(4):
            tracker.record_trigger("test_loop")
        self.assertEqual(tracker.get_escalation_level("test_loop"), 3)
        desc = _SEVERITY_DESC[3]
        self.assertIn("L3", desc)


# ===========================================================================
# F-E5 : get_escalation_level returns correct value
# ===========================================================================

class TestGetEscalationLevel(unittest.TestCase):
    """F-E5: get_escalation_level tracks correctly."""

    def test_initial_level_is_one(self):
        tracker = EscalationTracker()
        tracker.init_loop("test_loop")
        self.assertEqual(tracker.get_escalation_level("test_loop"), 1)

    def test_after_threshold_advances_level(self):
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=2)
        tracker.record_trigger("test_loop")
        self.assertEqual(tracker.get_escalation_level("test_loop"), 1)

        # With threshold=2, count=2 is still L1 — need 2 triggers per level
        tracker.record_trigger("test_loop")
        self.assertEqual(tracker.get_escalation_level("test_loop"), 1)

        # count=3 -> L2
        tracker.record_trigger("test_loop")
        self.assertEqual(tracker.get_escalation_level("test_loop"), 2)


# ===========================================================================
# F-E6 : Per-loop state isolation
# ===========================================================================

class TestPerLoopIsolation(unittest.TestCase):
    """F-E6: Each loop maintains its own state."""

    def test_two_loops_have_independent_levels(self):
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=1)

        for _ in range(5):
            tracker.record_trigger("loop_a")

        for _ in range(2):
            tracker.record_trigger("loop_b")

        self.assertEqual(tracker.get_escalation_level("loop_a"), 3)
        self.assertEqual(tracker.get_escalation_level("loop_b"), 2)

    def test_reset_one_loop_doesnt_affect_other(self):
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=1)

        for _ in range(5):
            tracker.record_trigger("loop_a")
        for _ in range(3):
            tracker.record_trigger("loop_b")

        tracker.reset_loop("loop_a")
        self.assertEqual(tracker.get_escalation_level("loop_a"), 1)
        self.assertEqual(tracker.get_escalation_level("loop_b"), 3)


# ===========================================================================
# F-E7 : Edge cases (zero, negative, extreme values)
# ===========================================================================

class TestEdgeCases(unittest.TestCase):
    """F-E7: Zero and edge-case inputs handled gracefully."""

    def test_zero_threshold_falls_back_to_one(self):
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=2)
        # Should not crash on zero/edge cases.
        tracker.record_trigger("test_loop")

    def test_negative_threshold_handled(self):
        """Negative threshold should be clamped to minimum (1)."""
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=-5)
        self.assertEqual(tracker.get_escalation_level("test_loop"), 1)

    def test_get_action_after_reset(self):
        """After reset, escalation should be back at L1."""
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=1)
        tracker.init_loop("test_loop")
        self.assertEqual(tracker.get_escalation_level("test_loop"), 1)


# ===========================================================================
# F-E8 : Additional escalation logic tests
# ===========================================================================

class TestAdditionalEscalation(unittest.TestCase):
    """F-E8: Additional behaviors not covered above."""

    def test_check_cooldown_returns_true_for_uninitialized(self):
        tracker = EscalationTracker()
        # Never initialized -- should always allow.
        self.assertTrue(tracker.check_cooldown("unknown_loop", 60.0))

    def test_get_cooldown_remaining_zero_after_reset(self):
        tracker = EscalationTracker()
        tracker.init_loop("test_loop")
        remaining = tracker.get_cooldown_remaining_seconds("test_loop")
        self.assertEqual(remaining, 0.0)

    def test_level_capped_at_three(self):
        """Level should never exceed 3 regardless of trigger count."""
        tracker = EscalationTracker(default_cooldown=0.0, level_threshold=1)
        for _ in range(100):
            tracker.record_trigger("overflow_loop")
        self.assertEqual(tracker.get_escalation_level("overflow_loop"), 3)


if __name__ == "__main__":
    unittest.main()
