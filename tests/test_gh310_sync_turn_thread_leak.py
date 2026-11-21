"""GH-310 — thread leak in ``sync_turn()``: acceptance tests.

``sync_turn()`` used to spawn a new daemon thread for every call and never
tracked or joined it, so threads accumulated without bound over a long
session.  The fix routes every turn through a **single persistent worker
thread** (named ``memchorus-sync-turn``) consuming a FIFO queue, and drains
that worker (bounded join) at ``shutdown()`` / ``on_session_end()``.

Acceptance criteria (from issue #310):

* repeated ``sync_turn()`` calls never accumulate more than one sync-turn
  thread at a time, and zero after the drain;
* queued turns are actually processed (in order, with the right text);
* an in-flight enforce is allowed to **finish** at teardown (drained, not
  dropped);
* a dead worker is restarted so later turns still get processed (including
  a crashed worker, and the leftover-sentinel discard path);
* a red version gate still skips enforcement (no #284 regression);
* concurrent callers share one worker — the call path stays non-blocking.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest  # noqa: F401  (pytest import convention of sibling suites)

# --- path setup (mirror conftest.py / scope-3 suite) -------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from integrations.provider import (  # noqa: E402
    MemChorusMemoryProvider,
)
from integrations.provider._version_gate import VersionGateStatus  # noqa: E402



def _green():
    return VersionGateStatus(
        installed="2.0.80",
        expected="2.0.80",
        safe_for_reads=True,
        safe_for_writes=True,
        reason="",
        blocked_by="ok",
    )


def _red():
    return VersionGateStatus(
        installed="0.9.0",
        expected="2.0.80",
        safe_for_reads=True,
        safe_for_writes=False,
        reason="Installed SDK is older than the declared minimum.",
        blocked_by="version",
    )


def _make_provider(gate=_green()):
    provider = MemChorusMemoryProvider()
    provider._version_gate_status = gate
    orch = mock.MagicMock()
    provider._external_orchestrator = orch
    return provider


def _em(provider) -> "mock.MagicMock":
    """The mocked enforcement manager behind the provider's mock orchestrator."""
    orch = provider._external_orchestrator
    assert orch is not None
    em = orch._get_enforcement_manager.return_value
    assert em is not None
    return em


def _worker_name(provider) -> str:
    """The provider's sync-turn worker thread name (unique per instance)."""
    return f"memchorus-sync-turn-{id(provider):x}"


def _live_threads_for(provider) -> list:
    """Live threads spawned by *this* provider instance (name-scoped)."""
    name = _worker_name(provider)
    return [t for t in threading.enumerate() if t.name == name]


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestNoThreadLeak:
    """The core GH-310 acceptance: no accumulating untracked threads."""

    def test_repeated_calls_never_spawn_more_than_one_thread(self):
        provider = _make_provider()
        calls: list = []
        _em(provider).enforce.side_effect = calls.append

        for i in range(25):
            provider.sync_turn(f"user {i}", f"assistant {i}")

        assert _wait_until(lambda: len(calls) == 25), calls
        # At most one sync-turn worker exists at any time (old code: 25).
        assert len(_live_threads_for(provider)) <= 1

        # And none survive the drain — session end joins the worker.
        provider.on_session_end(messages=[{"role": "user", "content": "bye"}])
        assert _wait_until(lambda: len(_live_threads_for(provider)) == 0)

    def test_queued_turns_processed_in_order_with_text(self):
        provider = _make_provider()
        calls: list = []
        _em(provider).enforce.side_effect = calls.append

        turns = [("u1", "a1"), ("u2", "a2"), ("u3", "a3")]
        for u, a in turns:
            provider.sync_turn(u, a)

        assert _wait_until(lambda: len(calls) == 3)
        assert calls == [f"{u}\n{a}" for u, a in turns]

        provider.shutdown()  # clean up so no worker leaks into other tests

    def test_concurrent_callers_share_one_worker(self):
        provider = _make_provider()
        calls: list = []
        lock = threading.Lock()

        def rec(text):
            with lock:
                calls.append(text)

        _em(provider).enforce.side_effect = rec

        n = 20
        barrier = threading.Barrier(n)

        def call(i: int) -> None:
            barrier.wait()
            provider.sync_turn(f"u{i}", f"a{i}")

        threads = [
            threading.Thread(target=call, args=(i,), daemon=True) for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert _wait_until(lambda: len(calls) == n)
        assert len(_live_threads_for(provider)) <= 1

        # Drain so this test's worker does not leak into sibling tests.
        provider.on_session_end(messages=[])
        assert _wait_until(lambda: len(_live_threads_for(provider)) == 0)

    def test_red_gate_enqueues_nothing(self):
        """#284 gate regression: a blocked gate must skip enforcement."""
        provider = _make_provider(gate=_red())
        provider.sync_turn("user", "assistant")

        assert provider._enforce_queue.qsize() == 0
        assert provider._enforce_worker is None
        assert _live_threads_for(provider) == []


class TestDrainSemantics:
    """Issue #310 — queued/in-flight work is applied at teardown, not dropped."""

    def test_in_flight_enforce_completes_at_drain(self):
        provider = _make_provider()
        gate = threading.Event()
        em = _em(provider)

        def slow_enforce(text):
            gate.wait(timeout=5.0)  # still running when teardown begins

        em.enforce.side_effect = slow_enforce
        provider.sync_turn("user", "assistant")

        # Worker picked the item up and is now blocked inside enforce().
        assert _wait_until(lambda: em.enforce.called)

        # Teardown starts while enforce() is in flight: the drain must let it
        # finish (bounded join), not kill the thread mid-work.
        def release() -> None:
            time.sleep(0.2)
            gate.set()

        threading.Thread(target=release, daemon=True).start()
        provider.on_session_end(messages=[{"role": "user", "content": "end"}])

        assert em.enforce.call_count == 1
        assert _wait_until(lambda: len(_live_threads_for(provider)) == 0)

    def test_drain_is_idempotent_and_worker_restarts(self):
        provider = _make_provider()
        calls: list = []
        _em(provider).enforce.side_effect = calls.append

        provider.sync_turn("a", "b")
        assert _wait_until(lambda: len(calls) == 1)

        provider.on_session_end(messages=[])
        provider.on_session_end(messages=[])  # double drain: no raise/deadlock

        # A later turn must still work (worker restarted) — and the leftover
        # sentinel from the double drain must not wedge the new worker.
        provider.sync_turn("c", "d")
        assert _wait_until(lambda: len(calls) == 2)
        assert calls[1] == "c\nd"

        provider.shutdown()  # clean up so no worker leaks into other tests

    def test_shutdown_drains_worker(self):
        provider = _make_provider()
        calls: list = []
        _em(provider).enforce.side_effect = calls.append

        provider.sync_turn("turn", "one")
        assert _wait_until(lambda: len(calls) == 1)

        provider.shutdown()
        assert _wait_until(lambda: len(_live_threads_for(provider)) == 0)

        provider.sync_turn("turn", "two")
        assert _wait_until(lambda: len(calls) == 2)
        provider.shutdown()  # clean up


class TestWorkerRestart:
    """A dead worker (crash) must not strand the queue."""

    def test_worker_restarts_after_crash(self):
        provider = _make_provider()
        calls: list = []
        _em(provider).enforce.side_effect = calls.append

        provider.sync_turn("turn", "one")
        assert _wait_until(lambda: len(calls) == 1)

        # Simulate the worker dying (an exception escaping the loop) while it
        # is still the registered worker: the liveness check in
        # _enqueue_enforce must treat it as dead and start a fresh worker.
        dead = threading.Thread(target=lambda: None, daemon=True)
        dead.start()
        dead.join()
        with provider._enforce_worker_lock:
            provider._enforce_worker = dead
        assert not provider._enforce_worker.is_alive()

        provider.sync_turn("turn", "two")
        assert _wait_until(lambda: len(calls) == 2)
        assert calls[1] == "turn\ntwo"

        provider.shutdown()  # clean up the restarted worker
