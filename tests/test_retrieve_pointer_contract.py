#!/usr/bin/env python3
"""Tests for Issue #221 — the read contract on a low-signal-gate pointer.

The recall design is pointer-based: the low-signal gate collapses a bulky
body to ``read it: retrieve(key='…')`` on the premise that the body "stays
fully reachable via retrieve(key=…)" (stated in the gate's own docstring).
But ``mempalace_memory_source.retrieve()`` returns a bare, hard ``None`` on a
cache miss, so a *valid but cold/pruned* pointer dangles with no diagnostic and
no way to tell "cold, re-fetch me" from "never existed."

#221 makes the miss *tell the truth* and the directive *state* the boundary:

  1. ``retrieve(key)`` distinguishes three outcomes:
        - key present, body non-empty   -> the body (unchanged)
        - key present, body empty       -> the empty value ("" / {}) -- NOT the sentinel
        - key NOT found in the cache    -> a distinguishable not-found result
                                           (``RETRIEVE_MISS``), not a bare ``None``
  2. the collapse directive the agent follows states the cold-cache possibility.
  3. ``retrieve(key, fallback="live")`` re-fetches on a miss and returns the
     body when a live source can supply it (the miss case only — a hit is
     never wrapped, so existing ``result == payload`` round-trip tests stay green).

The sentinel / fallback surface does not exist at the base commit, so the tagged
tests FAIL RED; the "present, non-empty" case is a regression guard.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.mempalace_memory_source import MemPalaceMemorySource

try:
    from memchorus.mempalace_memory_source import RETRIEVE_MISS  # noqa: E402

    _HAS_MISS = True
    _MISS_TYPE = type(RETRIEVE_MISS)
except ImportError:  # RED until #221 lands
    _HAS_MISS = False
    _MISS_TYPE = None


def _src(tmp_path):
    return MemPalaceMemorySource(
        config={"skip_mcp": True, "cache_dir": str(tmp_path)}
    )


# =========================================================================== #
#  SECTION 1 — the three-outcome read contract                                #
# =========================================================================== #
class TestRetrieveOutcomes:
    def test_present_nonempty_returns_body(self, tmp_path):
        # Regression guard, unchanged at HEAD and after the fix.
        src = _src(tmp_path)
        src.save("k1", {"text": "the body"})
        out = src.retrieve("k1")
        assert out == {"text": "the body"}

    def test_present_empty_is_not_the_miss_sentinel(self, tmp_path):
        src = _src(tmp_path)
        src.save("empty_key", "")
        out = src.retrieve("empty_key")
        # Present-but-empty returns the empty VALUE (""), not the sentinel.
        assert out is not None
        if _HAS_MISS:
            assert not isinstance(out, _MISS_TYPE)
        assert out == ""

    def test_not_found_is_distinguishable_from_empty(self, tmp_path):
        assert _HAS_MISS, "RETRIEVE_MISS sentinel is not defined yet (#221)"
        src = _src(tmp_path)
        src.save("empty_key", "")
        empty_out = src.retrieve("empty_key")
        miss_out = src.retrieve("does_not_exist")
        # A cold/absent key is a distinct, non-None result ...
        assert miss_out is not None
        assert isinstance(miss_out, _MISS_TYPE)
        # ... and is NOT the same thing as a present-but-empty body.
        assert empty_out != miss_out
        assert empty_out == ""

    def test_miss_sentinel_is_stable_identity(self):
        # A single well-known sentinel, so callers can do ``is`` / ``isinstance``
        # and the orchestrator can fold it back to None at the boundary.
        assert _HAS_MISS
        assert RETRIEVE_MISS is RETRIEVE_MISS


# =========================================================================== #
#  SECTION 2 — opt-in live re-fetch on a miss                                 #
# =========================================================================== #
class TestFallbackLive:
    def test_fallback_live_refetches_on_miss(self, tmp_path):
        src = _src(tmp_path)
        calls = {"n": 0}

        def _refetch(key):
            calls["n"] += 1
            return {"refetched": True, "key": key}

        src._refetch_live = _refetch
        out = src.retrieve("cold_key", fallback="live")
        assert calls["n"] == 1
        assert out == {"refetched": True, "key": "cold_key"}

    def test_fallback_live_still_sentinel_when_live_empty(self, tmp_path):
        assert _HAS_MISS
        src = _src(tmp_path)
        src._refetch_live = lambda key: None
        out = src.retrieve("cold_key", fallback="live")
        assert isinstance(out, _MISS_TYPE)

    def test_warm_hit_ignores_fallback_and_is_not_sentinel(self, tmp_path):
        # A warm hit is returned raw (preserving ``result == payload``) — the
        # fallback path must not wrap a hit, only rescue a miss.
        src = _src(tmp_path)
        src.save("warm", {"text": "here"})
        # A live re-fetch that would return something DIFFERENT must not fire on
        # a warm key: the cached value wins.
        src._refetch_live = lambda key: {"not": "the warm body"}
        out = src.retrieve("warm", fallback="live")
        assert out == {"text": "here"}

    # ---- #238: default (non-stubbed) _refetch_live, live-hit -> body -------- #
    def test_default_refetch_live_hit_rescues_cold_miss(self, tmp_path):
        """#238: with NO `_refetch_live` monkey-patched, the default implementation
        must ask the live `search` surface for the key and, on a hit, return the
        body — so a valid-but-cold pointer is re-landed, not dangling."""
        src = _src(tmp_path)
        # A key that is NOT in the local cache…
        calls = []

        def _fake_search(query, limit=10, *, wing=None, room=None):
            calls.append(query)
            assert query == "COLD_KEY"
            return [{"key": "LIVE/1", "content": "the re-landed body", "source": "mcp_live"}]

        src.search = _fake_search
        out = src.retrieve("COLD_KEY", fallback="live")
        assert calls, "default _refetch_live must attempt a live search on a miss"
        assert out is not None
        assert isinstance(out, str) and out == "the re-landed body"

    # ---- #238: default (non-stubbed) _refetch_live, live-empty -> sentinel -- #
    def test_default_refetch_live_exhausted_returns_sentinel(self, tmp_path):
        """#238: with NO `_refetch_live` monkey-patched, when the live `search`
        surface has nothing for the key, the default must degrade to the
        distinguishable sentinel (not a bare None, not a crash)."""
        src = _src(tmp_path)
        assert _HAS_MISS

        def _empty_live(query, limit=10, *, wing=None, room=None):
            return []  # live surface is also cold

        src.search = _empty_live
        out = src.retrieve("COLD_KEY", fallback="live")
        assert isinstance(out, _MISS_TYPE), (
            "live-exhausted cold miss must be the distinguishable sentinel, got %r" % (out,)
        )


# =========================================================================== #
#  SECTION 3 — the collapsed directive states the cold-cache boundary         #
# =========================================================================== #
class TestDirectiveColdNote:
    def test_directive_contains_cold_cache_note(self):
        from memchorus.hooks import _format_context_block

        # A short JSON-blob body trips the #217 signal gate -> rendered as the
        # one-line ``read it: retrieve(key='…')`` directive.  That directive
        # must ALSO state the cold-cache possibility (assert the exact wording
        # loosely, so a future edit cannot silently drop it).
        item = {
            "key": "LEARNING_PTR7770",
            "content": '{"a": 1, "b": 2, "c": 3}',
        }
        out = _format_context_block([item])
        # The pointer directive is present ...
        assert "retrieve(key='" in out
        assert "LEARNING_PTR7770" in out
        # ... and the cold-cache note is stated alongside it.
        low = out.lower()
        assert ("cold" in low) or ("re-fetch" in low) or ("refetch" in low)
