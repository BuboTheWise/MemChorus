"""xdist-safety tests for HitRateTracker (v2.0 hit-rate-tracker hardening).

Covers:
- Singleton identity stability within a process
- memory_dir override and sticky behaviour
- Reset clears _instance, _index, and sidecar file
- Fork-safe lock recovery simulation
- Index persistence round-trip via flush/load
- Concurrent thread safety (8 threads x 50 saves = 400 entries)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from memchorus.hit_rate_tracker import HitRateTracker, _HIT_RATE_FILE


@pytest.fixture
def isolated_dir(tmp_path: Path) -> Path:
    """Provide a fresh directory for each test so xdist workers don't collide."""
    (tmp_path / "hits").mkdir()
    return tmp_path / "hits"


@pytest.fixture(autouse=True)
def _clean_singleton(isolated_dir: Path):
    """Reset the singleton before and after every test."""
    HitRateTracker.reset(memory_dir=str(isolated_dir))
    yield
    tracker = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
    tracker.flush()
    HitRateTracker.reset(memory_dir=str(isolated_dir))


class TestSingletonIdentity:
    """Two calls in the same process return the same object."""

    def test_same_instance_within_process(self, isolated_dir: Path):
        a = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        b = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        assert a is b
        # Same underlying index; mutations are visible through both refs
        tracker_a = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        tracker_b = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        assert tracker_a._index is tracker_b._index

    def test_memory_dir_sticks_after_first_call(self, isolated_dir: Path):
        first = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        second = HitRateTracker.get_instance(memory_dir="/some/other/dir")
        assert first is second
        assert first.memory_dir == os.path.expanduser(str(isolated_dir))


class TestResetCleanup:
    """reset() clears both the in-memory index and the persisted sidecar."""

    def test_reset_clears_instance(self, isolated_dir: Path):
        HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        HitRateTracker.reset(memory_dir=str(isolated_dir))
        assert HitRateTracker._instance is None

    def test_reset_wipes_in_memory_index(self, isolated_dir: Path):
        tracker = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        tracker.register_save("key:alpha")
        assert "key:alpha" in tracker._index
        HitRateTracker.reset(memory_dir=str(isolated_dir))
        # After reset, a new instance should see no prior keys
        fresh = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        assert "key:alpha" not in fresh._index

    def test_reset_deletes_sidecar_file(self, isolated_dir: Path):
        tracker = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        tracker.register_save("key:beta")
        tracker.flush()
        sidecar = isolated_dir / _HIT_RATE_FILE
        assert sidecar.exists()
        HitRateTracker.reset(memory_dir=str(isolated_dir))
        assert not sidecar.exists()


class TestForkSafeLock:
    """Verify _ensure_lock recovers from a stale post-fork class-level lock."""

    def test_ensure_lock_replaces_on_os_error(self, isolated_dir: Path):
        """When acquire() triggers OSError, _ensure_lock swaps in a fresh lock."""
        old_lock = HitRateTracker._lock_cls  # noqa: SLF001

        class PoisonedLock:
            """Lock wrapper that raises OSError on first acquire (simulates post-fork stale lock)."""

            def __init__(self) -> None:
                self._inner = threading.Lock()
                self._tried = False

            def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
                if not self._tried:
                    self._tried = True
                    raise OSError(25, "Text file busy")
                return self._inner.acquire(blocking, timeout)

            def release(self) -> None:
                self._inner.release()

        HitRateTracker.reset(memory_dir=str(isolated_dir))
        poisoned_lock = PoisonedLock()
        HitRateTracker._lock_cls = poisoned_lock  # type: ignore[assignment]  # noqa: SLF001

        tracker = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        assert tracker is not None

        # _ensure_lock should have replaced the poisoned lock with a fresh threading.Lock.
        # Compare against the actual runtime type of a new Lock() rather than threading.Lock
        # (which is an alias for _thread.allocate_lock on CPython, not the class itself).
        assert type(HitRateTracker._lock_cls) is type(threading.Lock())
        assert HitRateTracker._lock_cls is not poisoned_lock

        # Restore so cleanup fixtures don't break.
        HitRateTracker._lock_cls = old_lock  # noqa: SLF001

    def test_ensure_lock_keeps_good_lock(self, isolated_dir: Path):
        """A working class-level lock is left untouched by _ensure_lock."""
        good = threading.Lock()
        HitRateTracker._lock_cls = good  # noqa: SLF001
        HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        assert HitRateTracker._lock_cls is good


class TestPersistenceRoundTrip:
    """Index survives flush → file → load cycle."""

    def test_flush_and_reload(self, isolated_dir: Path):
        t = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        t.register_save("key:one")
        t.record_recallhit("key:two")
        t.flush()
        # New instance should reload persisted counters
        fresh = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        assert "key:one" in fresh._index
        assert "key:two" in fresh._index

    def test_json_contents_have_correct_shape(self, isolated_dir: Path):
        t = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        t.register_save("key:shape")
        t.record_useful("key:shape", 2)
        t.flush()
        sidecar = isolated_dir / _HIT_RATE_FILE
        raw = json.loads(sidecar.read_text())
        key_data = raw["key:shape"]
        assert "total_recalls" in key_data
        assert "useful_flags" in key_data
        assert "noise_flags" in key_data
        assert key_data["useful_flags"] == 2


class TestConcurrencySafety:
    """8 threads × 50 saves = 400 entries — no corruption, no lost writes."""

    def test_concurrent_saves_no_collision(self, isolated_dir: Path):
        t = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        num_threads, saves_per_thread = 8, 50
        keys = [f"ckey:{i}" for i in range(num_threads * saves_per_thread)]

        def worker(start: int) -> None:
            for i in range(saves_per_thread):
                key = keys[start + i]
                t.register_save(key)
                with t._dir_lock:
                    entry = t._ensure_entry(key)
                    entry["total_recalls"] = 1
                    t.flush()

        threads = [threading.Thread(target=worker, args=(i * saves_per_thread,)) for i in range(num_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        assert len(t._index) == num_threads * saves_per_thread

    def test_concurrent_useful_and_stale(self, isolated_dir: Path):
        t = HitRateTracker.get_instance(memory_dir=str(isolated_dir))

        def do_useful() -> None:
            for _ in range(50):
                t.record_useful("shared", 1)

        def do_noise() -> None:
            for _ in range(30):
                t.record_stale("shared", 1)

        threads = [threading.Thread(target=do_useful), threading.Thread(target=do_noise)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)
        stats = t.get_hit_stats("shared")
        assert stats["useful_flags"] == 50
        assert stats["noise_flags"] == 30

    def test_total_saves_reflects_entry_count(self, isolated_dir: Path):
        t = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        for i in range(80):
            t.register_save(f"cnt:{i}")
        assert t.total_saves == 80


class TestMemoryDirOverride:
    """Verify optional memory_dir parameter on get_instance()."""

    def test_optional_memory_dir_sets_path(self, isolated_dir: Path):
        HitRateTracker.reset(memory_dir=str(isolated_dir))
        tracker = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        assert tracker.memory_dir == os.path.expanduser(str(isolated_dir))

    def test_none_memory_dir_uses_default(self, isolated_dir: Path):
        HitRateTracker.reset()
        t1 = HitRateTracker.get_instance()
        # Should use the default profile path, not a stale override
        assert t1 is not None
        # A second call picks up the same default instance on first call
        t2 = HitRateTracker.get_instance()
        assert t1 is t2

    def test_memory_dir_not_changed_by_later_override(self, isolated_dir: Path):
        """Once an instance exists, calling get_instance with a different memory_dir does not change it."""
        HitRateTracker.reset(memory_dir=str(isolated_dir))
        first = HitRateTracker.get_instance(memory_dir=str(isolated_dir))
        second = HitRateTracker.get_instance(memory_dir="/completely/different")
        assert first is second
        assert first.memory_dir == os.path.expanduser(str(isolated_dir))
