"""
Tests for HitRateTracker — auto-tuning hit-rate persistence and counter mutations.

Acceptance criteria (AUTOTUNING.md §7):
  AC-H1: Counters increment correctly on recall events.
  AC-H2: Index persists to disk as JSON across process restarts.
  AC-H3: Missing _hit_rate key initializes gracefully (first-run scenario).
  AC-H4: register_save() records new entry paths with correct timestamps.
  AC-H5: flush() writes atomically via .tmp swap (no partial corruption).
  AC-H6: Thread-safe access — concurrent record_recallhit calls do not corrupt counters.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.hit_rate_tracker import HitRateTracker, _resolve_hit_rate_file


def _reset_tracker() -> HitRateTracker:
    """Reset singleton and return a fresh instance pointing to /dev/shm or similar."""
    HitRateTracker.reset()
    return HitRateTracker.get_instance()


# ---------------------------------------------------------------------------
# AC-H1: Counter mutations on recall/save/useful/stale
# ---------------------------------------------------------------------------


class TestHitRateCounters(unittest.TestCase):
    """Counter increment correctness."""

    def setUp(self) -> None:
        self.tracker = _reset_tracker()

    def test_register_save_creates_entry(self) -> None:
        key = "/tmp/test_memory_store.json"
        self.tracker.register_save(key)
        stats = self.tracker.get_hit_stats(key)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["total_recalls"], 0)
        self.assertEqual(stats["useful_flags"], 0)
        self.assertEqual(stats["noise_flags"], 0)
        self.assertIn("first_saved_at", stats)

    def test_record_recallhit_increments_counter(self) -> None:
        key = "/tmp/test_recalled_entry.json"
        self.tracker.register_save(key)
        for _ in range(5):
            self.tracker.record_recallhit(key)
        stats = self.tracker.get_hit_stats(key)
        self.assertEqual(stats["total_recalls"], 5)

    def test_record_useful_increments_flag(self) -> None:
        key = "/tmp/test_useful.json"
        self.tracker.register_save(key)
        for _ in range(3):
            self.tracker.record_useful(key)
        stats = self.tracker.get_hit_stats(key)
        self.assertEqual(stats["useful_flags"], 3)

    def test_record_stale_increments_noise(self) -> None:
        key = "/tmp/test_stale.json"
        self.tracker.register_save(key)
        for _ in range(2):
            self.tracker.record_stale(key)
        stats = self.tracker.get_hit_stats(key)
        self.assertEqual(stats["noise_flags"], 2)

    def test_unregistered_key_returns_none_for_get_stats(self) -> None:
        # get_hit_stats initialises on first call — so it returns fresh zeros
        stats = self.tracker.get_hit_stats("/nonexistent/key")
        self.assertIsNotNone(stats)
        self.assertEqual(stats["total_recalls"], 0)

    def test_total_saves_returns_entry_count(self) -> None:
        self.tracker.register_save("/a.json")
        self.tracker.register_save("/b.json")
        self.assertEqual(self.tracker.total_saves, 2)

    def test_total_recalls_aggregates(self) -> None:
        self.tracker.register_save("/x.json")
        self.tracker.register_save("/y.json")
        self.tracker.record_recallhit("/x.json")
        self.tracker.record_recallhit("/x.json")
        self.tracker.record_recallhit("/y.json")
        self.assertEqual(self.tracker.total_recalls, 3)


# ---------------------------------------------------------------------------
# AC-H2: Disk persistence across flush/load cycles
# ---------------------------------------------------------------------------


class TestHitRatePersistence(unittest.TestCase):
    """JSON index persists and reloads correctly."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        HitRateTracker.reset()
        # Instantiate directly with our temp dir
        self.tracker = HitRateTracker(self.tmpdir)
        HitRateTracker._instance = self.tracker

    def tearDown(self) -> None:
        HitRateTracker.reset()
        for f in Path(self.tmpdir).glob("*"):
            if f.is_file():
                f.unlink()
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def test_flush_writes_json_to_disk(self) -> None:
        self.tracker.register_save("/tmp/persist_1.json")
        self.tracker.record_recallhit("/tmp/persist_1.json")
        self.tracker.flush()
        index_file = _resolve_hit_rate_file(self.tmpdir)
        self.assertTrue(index_file.exists())
        data = json.loads(index_file.read_text())
        self.assertIn("/tmp/persist_1.json", data)
        self.assertEqual(data["/tmp/persist_1.json"]["total_recalls"], 1)

    def test_reload_from_existing_index(self) -> None:
        # First pass: write data
        self.tracker.register_save("/tmp/reload_key.json")
        self.tracker.record_recallhit("/tmp/reload_key.json")
        self.tracker.flush()

        # Second pass: create new instance pointing to same dir — should load
        HitRateTracker.reset()
        tracker2 = HitRateTracker(self.tmpdir)
        HitRateTracker._instance = tracker2
        stats = tracker2.get_hit_stats("/tmp/reload_key.json")
        self.assertIsNotNone(stats)
        self.assertEqual(stats["total_recalls"], 1)

    def test_atomic_swap_on_flush(self) -> None:
        """Atomic .tmp + os.replace ensures no partial writes."""
        self.tracker.register_save("/tmp/atomic.json")
        self.tracker.flush()
        tmp_file = _resolve_hit_rate_file(self.tmpdir).with_suffix(".tmp.json")
        self.assertFalse(tmp_file.exists())


# ---------------------------------------------------------------------------
# AC-H3: Missing _hit_rate key gracefully initializes
# ---------------------------------------------------------------------------


class TestMissingHitRateKey(unittest.TestCase):
    """First-run scenario where no index or entry exists."""

    def setUp(self) -> None:
        self.tracker = _reset_tracker()

    def test_record_recallhit_on_unregistered_key_silent(self) -> None:
        """Record recall on a key that was never registered — should not crash."""
        self.tracker.record_recallhit("/never/registered/key")
        # Should have auto-initialised
        stats = self.tracker.get_hit_stats("/never/registered/key")
        self.assertEqual(stats["total_recalls"], 1)

    def test_get_all_stats_empty_when_no_entries(self) -> None:
        all_stats = self.tracker.get_all_stats()
        self.assertEqual(all_stats, {})


# ---------------------------------------------------------------------------
# AC-H6: Thread safety on concurrent access
# ---------------------------------------------------------------------------


class TestHitRateThreadSafety(unittest.TestCase):
    """Concurrent record_recallhit calls do not corrupt counters."""

    def setUp(self) -> None:
        self.tracker = _reset_tracker()

    def test_concurrent_recalls_safe(self) -> None:
        key = "/tmp/concurrent_key"
        self.tracker.register_save(key)
        threads = []
        for _ in range(10):
            t = threading.Thread(target=self._burst_recalls, args=(key,))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stats = self.tracker.get_hit_stats(key)
        # 10 threads * 5 recalls each
        self.assertEqual(stats["total_recalls"], 50)

    @staticmethod
    def _burst_recalls(key: str) -> None:
        for _ in range(5):
            HitRateTracker.get_instance().record_recallhit(key)


if __name__ == "__main__":
    unittest.main()
