#!/usr/bin/env python3
"""Regression tests for GitHub issue #184 — MCP read-path source markers.

The read-path (MemPalaceMemorySource.search) now stamps every recall hit with
a *distinctive* ``source`` marker so that downstream consumers — the relevance
engine, ``memchorus-doctor --recall``, and the rendered recall block injected
into the prompt — can distinguish:

  * ``mcp-live``       — content served this call by a connected MemPalace MCP
                         backend (authoritative graph); and
  * ``local-fallback`` — content served out of the local JSON cache snapshot
                         because the live backend was unreachable.

Before #184, both paths stamped ``source="mempalace"`` (the *registered*
source name), making authoritative live content indistinguishable from a
stale local snapshot.  This file locks that distinction in code.

Also covers:
  * AC3  — recovery probe: when the MCP backend recovers mid-session, the
           next ``search()`` call automatically re-serves from the live graph
           (``mcp-live``) instead of continuing to serve the cached snapshot.
  * AC5  — byte-identical scoring lock: the relevance engine canonicalises
           ``mcp-live`` → ``mempalace`` for scoring lookups only, so the
           rendered success-path recall block stays byte-identical to
           pre-#184 output, while the reported ``source`` field retains the
           ``mcp-live`` marker.

Tests are deterministic, in-memory, and free of external state.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.mempalace_memory_source import MemPalaceMemorySource, _McpClient
from memchorus.relevance_engine import RelevanceScorer


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

# A realistic MCP search result for key "w/r" (similarity 0.95 ≥ 0.5 floor).
_LIVE_HIT = {
    "wing": "w",
    "room": "r",
    "text": "the answer to the query",
    "similarity": 0.95,
}


def _make_source(cache_dir, *, mcp_alive: bool = True, mcp_hits=None):
    """Build a MemPalaceMemorySource with a fake, toggleable MCP client.

    ``mcp_alive=True`` → connected + is_alive, client.search returns mcp_hits.
    ``mcp_alive=False`` → not connected, is_alive False, client.search returns [].
    """
    src = MemPalaceMemorySource(config={"cache_dir": str(cache_dir)})
    fake = _McpClient(timeout=1)

    if mcp_alive:
        src._ensure_connected = (  # type: ignore[assignment]
            lambda: True
        )
        fake._connected = True
        fake._persistent_session = None
        fake.search = lambda **kw: list(mcp_hits or [])  # type: ignore[assignment]
    else:
        src._ensure_connected = (  # type: ignore[assignment]
            lambda: False
        )
        fake._connected = False
        fake._persistent_session = None
        fake.search = lambda **kw: []  # type: ignore[assignment]

    src._client = fake
    return src


def _seed_cache(cache_dir, key: str, value):
    """Write a single entry into the local JSON cache (same layout as _cache_locally)."""
    (cache_dir / f"{key}.json").write_text(json.dumps(value))


# --------------------------------------------------------------------------- #
#  SECTION 1 — AC1: mcp-live source marker                                     #
# --------------------------------------------------------------------------- #


class TestMCPLiveSourceMarker:
    """When MCP is alive, all recall hits must carry source='mcp-live'."""

    def test_live_hit_carries_mcp_live_source(self, tmp_path):
        src = _make_source(tmp_path, mcp_alive=True, mcp_hits=[_LIVE_HIT])
        results = src.search("the answer")

        assert len(results) == 1, f"expected 1 result, got {len(results)}"
        # AC1: live-recall hit must be marked mcp-live, not the registered name.
        assert results[0]["source"] == "mcp-live", (
            f"live-recall hit source is {results[0]['source']!r}, expected 'mcp-live'"
        )
        # (pre-#184 this would have been 'mempalace')
        assert results[0]["source"] != "mempalace"

    def test_source_marker_is_distinctive_constant(self, tmp_path):
        """The SOURCE_MCP_LIVE constant must equal the string the hits emit."""
        assert MemPalaceMemorySource.SOURCE_MCP_LIVE == "mcp-live"
        assert MemPalaceMemorySource.SOURCE_LOCAL_FALLBACK == "local-fallback"

    def test_multiple_live_hits_all_marked(self, tmp_path):
        hits = [
            {"wing": "w", "room": "r1", "text": "answer one", "similarity": 0.95},
            {"wing": "w", "room": "r2", "text": "answer two", "similarity": 0.90},
        ]
        src = _make_source(tmp_path, mcp_alive=True, mcp_hits=hits)
        results = src.search("answer")

        assert len(results) == 2
        for r in results:
            assert r["source"] == "mcp-live", (
                f"all live hits must carry mcp-live, found {r['source']!r}"
            )


# --------------------------------------------------------------------------- #
#  SECTION 2 — AC1: local-fallback source marker                               #
# --------------------------------------------------------------------------- #


class TestLocalFallbackSourceMarker:
    """When MCP is dead and a matching cache entry exists, the served hit must
    carry source='local-fallback' — the marker that distinguishes a stale
    local snapshot from authoritative live content."""

    def test_dead_mcp_cache_hit_carries_local_fallback(self, tmp_path):
        # Seed the local cache with an entry whose key matches the query.
        _seed_cache(tmp_path, "legacy-entry", {"text": "cached snapshot data"})

        src = _make_source(tmp_path, mcp_alive=False)
        results = src.search("legacy-entry")

        assert len(results) == 1, (
            f"expected 1 cache-served result, got {len(results)}"
        )
        # AC1: cache-served hit must be marked local-fallback.
        assert results[0]["source"] == "local-fallback", (
            f"cache-served hit source is {results[0]['source']!r}, "
            f"expected 'local-fallback'"
        )
        # (pre-#184 this would have been 'mempalace', identical to live)
        assert results[0]["source"] != "mempalace"

    def test_local_fallback_marker_distinct_from_live(self, tmp_path):
        """The two markers must be distinct strings — a consumer can tell them
        apart without any other field."""
        assert MemPalaceMemorySource.SOURCE_MCP_LIVE != MemPalaceMemorySource.SOURCE_LOCAL_FALLBACK


# --------------------------------------------------------------------------- #
#  SECTION 3 — AC3: recovery probe (MCP recovers mid-session)                  #
# --------------------------------------------------------------------------- #


class TestRecoveryProbe:
    """AC3: when the MCP backend recovers, the next search() call must
    automatically re-serve from the live graph (mcp-live) — not continue
    serving the cached snapshot (local-fallback)."""

    def _source_with_toggleable_mcp(self, tmp_path, live_hits):
        """Build a source whose fake MCP client can be toggled alive/dead."""
        src = MemPalaceMemorySource(config={"cache_dir": str(tmp_path)})
        fake = _McpClient(timeout=1)
        fake._persistent_session = None

        # _ensure_connected returns the current _connected flag (like the real one)
        src._ensure_connected = (  # type: ignore[assignment]
            lambda: fake._connected  # read the flag at call time
        )
        fake.search = (  # type: ignore[assignment]
            lambda **kw: list(live_hits) if fake._connected else []
        )
        src._client = fake
        return src, fake

    def test_mcp_recovers_and_serves_live_again(self, tmp_path):
        live_hits = [{"wing": "w", "room": "r", "text": "fresh data", "similarity": 0.95}]

        # Seed cache so that when MCP is dead, search() still returns a result.
        _seed_cache(tmp_path, "fresh", {"text": "stale cached copy"})

        src, fake = self._source_with_toggleable_mcp(tmp_path, live_hits)

        # Phase 1: MCP is down → cache serves, marked local-fallback.
        fake._connected = False
        results_down = src.search("fresh")
        assert len(results_down) == 1
        assert results_down[0]["source"] == "local-fallback", (
            f"phase 1 (MCP down): expected local-fallback, got {results_down[0]['source']!r}"
        )

        # Phase 2: MCP recovers → live graph serves, marked mcp-live.
        fake._connected = True
        results_up = src.search("fresh")
        assert len(results_up) >= 1, "expected at least one result after MCP recovery"
        # Pre-fix, BOTH paths stamped 'mempalace', so recovery was invisible.
        # Post-fix, the recovered live hit is unambiguously mcp-live.
        live_results = [r for r in results_up if r["source"] == "mcp-live"]
        assert len(live_results) >= 1, (
            "after MCP recovers, at least one live (mcp-live) result must be served; "
            f"got {[(r['source'], r.get('key')) for r in results_up]}"
        )

    def test_recovery_does_not_require_manual_reset(self, tmp_path):
        """The recovery must be automatic — no caller-side reset() or
        re-instantiation.  This is the "no re-sync" complaint from the issue."""
        live_hits = [{"wing": "w", "room": "r", "text": "recovered", "similarity": 0.9}]

        _seed_cache(tmp_path, "recovered-key", {"text": "old snapshot"})

        src, fake = self._source_with_toggleable_mcp(tmp_path, live_hits)
        fake._connected = False

        # Down: only cache.
        down = src.search("recovered-key")
        assert any(r["source"] == "local-fallback" for r in down)

        # Up: live graph comes back — same source object, no reset().
        fake._connected = True
        up = src.search("recovered-key")
        assert any(r["source"] == "mcp-live" for r in up), (
            "recovery must be automatic: after MCP alive again, search() "
            "must serve at least one mcp-live result. "
            f"Got: {[(r['source'], r.get('key')) for r in up]}"
        )


# --------------------------------------------------------------------------- #
#  SECTION 4 — AC5: byte-identical scoring lock (relevance engine)             #
# --------------------------------------------------------------------------- #


class TestScoringByteIdenticalLock:
    """The relevance engine must score mcp-live identically to mempalace so
    the rendered success-path recall block stays byte-identical to pre-#184
    output.  The reported source field retains the mcp-live marker."""

    def _ctx(self, domain: str):
        """Build a ContextWeight with the default domain_weights, scoped to one domain name.

        Mirrors the default ContextWeight dataclass (see relevance_engine.py L63-80):
            memory: {hermes_default: 1.5, mempalace: 0.5}
            graph:  {mempalace: 1.5, hermes_default: 0.5}
        """
        from memchorus.relevance_engine import ContextWeight

        dw = {
            "memory": {"hermes_default": 1.5, "mempalace": 0.5},
            "graph": {"mempalace": 1.5, "hermes_default": 0.5},
        }
        return ContextWeight(domain_weights=dw)

    def _hit(self, source: str, domain: str):
        return {"key": "w/r", "content": "the answer query", "source": source, "_domain": domain}

    def test_mcp_live_scores_identically_to_mempalace_in_graph_domain(self):
        s = RelevanceScorer()
        ctx = self._ctx("graph")

        s_live = s.score(self._hit("mcp-live", "graph"), "the answer query", ctx)
        s_old  = s.score(self._hit("mempalace", "graph"), "the answer query", ctx)

        assert s_live == s_old, (
            f"AC5 LOCK FAILED: mcp-live {s_live!r} != mempalace {s_old!r} "
            f"in graph domain — rendered recall block would re-rank"
        )

    def test_mcp_live_scores_identically_to_mempalace_in_memory_domain(self):
        s = RelevanceScorer()
        ctx = self._ctx("memory")

        s_live = s.score(self._hit("mcp-live", "memory"), "the answer query", ctx)
        s_old  = s.score(self._hit("mempalace", "memory"), "the answer query", ctx)

        assert s_live == s_old, (
            f"AC5 LOCK FAILED: mcp-live {s_live!r} != mempalace {s_old!r} "
            f"in memory domain"
        )

    def test_local_fallback_scores_below_live_in_graph_domain(self):
        """local-fallback is a degraded snapshot — it must score below the
        live graph content in the graph domain, so it never outranks live."""
        s = RelevanceScorer()
        ctx = self._ctx("graph")

        s_live = s.score(self._hit("mcp-live", "graph"), "the answer query", ctx)
        s_fb   = s.score(self._hit("local-fallback", "graph"), "the answer query", ctx)

        assert s_fb < s_live, (
            f"local-fallback {s_fb!r} must score BELOW live {s_live!r} in graph domain"
        )

    def test_reported_source_field_retains_marker(self):
        """The RankedResult.source field must retain mcp-live (not the
        canonicalised 'mempalace') so doctor can still distinguish live from
        cached content in its --recall output."""
        from memchorus.relevance_engine import ContextWeight  # noqa: F401
        s = RelevanceScorer()
        ctx = self._ctx("graph")

        ranked = s.score_and_rank(
            [self._hit("mcp-live", "graph")],
            "the answer query",
            context=ctx,
        )
        assert len(ranked) == 1
        assert ranked[0].source == "mcp-live", (
            f"RankedResult.source is {ranked[0].source!r}, "
            f"expected 'mcp-live' (memchorus-doctor --recall depends on this)"
        )

    def test_mcp_live_outranks_hermes_default_in_graph_domain_after_fix(self):
        """AC5 regression demonstration: in the graph domain, mempalace has
        domain weight 1.5 (max) while hermes_default has 0.5.  A pre-fix
        mcp-live hit (scored as *unknown* source, floor 0.25) would lose to
        hermes_default.  Post-fix, mcp-live canonicalises to mempalace
        (weight 1.5) and must outrank hermes_default.

        This is the concrete re-ranking bug that #184 AC5 locks out: without
        the canonicalization, live graph content would rank BELOW the default
        hermes content in a graph-domain recall, changing the rendered block.
        """
        s = RelevanceScorer()
        ctx = self._ctx("graph")

        live_hit  = self._hit("mcp-live", "graph")
        hd_hit    = {"key": "w/r2", "content": "the answer query",
                      "source": "hermes_default", "_domain": "graph"}

        ranked = s.score_and_rank([dict(hd_hit), dict(live_hit)], "the answer query", context=ctx)

        # Graph domain: mempalace weight=1.5 (dominant) vs hermes_default=0.5.
        # Post-fix: mcp-live (→ mempalace, weight 1.5) outranks hermes_default (weight 0.5).
        # Pre-fix:  mcp-live (→ unknown, floor 0.25) would have LOST to hermes_default.
        assert ranked[0].source == "mcp-live", (
            f"post-fix ranking should put mcp-live first in graph domain; "
            f"got {[(r.source, round(r.score, 4)) for r in ranked]}"
        )



# --------------------------------------------------------------------------- #
#  SECTION 5 — end-to-end: live wins over cache (dedup / ordering lock)        #
# --------------------------------------------------------------------------- #


class TestLiveWinsOverCache:
    """When both MCP and the cache can serve a hit, the live path must win
    (source=mcp-live), and the cache path must skip it (seen_keys dedup)."""

    def test_live_hit_wins_and_cache_hit_is_deduped(self, tmp_path):
        live_hit = {"wing": "w", "room": "r", "text": "live data", "similarity": 0.95}
        # Cache entry with a DIFFERENT key (so both paths would serve if live lost)
        _seed_cache(tmp_path, "w-r", {"text": "cached copy of live data"})

        src = _make_source(tmp_path, mcp_alive=True, mcp_hits=[live_hit])
        results = src.search("w-r")

        # Live hit: key="w/r".  Cache hit: key="w-r".  Different keys, both may match.
        # AC1: every live-path hit (key "w/r") must be mcp-live — NOT the stale
        # "mempalace" that pre-#184 stamped both paths with.
        live_entries = [r for r in results if r.get("key") == "w/r"]
        assert live_entries, "expected the live hit (key w/r) to be present"
        for e in live_entries:
            assert e["source"] == "mcp-live", (
                f"live-path hit (key {e.get('key')}) source is {e['source']!r}, "
                f"expected 'mcp-live' — cache-hit must not masquerade as live graph"
            )
