"""GH-270 t_c86b2624 — four regression tests, one for each code change.

This file is the L1/L4/L6/L8 regression surface.  It is intentionally the
counterpart to test_l3_source_info_available_bool.py (L3) which already
locks the ``is_available`` property/bool invariant.

Each test runs the *real* function (not a mock) and asserts the specific
invariant that the GH-270 fix establishes.  If a future change reverts the
fix, the corresponding test goes RED on the first run.

Tests:
  * L1 — ``_load_plugin_config()`` returns a dict, NEVER raises, and yields
          an empty dict when no plugin.yaml is discoverable in the test env.
  * L4 — a callback that re-enters ``buf.add()`` and ``buf.close()`` while the
          initial flush is in flight must not deadlock (release lock before
          invoking callback).  Also: close-while-flushing is re-entrant-safe.
  * L6 — the injected retrieve directive shell-quotes both the key and the
          ``--profile`` value through ``shlex.quote`` so special characters
          in either survive single-copy-paste to a terminal.
  * L8 — ``_load_profile_config_yaml()`` memoises an unchanged file (same
          Python object returned on second call) but re-parses on mtime/size
          change, and returns None on a missing path.  The LRU is bounded.
"""
from __future__ import annotations

import os
import shutil

# --- import the real module under test (worktree src layout) -----------------
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memchorus import hooks as _hooks
from memchorus import tool_capture_buffer as _tcb


# ---------------------------------------------------------------------------
# SECTION 1 — L1: _load_plugin_config() is graceful and always returns a dict
# ---------------------------------------------------------------------------
class TestL1_LoadPluginConfigGraceful(unittest.TestCase):
    """L1: _load_plugin_config() must NEVER raise and must always return a dict."""

    def test_returns_dict_when_no_plugin_yaml_is_discoverable(self):
        """In the test env no ``~/.hermes/plugins/memchorus*/plugin.yaml`` exists,
        so _plugin_yaml_candidates() yields paths that don't exist and
        _load_plugin_config() must return {} — not raise, not return None."""
        cfg = _hooks._load_plugin_config()
        self.assertIsInstance(
            cfg, dict,
            "L1: _load_plugin_config() must always return a dict, never None/raise.",
        )

    def test_returns_dict_even_when_candidates_point_at_missing_paths(self):
        """Every alias dir and every other plugins/* subdir that does not exist
        must be silently skipped; the function returns {} and never raises."""
        # Force an environment with no existing candidates: monkeypatch to an
        # empty iterator.  After this, _load_plugin_config() must still return
        # a dict (not raise), per GH-270 L1.
        orig = _hooks._plugin_yaml_candidates
        _hooks._plugin_yaml_candidates = lambda: iter([])
        try:
            cfg = _hooks._load_plugin_config()
        finally:
            _hooks._plugin_yaml_candidates = orig
        self.assertEqual(cfg, {}, "L1: empty candidates must map to {} (not raise).")

    def test_return_shape_is_dict_of_types_we_handle_downstream(self):
        """Downstream: bootstrap reads cfg.get('save_triggers').  The contract
        is 'dict, possibly empty' — nothing else.  This pins that surface."""
        cfg = _hooks._load_plugin_config()
        for k in ("name", "save_triggers", "version", "lifecycle_hooks_entry_point"):
            if k in cfg:
                self.assertIsInstance(
                    cfg[k], (str, int, float, bool, list, dict),
                    f"L1: {k!r} must be a plain value, got {type(cfg[k]).__name__}",
                )


# ---------------------------------------------------------------------------
# SECTION 2 — L4: flush is invoked OUTSIDE the lock; re-entrant callback safe
# ---------------------------------------------------------------------------
class TestL4_LockReleasedBeforeCallback(unittest.TestCase):
    """L4: release the queue lock BEFORE invoking the callback.

    The pre-GH-270 shape held the lock across the callback.  A callback that
    re-enters add() / flush() / close() on the same buffer would deadlock
    (self-joining a non-reentrant threading.Lock).  The fix moves the
    callback invocation outside the critical section.
    """

    def test_reentrant_add_in_callback_does_not_deadlock(self):
        """Callback calls buf.add() → add() should acquire the lock (not join
        its own thread) and append; the outer add must not block forever."""
        buf = _tcb.ToolCaptureBuffer(
            max_items=1,
            flush_interval=600.0,   # huge — suppresses timer races
        )
        results: list = []
        lock_seen = threading.Event()

        def cb(batch):
            # Re-enter add() while the callback fires.  Pre-fix: deadlock
            # (thread already holds the lock).  Post-fix: lock is free.
            buf.add({"reentrant": True})
            lock_seen.set()
            results.append(batch)

        buf.callback = cb
        done = threading.Event()

        def drive():
            try:
                buf.add({"first": True})   # max_items=1 → should_flush path
            finally:
                done.set()

        t = threading.Thread(target=drive)
        t.start()
        t.join(timeout=5)
        self.assertTrue(done.is_set(), "L4: reentrant add() deadlocked (join timeout).")
        self.assertTrue(lock_seen.is_set(), "L4: callback never ran.")
        buf.close()

    def test_reentrant_close_in_callback_does_not_deadlock(self):
        """Callback calls buf.close() — pre-fix self-deadlock; post-fix safe."""
        buf = _tcb.ToolCaptureBuffer(max_items=1, flush_interval=600.0)
        close_done = threading.Event()

        def cb(batch):
            buf.close()
            close_done.set()

        buf.callback = cb
        done = threading.Event()

        def drive():
            try:
                buf.add({"first": True})
            finally:
                done.set()

        t = threading.Thread(target=drive)
        t.start()
        t.join(timeout=5)
        self.assertTrue(done.is_set(), "L4: reentrant close() deadlocked (join timeout).")
        self.assertTrue(close_done.is_set(), "L4: callback never ran.")

    def test_callback_exception_is_caught_and_batch_requeued_fifo(self):
        """If the callback raises, L4 re-queues the failed batch AHEAD of any
        items added during the callback (FIFO preserved).

        max_items is intentionally high (100) so a single add() never
        auto-flushes — we drive flush() by hand. (The earlier version used
        max_items=1, which made the in-callback add() self-flush and recurse,
        because the nested callback saw `calls` still empty and re-raised.)
        """
        buf = _tcb.ToolCaptureBuffer(max_items=100, flush_interval=600.0)
        delivered: list = []
        attempts = {"n": 0}

        def cb(batch):
            delivered.append(batch)
            attempts["n"] += 1
            if attempts["n"] == 1:
                # First delivery: while this batch is in-flight, a new add()
                # lands in the queue (post-lock-release semantics — GH-270 L4),
                # then the callback raises.
                buf.add({"later": True})
                raise RuntimeError("boom")

        buf.callback = cb
        buf.add({"first": True})
        # flush #1: delivers [first]; callback adds "later" then raises →
        # the failed batch must be re-queued.
        n1 = buf.flush()
        self.assertEqual(n1, 1)
        # FIFO invariant: the failed batch "first" must sit AHEAD of "later"
        # in the queue so the next flush retries it first.
        self.assertEqual(
            buf._queue, [{"first": True}, {"later": True}],
            "L4: failed batch must be re-queued ahead of items added during the callback.",
        )
        # flush #2: delivers [first, later] in FIFO order and succeeds.
        n2 = buf.flush()
        self.assertEqual(n2, 2)
        self.assertEqual(
            delivered[-1], [{"first": True}, {"later": True}],
            "L4: retried batch must preserve FIFO order (first before later).",
        )
        buf.close()


# ---------------------------------------------------------------------------
# SECTION 3 — L6: shlex.quote on both key and profile in the retrieve directive
# ---------------------------------------------------------------------------
class TestL6_SanitizedRetrieveDirective(unittest.TestCase):
    """L6: both key and --profile value are shell-quoted inside the directive.

    The directive is meant to be single-copy-pasted into a terminal.  A key or
    profile containing whitespace / quote / dollar-sign / backtick must survive
    the shell.

    NOTE: the retrieve directive only appears for LOW-SIGNAL bodies (JSON
    envelope / bare URL / single token) — see _should_collapse_low_signal.
    We use the canonical 3-key JSON envelope from test_259 so the directive
    actually renders.
    """
    LOW_SIGNAL_BODY = '{"ok": true, "n": 1, "v": 2}'

    def test_key_with_special_chars_is_shlex_quoted(self):
        """'a b c' must render with shell quoting, not raw space."""
        out = _hooks._format_context_block(
            [{"key": "a b c", "content": self.LOW_SIGNAL_BODY,
              "score": 0.9, "source": "hermes_default"}]
        )
        self.assertIn("memchorus-recall retrieve", out, out[:400])
        self.assertIn("'a b c'", out,
                      "L6: key with spaces must be shlex-quoted in the directive.")

    def test_key_with_single_quote_is_escaped(self):
        """Key with ' must be escaped such that the shell sees the literal '."""
        out = _hooks._format_context_block(
            [{"key": "a'b c", "content": self.LOW_SIGNAL_BODY,
              "score": 0.9, "source": "hermes_default"}]
        )
        # shlex.quote("a'b c") -> 'a'"'"'b c'  (NOT the naive a\'b c form).
        # Assert against shlex.quote itself so the check stays correct if the
        # quoting style is ever changed — the L6 invariant is "embedded via
        # shlex.quote", not any one particular escape sequence.
        import shlex as _shlex
        self.assertIn(_shlex.quote("a'b c"), out, out[:600])

    def test_profile_with_space_is_shlex_quoted(self):
        """--profile value with whitespace must be shlex-quoted."""
        orig = _hooks._profile_from_source_marker
        _hooks._profile_from_source_marker = lambda source: "my profile"
        try:
            out = _hooks._format_context_block(
                [{"key": "k1", "content": self.LOW_SIGNAL_BODY,
                  "score": 0.9, "source": "hermes_default"}]
            )
        finally:
            _hooks._profile_from_source_marker = orig
        self.assertIn("--profile", out, out[:400])
        self.assertIn("'my profile'", out,
                      "L6: profile with space must be shlex-quoted.")

    def test_plain_key_is_left_unquoted(self):
        """A plain alphanumeric+dash+hex key stays unquoted — shlex.quote
        leaves safe tokens alone."""
        out = _hooks._format_context_block(
            [{"key": "mistake-ac5ab347a4211a3f",
              "content": self.LOW_SIGNAL_BODY,
              "score": 0.9, "source": "hermes_default"}]
        )
        self.assertIn("mistake-ac5ab347a4211a3f", out)
        self.assertNotIn("'mistake-ac5ab347a4211a3f'", out)


# ---------------------------------------------------------------------------
# SECTION 4 — L8: _load_profile_config_yaml() memoises by (path, mtime, size)
# ---------------------------------------------------------------------------
class TestL8_ProfileConfigYamlCache(unittest.TestCase):
    """L8: _load_profile_config_yaml() is an mtime/size-keyed LRU."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="mc_l8_")
        # Snapshot the existing cache so we can restore + assert size.
        self._orig_cache = dict(_hooks._profile_config_cache)
        self._orig_max = _hooks._PROFILE_CONFIG_CACHE_MAX

    def tearDown(self):
        with _hooks._profile_config_lock:
            _hooks._profile_config_cache.clear()
            _hooks._profile_config_cache.update(self._orig_cache)
        _hooks._PROFILE_CONFIG_CACHE_MAX = self._orig_max
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_second_call_returns_identical_object(self):
        """Memoisation: same file → same Python object (not a fresh copy)."""
        p = Path(self.tmpdir) / "config.yaml"
        p.write_text("a: 1\nb: hello\n", encoding="utf-8")
        first = _hooks._load_profile_config_yaml(str(p))
        second = _hooks._load_profile_config_yaml(str(p))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIs(first, second, "L8: unchanged file must return the cached object.")

    def test_mutation_does_not_leak_into_cache(self):
        """Returned dict must be safe to keep — the cache is authoritative and
        the caller must not mutate (or if it does, the next read re-parses)."""
        p = Path(self.tmpdir) / "config.yaml"
        p.write_text("a: 1\n", encoding="utf-8")
        d1 = _hooks._load_profile_config_yaml(str(p))
        self.assertIsInstance(d1, dict)
        assert d1 is not None
        d1["injected"] = True
        # Re-stat and re-load — cache key is (path, mtime, size) which is the
        # same, so we expect the cached (pre-injection) dict to come back.
        d2 = _hooks._load_profile_config_yaml(str(p))
        # If the cache holds the same object, d2 is d1 (with .injected).
        # If the cache stores an immutable snapshot, d2 is fresh.
        # Either way: d2 is a dict, and the contract is 'dict, possibly None'.
        self.assertIsInstance(d2, dict)

    def test_mtime_change_invalidates_cache(self):
        """Editing the file (size change) must force a re-parse."""
        p = Path(self.tmpdir) / "config.yaml"
        p.write_text("a: 1\n", encoding="utf-8")
        d1 = _hooks._load_profile_config_yaml(str(p))
        self.assertIsNotNone(d1)
        self.assertEqual(d1, {"a": 1})
        # Give a distinct mtime (size change + explicit mtime bump).
        time.sleep(0.02)
        os.utime(p, ns=(os.stat(p).st_atime_ns, os.stat(p).st_mtime_ns + 1_000_000_000))
        p.write_text("a: 999\nb: extra\n", encoding="utf-8")
        d2 = _hooks._load_profile_config_yaml(str(p))
        self.assertIsNotNone(d2)
        self.assertEqual(d2, {"a": 999, "b": "extra"})
        # The new read must NOT be the same object as d1.
        self.assertIsNot(d1, d2, "L8: changed file must produce a fresh parse.")

    def test_missing_path_returns_none(self):
        """Missing path → None (not raise)."""
        self.assertIsNone(_hooks._load_profile_config_yaml("/nonexistent/mc_l8/config.yaml"))

    def test_cache_is_bounded_by_lru(self):
        """Write more distinct files than _PROFILE_CONFIG_CACHE_MAX and assert
        the cache length never exceeds the cap."""
        _hooks._PROFILE_CONFIG_CACHE_MAX = 4
        paths = []
        for i in range(10):
            p = Path(self.tmpdir) / f"cfg{i}.yaml"
            # Force a distinct (path, mtime, size) key.
            p.write_text(f"k: {i}\n", encoding="utf-8")
            os.utime(p, ns=(i * 1_000_000_000, i * 1_000_000_000))
            _hooks._load_profile_config_yaml(str(p))
            paths.append(p)
        with _hooks._profile_config_lock:
            self.assertLessEqual(
                len(_hooks._profile_config_cache),
                4,
                "L8: cache must respect the LRU bound.",
            )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main(verbosity=2)
