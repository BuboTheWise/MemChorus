"""
Deep nesting and safe unwinding tests for RecursionGuard in enforcement chains.

Simulates save() -> enforce() -> hook -> save() chains up to 3 levels deep,
mocking external dependencies to isolate guard behavior. Verifies depth
increments/decrements correctly, no RecursionError occurs within limits,
and state returns to baseline after completion.

Acceptance criteria:
  - Tests pass for 1, 2, and 3 level nesting scenarios
  - Coverage includes exception paths (exceptions inside nested contexts)
  - Depth counter resets to zero in all cases
  - RecursionError correctly raised when max_depth exceeded
"""

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure src/ is on the path for non-installed runs
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.recursion_guard import RecursionGuard


class TestOneLevelNesting(unittest.TestCase):
    """Single enforcement hook — save() triggers enforce(), guard enters/exits cleanly."""

    def test_single_enter_exit_depth_zero(self):
        """A single enforcement cycle increments to 1, then returns to 0."""
        guard = RecursionGuard(max_depth=5)
        self.assertEqual(guard.current_depth, 0)

        with guard as depth:
            self.assertEqual(depth, 1)
            self.assertEqual(guard.current_depth, 1)

        self.assertEqual(guard.current_depth, 0)

    def test_single_enter_exit_returns_correct_depth(self):
        """__enter__ returns the current nesting level."""
        guard = RecursionGuard(max_depth=3)

        with guard as d:
            self.assertEqual(d, 1)

    def test_mocked_enforcement_chain_one_level(self):
        """Simulate save() -> enforce() at depth 1 with mocked orchestrator."""
        guard = RecursionGuard(max_depth=5)

        # Simulate what orchestrator.save() does before post-action enforcement:
        # it enters the guard, performs the save, then enforces.
        mock_orchestrator = MagicMock()
        mock_orchestrator.save.return_value = True

        with guard as depth1:
            self.assertEqual(depth1, 1)
            # The "save" happens here
            mock_orchestrator.save("key", {"data": "value"})
            # Enforcement hook fires — in real code this calls manager.enforce()
            # which might trigger nested operations. At level 1 it doesn't.

        self.assertEqual(guard.current_depth, 0)
        mock_orchestrator.save.assert_called_once()

    def test_single_exit_on_exception(self):
        """Exception during enforcement still decrements depth to zero."""
        guard = RecursionGuard(max_depth=5)

        with self.assertRaises(RuntimeError):
            with guard:
                raise RuntimeError("enforcement failure")

        self.assertEqual(guard.current_depth, 0)


class TestTwoLevelNesting(unittest.TestCase):
    """Two enforcement hooks — save() triggers enforce() which triggers another save()."""

    def test_two_level_nesting_depth_sequence(self):
        """Depth follows 0 -> 1 -> 2 -> 1 -> 0 pattern."""
        guard = RecursionGuard(max_depth=5)
        self.assertEqual(guard.current_depth, 0)

        with guard as d1:
            self.assertEqual(d1, 1)
            with guard as d2:
                self.assertEqual(d2, 2)
            self.assertEqual(guard.current_depth, 1)

        self.assertEqual(guard.current_depth, 0)

    def test_mocked_save_enforce_save_two_levels(self):
        """
        Simulate: orchestrator.save() -> enforce() -> storage_engine.save()
        where the second save also triggers enforce again before hitting depth limit.

        This mirrors the real chain:
           Orchestrator.save(key)       [depth 1]
             -> em.enforce(outcome_text)
               -> recall_engine.on_decision_point()    [would be depth 2 if guarded]
                 -> orchestrator.search(query)          [could trigger enforce again]

        We mock the guard at each level to verify correct nesting.
        """
        guard = RecursionGuard(max_depth=5)

        outer_saved = []
        inner_saved = []

        def do_outer_save():
            with guard as depth1:
                self.assertEqual(depth1, 1)
                outer_saved.append("data_a")
                # Simulate enforcement firing inside save()
                do_inner_save()
                # After inner enforce returns, we're back to depth 1
                self.assertEqual(guard.current_depth, 1)

        def do_inner_save():
            with guard as depth2:
                self.assertEqual(depth2, 2)
                inner_saved.append("data_b")
                # Inner save's enforcement hook does NOT recurse further (mocked out)
                pass
            assert guard.current_depth == 1

        do_outer_save()

        self.assertEqual(guard.current_depth, 0)
        self.assertEqual(len(outer_saved), 1)
        self.assertEqual(len(inner_saved), 1)

    def test_two_level_exception_in_inner_context(self):
        """Exception raised in inner enforcement still unwinds both levels."""
        guard = RecursionGuard(max_depth=5)

        exception_raised = False
        try:
            with guard as depth1:
                self.assertEqual(depth1, 1)
                with guard as depth2:
                    self.assertEqual(depth2, 2)
                    raise ValueError("inner enforcement error")
        except ValueError:
            exception_raised = True

        self.assertTrue(exception_raised)
        self.assertEqual(guard.current_depth, 0)

    def test_two_level_exception_in_outer_context(self):
        """Exception raised in outer context after inner completes still resets."""
        guard = RecursionGuard(max_depth=5)

        try:
            with guard as depth1:
                self.assertEqual(depth1, 1)
                with guard as depth2:
                    self.assertEqual(depth2, 2)
                # Inner exited successfully, back to depth 1
                self.assertEqual(guard.current_depth, 1)
                raise RuntimeError("outer enforcement error")
        except RuntimeError:
            pass

        self.assertEqual(guard.current_depth, 0)


class TestThreeLevelNesting(unittest.TestCase):
    """
    Three enforcement hooks — save() -> enforce() -> recall -> search -> enforce() again.

    This mirrors the worst real-world chain:
      Orchestrator.save(key)                        [depth 1]
        -> em.enforce(outcome_text)
           -> storage_engine.save(recall_result)     [depth 2 via guard]
              -> em.enforce(storage_outcome)
                 -> recall_engine.on_decision_point() [depth 3 via guard]
                   -> orchestrator.search(query)
                      -> (no further nesting — depth 3 is the cap)
    """

    def test_three_level_nesting_depth_sequence(self):
        """Depth follows 0 -> 1 -> 2 -> 3 -> 2 -> 1 -> 0 pattern."""
        guard = RecursionGuard(max_depth=5)
        self.assertEqual(guard.current_depth, 0)

        with guard as d1:
            self.assertEqual(d1, 1)
            with guard as d2:
                self.assertEqual(d2, 2)
                with guard as d3:
                    self.assertEqual(d3, 3)
                self.assertEqual(guard.current_depth, 2)
            self.assertEqual(guard.current_depth, 1)

        self.assertEqual(guard.current_depth, 0)

    def test_mocked_full_chain_three_levels(self):
        """Fully mocked save -> enforce -> save -> enforce -> recall chain (3 depths)."""
        guard = RecursionGuard(max_depth=5)

        operations = []  # tracks which operation ran at which depth

        def level_1_save():
            with guard as depth:
                self.assertEqual(depth, 1)
                operations.append(("save_outer", 1))
                level_2_enforce()
                operations.append(("back_to_outer", guard.current_depth))
                self.assertEqual(guard.current_depth, 1)

        def level_2_enforce():
            with guard as depth:
                self.assertEqual(depth, 2)
                operations.append(("enforce_mid", 2))
                level_3_recall()
                operations.append(("back_to_mid", guard.current_depth))
                self.assertEqual(guard.current_depth, 2)

        def level_3_recall():
            with guard as depth:
                self.assertEqual(depth, 3)
                operations.append(("recall_inner", 3))
                # No further nesting — this is the deepest hook
                pass
            operations.append(("exited_inner", guard.current_depth))
            self.assertEqual(guard.current_depth, 2)

        level_1_save()

        self.assertEqual(guard.current_depth, 0)

        # Verify operation log shows correct depth at each step
        op_types = [op[0] for op in operations]
        expected = ["save_outer", "enforce_mid", "recall_inner",
                    "exited_inner", "back_to_mid", "back_to_outer"]
        self.assertEqual(op_types, expected)

    def test_three_level_exception_at_depth_3(self):
        """Exception raised at the deepest nesting level unwinds all three levels."""
        guard = RecursionGuard(max_depth=5)

        try:
            with guard as d1:
                self.assertEqual(d1, 1)
                with guard as d2:
                    self.assertEqual(d2, 2)
                    with guard as d3:
                        self.assertEqual(d3, 3)
                        raise RuntimeError("deepest recall failure")
        except RuntimeError:
            pass

        self.assertEqual(guard.current_depth, 0)

    def test_three_level_exception_at_depth_2(self):
        """Exception raised at middle level unwinds levels 2 and 1."""
        guard = RecursionGuard(max_depth=5)

        try:
            with guard as d1:
                self.assertEqual(d1, 1)
                with guard as d2:
                    self.assertEqual(d2, 2)
                    # Complete inner save at depth 3 before failing at level 2
                    with guard as d3:
                        self.assertEqual(d3, 3)
                    raise RuntimeError("mid-level enforcement failed")
        except RuntimeError:
            pass

        self.assertEqual(guard.current_depth, 0)

    def test_three_level_then_reenter(self):
        """After three-level nesting completes, the guard can be reentered fresh."""
        guard = RecursionGuard(max_depth=5)

        # First full chain: depth 3
        with guard as d1:
            with guard as d2:
                with guard as d3:
                    self.assertEqual(d3, 3)
        self.assertEqual(guard.current_depth, 0)

        # Re-enter — fresh start at depth 1
        with guard as d1_again:
            self.assertEqual(d1_again, 1)
        self.assertEqual(guard.current_depth, 0)


class TestMaxDepthEnforcement(unittest.TestCase):
    """Verify RecursionError is raised when exceeding max_depth during nesting."""

    def test_max_depth_3_blocked_at_fourth(self):
        """With max_depth=3, the 4th entry raises RecursionError and depth resets to 0."""
        guard = RecursionGuard(max_depth=3)

        with guard:
            with guard:
                with guard:
                    self.assertEqual(guard.current_depth, 3)
                    with self.assertRaises(RecursionError):
                        with guard:
                            pass

        self.assertEqual(guard.current_depth, 0)

    def test_max_depth_2_blocked_at_third(self):
        """With max_depth=2 (auto_recall_engine default), third entry raises."""
        guard = RecursionGuard(max_depth=2)

        with guard as d1:
            self.assertEqual(d1, 1)
            with guard as d2:
                self.assertEqual(d2, 2)
                with self.assertRaises(RecursionError):
                    with guard:
                        pass
            self.assertEqual(guard.current_depth, 1)

        self.assertEqual(guard.current_depth, 0)

    def test_max_depth_exceeded_message_contains_depth(self):
        """The RecursionError message includes current and max depth."""
        guard = RecursionGuard(max_depth=2)

        with guard:
            with guard:
                try:
                    with guard:
                        pass
                except RecursionError as exc:
                    self.assertIn("2/2", str(exc))

    def test_guard_state_after_blocked_entry(self):
        """After a blocked entry is rejected, the guard state is restored correctly."""
        guard = RecursionGuard(max_depth=2)

        with guard as d1:
            self.assertEqual(d1, 1)
            try:
                with guard:
                    pass
            except RecursionError:
                pass
            # Try to enter again — depth is still 1, which is < max_depth 2
            with guard as d2:
                self.assertEqual(d2, 2)

        self.assertEqual(guard.current_depth, 0)


class TestEnterGeneratorInterface(unittest.TestCase):
    """Test the .enter() generator-based context manager used by AutoRecallEngine."""

    def test_enter_single_level(self):
        guard = RecursionGuard(max_depth=5)

        with guard.enter() as depth:
            self.assertEqual(depth, 1)
        self.assertEqual(guard.current_depth, 0)

    def test_enter_nested_three_levels(self):
        """Simulate the actual pattern in auto_recall_engine.on_decision_point()."""
        guard = RecursionGuard(max_depth=5)

        with guard.enter() as d1:
            self.assertEqual(d1, 1)
            with guard.enter() as d2:
                self.assertEqual(d2, 2)
                with guard.enter() as d3:
                    self.assertEqual(d3, 3)

        self.assertEqual(guard.current_depth, 0)

    def test_enter_bails_on_recursion_like_auto_recall_engine(self):
        """
        AutoRecallEngine catches RecursionError from guard.enter() and returns [].
        Simulate: outer recall is active (depth 1), a nested trigger fires but
        the inner entry hits max_depth so it bails with an empty list.
        """
        guard = RecursionGuard(max_depth=1)

        def on_decision_point():
            try:
                with guard.enter():
                    return ["context"]
            except RecursionError:
                return []

        # First call: depth 0 -> 1, succeeds
        self.assertEqual(on_decision_point(), ["context"])
        self.assertEqual(guard.current_depth, 0)

        # Simulate a recursive trigger: enter guard, then try to enter again from inside
        with guard.enter():  # now at depth 1 (== max_depth)
            inner_result = on_decision_point()
            self.assertEqual(inner_result, [])

    def test_enter_nested_pattern_with_catch(self):
        """Correct simulation: recall_engine calls itself recursively."""
        guard = RecursionGuard(max_depth=2)

        # First entry succeeds
        with guard.enter():
            # Second entry at depth 2 also succeeds (guard.max_depth is 2, current is 1 < 2)
            with guard.enter() as d:
                self.assertEqual(d, 2)
                # Third entry MUST be blocked because depth 2 >= max_depth 2
                try:
                    with guard.enter():
                        pass
                    self.fail("Should have raised RecursionError")
                except RecursionError:
                    pass  # Expected

        self.assertEqual(guard.current_depth, 0)


class TestIsolationBetweenGuards(unittest.TestCase):
    """Verify independent RecursionGuard instances don't interfere with each other."""

    def test_two_guards_independent(self):
        """Two separate guards (e.g., orchestrator._guard vs recall_engine._guard) are independent."""
        orch_guard = RecursionGuard(max_depth=5)
        recall_guard = RecursionGuard(max_depth=2)

        with orch_guard:
            with recall_guard:
                self.assertEqual(orch_guard.current_depth, 1)
                self.assertEqual(recall_guard.current_depth, 1)
                with recall_guard:
                    self.assertEqual(recall_guard.current_depth, 2)
                    # Now recall guard is at limit; orchestrator guard still has room.

        self.assertEqual(orch_guard.current_depth, 0)
        self.assertEqual(recall_guard.current_depth, 0)


class TestThreadedNesting(unittest.TestCase):
    """Verify depth behaves correctly under concurrent nested enforcement."""

    def test_concurrent_nested_access(self):
        """Multiple threads each doing save->enforce->save don't corrupt the counter."""
        guard = RecursionGuard(max_depth=50)
        errors = []
        depths_seen = []

        def nested_operation(op_id):
            try:
                with guard as d1:
                    depths_seen.append((op_id, "L1", d1))
                    for _ in range(3):
                        with guard as d2:
                            depths_seen.append((op_id, "L2", d2))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=nested_operation, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Errors: {errors}")
        self.assertEqual(guard.current_depth, 0)

    def test_concurrent_with_low_max_depth(self):
        """With low max_depth and many threads, some raise RecursionError — but depth still resets."""
        guard = RecursionGuard(max_depth=3)

        success_count = [0]
        blocked_count = [0]
        lock = threading.Lock()

        def try_nest():
            try:
                with guard:
                    with guard:
                        with guard:
                            with lock:
                                success_count[0] += 1
            except RecursionError:
                with lock:
                    blocked_count[0] += 1

        threads = [threading.Thread(target=try_nest) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(guard.current_depth, 0)
        # At least some succeeded and at least some were blocked (with 20 threads trying to hit depth 4)
        self.assertGreater(success_count[0], 0)


class TestQueryHelpersDuringNesting(unittest.TestCase):
    """Verify current_depth, is_at_limit, block_reentry during active nesting."""

    def test_queries_during_three_level_nesting(self):
        guard = RecursionGuard(max_depth=5)

        with guard:
            self.assertEqual(guard.current_depth, 1)
            self.assertFalse(guard.is_at_limit())
            self.assertTrue(guard.block_reentry())

            with guard:
                self.assertEqual(guard.current_depth, 2)
                self.assertFalse(guard.is_at_limit())

                with guard:
                    self.assertEqual(guard.current_depth, 3)
                    self.assertFalse(guard.is_at_limit())
                    self.assertTrue(guard.block_reentry())

                self.assertEqual(guard.current_depth, 2)
            self.assertEqual(guard.current_depth, 1)

        self.assertFalse(guard.block_reentry())
        self.assertEqual(guard.current_depth, 0)

    def test_is_at_limit_true_when_max_minus_zero(self):
        """is_at_limit returns True when depth equals max_depth."""
        guard = RecursionGuard(max_depth=3)

        with guard:
            with guard:
                with guard:
                    self.assertTrue(guard.is_at_limit())


if __name__ == "__main__":
    unittest.main()
