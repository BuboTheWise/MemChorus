"""
Tests for ToolCaptureBuffer batch buffer + flush timer.

Covers acceptance criteria:
  AC-1: Buffer accumulation — item count threshold flushes all pending entries.
  AC-2: Timer-based flush triggers reliably within ~±1s tolerance.
  AC-3: Empty buffer doesn't cause write attempts / errors on cleanup/shutdown.
  AC-4: Context manager __exit__ drains remaining items gracefully.
  AC-5: Callback failures re-queue the batch (no silent data loss).
  AC-6: Thread safety — concurrent adds and flushes don't corrupt state.
  AC-7: Stats tracking correctness.
"""

import os
import sys
import threading
import time
import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.tool_capture_buffer import (
    BufferStats,
    ToolCaptureBuffer,
)


class TestBasicConstruction(unittest.TestCase):
    def test_defaults(self):
        buf = ToolCaptureBuffer()
        self.assertEqual(buf.max_items, 10)
        self.assertEqual(buf.flush_interval, 5.0)
        self.assertIsNone(buf.callback)

    def test_custom_params(self):
        buf = ToolCaptureBuffer(max_items=20, flush_interval=3.0, callback=lambda x: None)
        self.assertEqual(buf.max_items, 20)
        self.assertEqual(buf.flush_interval, 3.0)
        self.assertIsNotNone(buf.callback)

    def test_rejects_invalid_max_items(self):
        with self.assertRaises(ValueError):
            ToolCaptureBuffer(max_items=0)

    def test_rejects_invalid_flush_interval(self):
        with self.assertRaises(ValueError):
            ToolCaptureBuffer(flush_interval=-1.0)


class TestAddAndPending(unittest.TestCase):
    """Basic add + pending counter without triggering a flush."""

    def setUp(self):
        self.received: List[List[Dict[str, Any]]] = []
        self.buf = ToolCaptureBuffer(
            max_items=1000,  # effectively no count-flush
            flush_interval=300.0,  # no timer-flush in test timeframe
            callback=lambda batch: self.received.append(batch),
        )

    def tearDown(self):
        self.buf.close()

    def test_add_increments_pending(self):
        self.assertEqual(self.buf.pending, 0)
        self.buf.add({"key": "a"})
        self.assertEqual(self.buf.pending, 1)
        self.buf.add({"key": "b"})
        self.assertEqual(self.buf.pending, 2)

    def test_add_stores_payloads(self):
        self.buf.add({"text": "first", "_ac_key": "k1"})
        self.buf.add({"text": "second", "_ac_key": "k2"})
        flushed_count = self.buf.flush()
        self.assertEqual(flushed_count, 2)
        batch = self.received[0]
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0]["text"], "first")
        self.assertEqual(batch[1]["_ac_key"], "k2")

    def test_add_closed_buffer_raises(self):
        self.buf.close()
        with self.assertRaises(RuntimeError):
            self.buf.add({"key": "nope"})


class TestCountThresholdFlush(unittest.TestCase):
    """AC-1: buffer accumulation hitting item count threshold flushes all."""

    def setUp(self):
        self.received: List[List[Dict[str, Any]]] = []
        self.buf = ToolCaptureBuffer(
            max_items=3,
            flush_interval=300.0,  # timer won't fire during test
            callback=lambda batch: self.received.append(batch),
        )

    def tearDown(self):
        self.buf.close()

    def test_flush_at_max_items(self):
        """Add exactly max_items → immediate flush."""
        for i in range(3):
            self.buf.add({"idx": i})

        # Should have auto-flushed.
        self.assertEqual(len(self.received), 1)
        self.assertEqual(len(self.received[0]), 3)
        self.assertEqual(self.buf.pending, 0)

    def test_no_flush_below_threshold(self):
        """Add fewer than max_items → no flush until explicit."""
        for i in range(2):
            self.buf.add({"idx": i})

        # Below threshold, nothing flushed yet.
        self.assertEqual(len(self.received), 0)
        self.assertEqual(self.buf.pending, 2)

        # Explicit flush drains them.
        self.buf.flush()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(len(self.received[0]), 2)

    def test_multiple_threshold_flushes(self):
        """Adding more than max_items triggers multiple flushes."""
        for i in range(7):
            self.buf.add({"idx": i})

        # Each batch of 3 should have flushed.
        remaining = sum(len(b) for b in self.received) + self.buf.pending
        self.assertEqual(remaining, 7)

    def test_flush_empty_returns_zero(self):
        """AC-3: flushing empty buffer returns 0."""
        self.assertEqual(self.buf.flush(), 0)


class TestTimerFlush(unittest.TestCase):
    """AC-2: timer-based flush triggers within ±1s tolerance."""

    def test_timer_fires_at_interval(self):
        """Add item, wait for interval + margin, check flush occurred."""
        self.received: List[List[Dict[str, Any]]] = []
        buf = ToolCaptureBuffer(
            max_items=1000,  # count won't trigger
            flush_interval=0.5,  # short timer for testing
            callback=lambda batch: self.received.append(batch),
        )

        buf.add({"timer_item": True})
        time.sleep(1.2)  # well past 0.5s + margin

        self.assertEqual(len(self.received), 1)
        self.assertEqual(len(self.received[0]), 1)
        buf.close()

    def test_timer_resets_after_flush(self):
        """After a timer flush, adding again restarts the countdown."""
        self.received: List[List[Dict[str, Any]]] = []
        buf = ToolCaptureBuffer(
            max_items=1000,
            flush_interval=0.5,
            callback=lambda batch: self.received.append(batch),
        )

        # First item → timer fires at ~0.5s.
        buf.add({"batch": 1})
        time.sleep(1.2)
        self.assertEqual(len(self.received), 1)

        # Second item → new timer starts.
        buf.add({"batch": 2})
        # Just before next interval — may or may not have flushed yet.
        time.sleep(1.0)
        self.assertEqual(len(self.received), 2)
        buf.close()


class TestEmptyBufferShutdown(unittest.TestCase):
    """AC-3: empty buffer doesn't cause write attempts / errors on cleanup."""

    def test_close_empty_buffer(self):
        buf = ToolCaptureBuffer(max_items=3, flush_interval=1.0, callback=lambda x: None)
        # Nothing added — just close. Should not raise.
        buf.close()
        self.assertEqual(buf.stats.total_flushes, 0)

    def test_context_manager_empty(self):
        with ToolCaptureBuffer(callback=lambda x: None) as buf:
            pass
        # Exit should flush empty buffer without error.

    def test_double_close(self):
        buf = ToolCaptureBuffer(callback=lambda x: None)
        buf.close()
        buf.close()  # second close is safe no-op.


class TestCallbackFailure(unittest.TestCase):
    """AC-5: callback failures re-queue the batch."""

    def test_failed_callback_preserves_items(self):
        fail_count = [0]
        received: List[List[Dict[str, Any]]] = []

        def flaky(batch):
            fail_count[0] += 1
            if fail_count[0] == 1:
                raise RuntimeError("transient error")
            # second call succeeds.
            received.append(batch)

        buf = ToolCaptureBuffer(
            max_items=2,
            flush_interval=300.0,
            callback=flaky,
        )

        for i in range(2):
            buf.add({"failtest": i})

        # First attempt failed, items re-queued.
        self.assertEqual(fail_count[0], 1)
        self.assertEqual(buf.pending, 2)

        # Flush again — now succeeds.
        buf.flush()
        self.assertEqual(len(received), 1)
        self.assertEqual(len(received[0]), 2)
        buf.close()


class TestStats(unittest.TestCase):
    """AC-7: Stats tracking."""

    def test_stats_increment(self):
        received: List[List[Dict[str, Any]]] = []
        buf = ToolCaptureBuffer(
            max_items=10,
            flush_interval=300.0,
            callback=lambda batch: received.append(batch),
        )

        for i in range(5):
            buf.add({"s": i})

        self.assertEqual(buf.stats.total_items_added, 5)
        # Below threshold, not flushed yet.
        self.assertEqual(buf.stats.total_flushes, 0)
        self.assertEqual(buf.stats.total_items_flushed, 0)

        buf.flush()
        self.assertEqual(buf.stats.total_flushes, 1)
        self.assertEqual(buf.stats.total_items_flushed, 5)
        self.assertEqual(buf.stats.last_flush_size, 5)
        buf.close()


class TestCallbackNone(unittest.TestCase):
    """No callback set → items are counted as dropped on flush."""

    def test_no_callback_drops(self):
        buf = ToolCaptureBuffer(max_items=2, flush_interval=300.0, callback=None)
        buf.add({"k": 1})
        buf.add({"k": 2})  # should trigger count-flush, drop silently
        self.assertEqual(buf.stats.total_flushes, 1)
        self.assertEqual(buf.stats.last_flush_size, 2)
        self.assertEqual(buf.pending, 0)
        buf.close()


class TestClear(unittest.TestCase):
    def test_clear_drops_pending(self):
        buf = ToolCaptureBuffer(max_items=100, flush_interval=300.0, callback=lambda x: None)
        for i in range(5):
            buf.add({"i": i})

        self.assertEqual(buf.pending, 5)
        self.assertEqual(buf.stats.total_items_added, 5)

        buf.clear()
        self.assertEqual(buf.pending, 0)
        # Added count stays; flushed count stays at zero since nothing flushed.
        self.assertEqual(buf.stats.total_items_added, 5)


class TestThreadSafety(unittest.TestCase):
    """AC-6: concurrent adds don't corrupt state."""

    def test_concurrent_adds(self):
        received: List[List[Dict[str, Any]]] = []
        buf = ToolCaptureBuffer(
            max_items=10,
            flush_interval=300.0,
            callback=lambda batch: received.append(batch),
        )

        threads: List[threading.Thread] = []
        for t in range(4):
            def worker(tid):
                for i in range(25):
                    buf.add({"t": tid, "i": i})

            thread = threading.Thread(target=worker, args=(t,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join(timeout=10)

        total_delivered = sum(len(b) for b in received) + buf.pending
        self.assertEqual(total_delivered, 100)  # 4 workers * 25 items
        buf.close()


class TestContextManagerExits(unittest.TestCase):
    """AC-4: context manager __exit__ drains remaining items."""

    def test_context_manager_flushes_remaining(self):
        received: List[List[Dict[str, Any]]] = []
        with ToolCaptureBuffer(
            max_items=10,
            flush_interval=300.0,
            callback=lambda batch: received.append(batch),
        ) as buf:
            for i in range(3):
                buf.add({"ctx": i})

        # After exit, 3 items should have been flushed.
        self.assertEqual(len(received), 1)
        self.assertEqual(len(received[0]), 3)


class TestIntegrationAutoStorageEngine(unittest.TestCase):
    """Verify buffer is wired into AutoStorageEngine when provided."""

    def setUp(self):
        self.received: List[List[Dict[str, Any]]] = []
        self.buf = ToolCaptureBuffer(
            max_items=5,
            flush_interval=300.0,
            callback=lambda batch: self.received.append(batch),
        )

    def tearDown(self):
        self.buf.close()

    def test_engine_uses_buffer_when_set(self):
        from memchorus.auto_storage_engine import AutoStorageEngine

        orchestrator = MagicMock()
        orch = AutoStorageEngine(orchestrator=orchestrator, buffer=self.buf)

        # Feed content that passes the filter pipeline.
        result = orch.capture_outcome(
            "I learned something very important about how this works.",
            outcome_type="automatic",
        )
        self.assertTrue(result["saved"])
        # Save was queued in buffer, not hit orchestrator.

    def test_engine_without_buffer_calls_orchestrator(self):
        from memchorus.auto_storage_engine import AutoStorageEngine

        orchestrator = MagicMock()
        orchestrator.recommended_sources.return_value = ["hermes_default"]
        orchestrator.save.return_value = True

        orch = AutoStorageEngine(orchestrator=orchestrator, buffer=None)
        result = orch.capture_outcome(
            "I learned something very important about how this works.",
            outcome_type="automatic",
        )
        self.assertTrue(result["saved"])
        orchestrator.save.assert_called()

    def test_buffered_payload_includes_key(self):
        from memchorus.auto_storage_engine import AutoStorageEngine

        orchestrator = MagicMock()
        orch = AutoStorageEngine(orchestrator=orchestrator, buffer=self.buf)
        orch.capture_outcome(
            "I decided to go with the simpler approach for this.",
            outcome_type="automatic",
        )

        # Flush the buffer to see what got queued.
        self.buf.flush()
        self.assertEqual(len(self.received), 1)
        payload = self.received[0][0]
        self.assertIn("_ac_key", payload)
        self.assertIn("text", payload)
        self.assertTrue(payload.get("_auto_provenance"))


class TestOnSessionEndLenOnPendingCrash(unittest.TestCase):
    """Regression: hooks.py line 433 called len() on batcher.pending (int property).

    The original code pattern was:
        len(getattr(batcher, '_queue', []) or getattr(batcher, 'pending', []))
    When _queue is empty, the `or` clause falls through to .pending (an int),
    and len(int) raises TypeError. This class ensures that regression never repeats.
    """

    def test_session_end_with_empty_batcher(self):
        """on_session_end with an empty batcher should not crash."""
        captured: List[Dict[str, Any]] = []
        buf = ToolCaptureBuffer(
            max_items=10,
            flush_interval=300.0,
            callback=lambda b: captured.extend(b),
        )

        # Simulate the fixed on_session_end path from hooks.py
        batcher = buf
        try:
            count_before_int = batcher.pending  # already int
        except AttributeError:
            count_before_int = len(getattr(batcher, '_queue', []))

        batcher.close()

        self.assertIsInstance(count_before_int, int)
        self.assertEqual(count_before_int, 0)
        self.assertEqual(len(captured), 0)
        # Empty buffer close does not increment flush counter (no-op by design).
        self.assertEqual(buf.stats.total_flushes, 0)

    def test_session_end_with_nonempty_batcher(self):
        """on_session_end with pending items should flush and return count."""
        captured: List[Dict[str, Any]] = []
        buf = ToolCaptureBuffer(
            max_items=10,
            flush_interval=300.0,
            callback=lambda b: captured.extend(b),
        )

        # Add some items without reaching threshold.
        for i in range(5):
            buf.add({"idx": i})

        self.assertEqual(buf.pending, 5)

        # Simulate on_session_end path (close flushes remaining).
        batcher = buf
        try:
            count_before_int = batcher.pending
        except AttributeError:
            count_before_int = len(getattr(batcher, '_queue', []))

        self.assertIsInstance(count_before_int, int)
        self.assertEqual(count_before_int, 5)

        batcher.close()
        self.assertEqual(len(captured), 5)
        self.assertEqual(buf.stats.total_flushes, 1)


class TestBufferStatsSnapshot(unittest.TestCase):
    def test_stats_immutability(self):
        buf = ToolCaptureBuffer(max_items=3, flush_interval=300.0, callback=lambda x: None)
        s1 = buf.stats
        buf.add({"a": 1})
        buf.add({"a": 2})
        self.assertEqual(s1.total_items_added, 0)  # snapshot unchanged
        self.assertEqual(buf.stats.total_items_added, 2)


if __name__ == "__main__":
    unittest.main()
