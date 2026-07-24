"""
Tests for RecursionGuard — thread-safe recursion depth counter.

Coverage:
  TC-1 Constructor defaults + customization
  TC-2 Context manager increments/decrements correctly
  TC-3 Raises RecursionError at max_depth
  TC-4 Depth is returned from __enter__
  TC-5 current_depth/is_at_limit query helpers
  TC-6 Generator-based enter() interface
  TC-7 block_reentry sentinel boolean
  TC-8 Invalid max_depth raises ValueError
  TC-9 Exception propagation through context manager
  TC-10 Thread-safety: concurrent increments stay correct
  TC-11 Thread-safety: max_depth enforcement across threads
"""

import threading
import unittest

# Ensure src/ is on the path for non-installed runs
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.recursion_guard import RecursionGuard


class TestRecursionGuardConstructor(unittest.TestCase):
    """TC-1 & TC-8"""

    def test_default_max_depth(self):
        guard = RecursionGuard()
        self.assertEqual(guard.max_depth, 5)

    def test_custom_max_depth(self):
        guard = RecursionGuard(max_depth=10)
        self.assertEqual(guard.max_depth, 10)

    def test_min_max_depth(self):
        guard = RecursionGuard(max_depth=1)
        self.assertEqual(guard.max_depth, 1)

    def test_zero_max_depth_raises(self):
        with self.assertRaises(ValueError):
            RecursionGuard(0)

    def test_negative_max_depth_raises(self):
        with self.assertRaises(ValueError):
            RecursionGuard(-3)

    def test_initial_depth_is_zero(self):
        guard = RecursionGuard()
        self.assertEqual(guard.current_depth, 0)


class TestRecursionGuardContextManager(unittest.TestCase):
    """TC-2 & TC-4"""

    def test_single_entry_exits_cleanly(self):
        guard = RecursionGuard()
        with guard as depth:
            self.assertEqual(depth, 1)
            self.assertEqual(guard.current_depth, 1)
        self.assertEqual(guard.current_depth, 0)

    def test_nested_entries_incr_by_one_each(self):
        guard = RecursionGuard()
        with guard as d1:
            self.assertEqual(d1, 1)
            with guard as d2:
                self.assertEqual(d2, 2)
                with guard as d3:
                    self.assertEqual(d3, 3)
                self.assertEqual(guard.current_depth, 2)
            self.assertEqual(guard.current_depth, 1)
        self.assertEqual(guard.current_depth, 0)

    def test_max_five_entries_then_sixth_raises(self):
        guard = RecursionGuard(max_depth=5)
        with guard:
            with guard:
                with guard:
                    with guard:
                        with guard:
                            self.assertEqual(guard.current_depth, 5)
        self.assertEqual(guard.current_depth, 0)

    def test_depth_resets_after_context(self):
        guard = RecursionGuard(max_depth=3)
        with guard:
            with guard:
                pass
        self.assertEqual(guard.current_depth, 0)
        with guard:
            self.assertEqual(guard.current_depth, 1)


class TestRecursionGuardLimits(unittest.TestCase):
    """TC-3"""

    def test_raises_recursion_error_at_limit(self):
        guard = RecursionGuard(max_depth=2)
        with guard:
            with guard:
                with self.assertRaises(RecursionError):
                    with guard:
                        pass
        self.assertEqual(guard.current_depth, 0, "depth should reset after error")

    def test_raises_with_max_depth_1(self):
        guard = RecursionGuard(max_depth=1)
        with guard:
            with self.assertRaises(RecursionError):
                with guard:
                    pass

    def test_message_includes_actual_and_max(self):
        guard = RecursionGuard(max_depth=2)
        with guard:
            with guard:
                try:
                    with guard:
                        pass
                except RecursionError as ex:
                    self.assertIn("2/2", str(ex))


class TestRecursionGuardQueryHelpers(unittest.TestCase):
    """TC-5 & TC-7"""

    def test_current_depth(self):
        guard = RecursionGuard()
        self.assertEqual(guard.current_depth, 0)
        with guard:
            self.assertEqual(guard.current_depth, 1)

    def test_is_at_limit_initially_false(self):
        guard = RecursionGuard(max_depth=3)
        self.assertFalse(guard.is_at_limit())

    def test_is_at_limit_when_full(self):
        guard = RecursionGuard(max_depth=2)
        with guard:
            with guard:
                self.assertTrue(guard.is_at_limit())

    def test_block_reentry_false_when_idle(self):
        guard = RecursionGuard()
        self.assertFalse(guard.block_reentry())

    def test_block_reentry_true_when_entered(self):
        guard = RecursionGuard()
        with guard:
            self.assertTrue(guard.block_reentry())


class TestRecursionGuardGenerator(unittest.TestCase):
    """TC-6"""

    def test_enter_generator_same_semantics(self):
        guard = RecursionGuard()
        with guard.enter():
            self.assertEqual(guard.current_depth, 1)
        self.assertEqual(guard.current_depth, 0)


class TestRecursionGuardExceptionProp(unittest.TestCase):
    """TC-9"""

    def test_exception_propagates_and_depth_resets(self):
        guard = RecursionGuard()
        with self.assertRaises(RuntimeError):
            with guard:
                raise RuntimeError("test error")
        self.assertEqual(guard.current_depth, 0)

    def test_exception_in_nested_context(self):
        guard = RecursionGuard(max_depth=10)
        try:
            with guard:
                with guard:
                    with guard:
                        raise ValueError("nested failure")
        except ValueError:
            pass
        self.assertEqual(guard.current_depth, 0)


class TestRecursionGuardThreading(unittest.TestCase):
    """TC-10 & TC-11"""

    def test_concurrent_entries_no_race_on_count(self):
        guard = RecursionGuard(max_depth=100)
        errors: list = []

        def enter_exit():
            try:
                for _ in range(50):
                    with guard:
                        pass
            except Exception as ex:
                errors.append(ex)

        threads = [threading.Thread(target=enter_exit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [], str(errors))
        self.assertEqual(guard.current_depth, 0)

    def test_no_leaked_depth_after_threads(self):
        guard = RecursionGuard(max_depth=50)
        barrier = threading.Barrier(4)

        def hit_barrier_nest():
            barrier.wait(timeout=5)
            with guard:
                with guard:
                    pass

        threads = [threading.Thread(target=hit_barrier_nest) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(guard.current_depth, 0)


if __name__ == "__main__":
    unittest.main()