"""HitRateTracker — verify counters increment on recall, persist across runs, handle missing _hit_rate key."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from memchorus.hit_rate_tracker import (
    HitRateTracker,
    _HIT_RATE_FILE,
    _resolve_hit_rate_file,
    _load_index,
    _save_index,
)


@pytest.fixture(autouse=True)
def _reset_tracker():
    """Reset the singleton before each test to prevent cross-test contamination."""
    HitRateTracker.reset()
    yield
    HitRateTracker.reset()


@pytest.fixture
def memory_dir(tmp_path):
    """Provide a temporary directory as memory_dir."""
    subdir = tmp_path / "test_memories"
    subdir.mkdir()
    return str(subdir)


class TestResolveAndPersistence:
    """Verify sidecar file resolution and load/save helpers."""

    def test_resolve_hit_rate_file(self, memory_dir):
        expected = Path(memory_dir) / _HIT_RATE_FILE
        assert _resolve_hit_rate_file(memory_dir) == expected

    def test_load_index_missing_returns_empty(self, memory_dir):
        result = _load_index(memory_dir)
        assert result == {}

    def test_load_index_corrupt_json_falls_back_to_empty(self, memory_dir):
        sidecar = Path(memory_dir) / _HIT_RATE_FILE
        sidecar.write_text("this is not json at all {{{")
        result = _load_index(memory_dir)
        assert result == {}

    def test_save_and_load_roundtrip(self, memory_dir):
        index = {"entry_42": {"total_recalls": 7, "useful_flags": 3, "noise_flags": 1}}
        _save_index(memory_dir, index)
        loaded = _load_index(memory_dir)
        assert loaded == index

    def test_load_index_non_dict_raw_falls_back(self, memory_dir):
        sidecar = Path(memory_dir) / _HIT_RATE_FILE
        sidecar.write_text('"just a string"')
        result = _load_index(memory_dir)
        assert result == {}


class TestRegisterSave:
    """Verify entry registration and counter initialization."""

    def test_register_save_creates_entry(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("doc_alpha")
        stats = tracker.get_hit_stats("doc_alpha")
        assert stats["total_recalls"] == 0
        assert "first_saved_at" in stats
        assert "last_seen_at" in stats

    def test_register_save_multiple_entries(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("entry_a")
        tracker.register_save("entry_b")
        assert "entry_a" in tracker._index
        assert "entry_b" in tracker._index

    def test_total_saves_reflects_entry_count(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("x1")
        tracker.register_save("x2")
        tracker.register_save("x3")
        assert tracker.total_saves == 3


class TestRecordRecallhit:
    """Verify recall counters increment correctly."""

    def test_record_recallhit_increments_total_recalls(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("entry_one")
        tracker.record_recallhit("entry_one")
        stats = tracker.get_hit_stats("entry_one")
        assert stats["total_recalls"] == 1

    def test_record_recallhit_multiple_times(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("entry_multi")
        for _ in range(5):
            tracker.record_recallhit("entry_multi")
        stats = tracker.get_hit_stats("entry_multi")
        assert stats["total_recalls"] == 5

    def test_total_recalls_aggregates_across_entries(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("e1")
        tracker.register_save("e2")
        tracker.record_recallhit("e1")
        tracker.record_recallhit("e1")
        tracker.record_recallhit("e2")
        assert tracker.total_recalls == 3


class TestFeedbackSignals:
    """Verify useful/noise flag recording."""

    def test_record_useful(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("entry_u")
        tracker.record_useful("entry_u", count=1)
        stats = tracker.get_hit_stats("entry_u")
        assert stats["useful_flags"] == 1

    def test_record_useful_accumulates_with_count(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("entry_ua")
        tracker.record_useful("entry_ua", count=3)
        tracker.record_useful("entry_ua", count=2)
        stats = tracker.get_hit_stats("entry_ua")
        assert stats["useful_flags"] == 5

    def test_record_stale(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("entry_n")
        tracker.record_stale("entry_n", count=1)
        stats = tracker.get_hit_stats("entry_n")
        assert stats["noise_flags"] == 1

    def test_record_stale_accumulates(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("entry_na")
        tracker.record_stale("entry_na", count=2)
        tracker.record_stale("entry_na", count=3)
        stats = tracker.get_hit_stats("entry_na")
        assert stats["noise_flags"] == 5


class TestPersistenceAcrossRuns:
    """Verify data persists and reloads correctly between instances."""

    def test_flush_and_reload(self, memory_dir):
        tracker1 = HitRateTracker.get_instance(memory_dir)
        tracker1.register_save("persist_entry")
        tracker1.record_recallhit("persist_entry")
        tracker1.record_useful("persist_entry", count=2)
        tracker1.flush()

        # Reset singleton to simulate new process
        HitRateTracker.reset()

        tracker2 = HitRateTracker.get_instance(memory_dir)
        stats = tracker2.get_hit_stats("persist_entry")
        assert stats["total_recalls"] == 1
        assert stats["useful_flags"] == 2

    def test_get_all_stats_snapshot(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("snap_a")
        tracker.register_save("snap_b")
        tracker.record_recallhit("snap_a")
        snapshot = tracker.get_all_stats()
        assert "snap_a" in snapshot
        assert "snap_b" in snapshot
        assert snapshot["snap_a"]["total_recalls"] == 1

    def test_first_run_scenario_missing_hit_rate_graceful(self, memory_dir):
        """On first run when no sidecar exists, tracker still works."""
        tracker = HitRateTracker.get_instance(memory_dir)
        # Sidecar doesn't exist yet, but registering should work
        tracker.register_save("fresh_start")
        stats = tracker.get_hit_stats("fresh_start")
        assert stats["total_recalls"] == 0
        assert "first_saved_at" in stats


class TestReset:
    """Verify clean reset between test runs."""

    def test_reset_clears_singleton(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("will_be_gone")
        assert tracker in list(HitRateTracker._instances.values())
        HitRateTracker.reset()
        assert list(HitRateTracker._instances.values()) == []

    def test_reset_with_memory_dir_deletes_sidecar(self, memory_dir):
        tracker = HitRateTracker.get_instance(memory_dir)
        tracker.register_save("will_be_gone")
        tracker.flush()
        sidecar = Path(memory_dir) / _HIT_RATE_FILE
        assert sidecar.exists()

        HitRateTracker.reset(memory_dir=memory_dir)
        assert not sidecar.exists()
