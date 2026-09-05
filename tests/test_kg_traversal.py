#!/usr/bin/env python3
"""
test_kg_traversal.py — IMPL #167 (KG read-side).

Covers the four acceptance areas for the multi-hop Knowledge-Graph recall
feature:

1. ``memchorus.knowledge_graph.build_subgraph`` — pure multi-hop subgraph
   builder.  No I/O, no MCP, no network.  Deterministic.
2. ``_McpClient.kg_subgraph`` and ``MemPalaceMemorySource.recall_kg`` — the
   MCP-backed entry points, exercised against a *fake`` subgraph (no server).
3. ``memchorus.recall_cli`` — the ``memchorus-recall kg`` CLI, tested
   in-process (no subprocess round-trip needed).
4. Hit-rate tracker integration — the orchestrator registers its KG results
   with the ``HitRateTracker`` exactly like vector recall hits do.

Run:  .venv/bin/python -m pytest tests/test_kg_traversal.py -v
"""

from __future__ import annotations

import json
import os
import sys
import shutil
import tempfile
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.knowledge_graph import build_subgraph, render_subgraph
from memchorus.hit_rate_tracker import HitRateTracker
from memchorus.mempalace_memory_source import _McpClient, MemPalaceMemorySource
from memchorus.orchestrator import MemoryOrchestrator
from memchorus.recall_cli import _render_text, _run_recall_kg


# ---------------------------------------------------------------------------
# Synthetic KG — deterministic, self-contained
# ---------------------------------------------------------------------------
#  A --works_on--> B
#  B --depends_on--> C
#  C --part_of--> A           (cycle; exercises the visited-set)
#  A --loves--> "D (free text)"   (description, not traversable)

FACTS: Dict[str, List[Dict[str, Any]]] = {
    "A": [
        {"direction": "outgoing", "subject": "A", "predicate": "works_on",
         "object": "B", "confidence": 0.9, "valid_from": "2026-01-01"},
        {"direction": "outgoing", "subject": "A", "predicate": "loves",
         "object": "D (free text)", "confidence": 0.5},
    ],
    "B": [
        {"direction": "outgoing", "subject": "B", "predicate": "depends_on",
         "object": "C", "confidence": 0.8},
    ],
    "C": [
        {"direction": "outgoing", "subject": "C", "predicate": "part_of",
         "object": "A", "confidence": 0.7},
    ],
    "D (free text)": [],
}


def _fake_fetch(entity: str) -> List[Dict[str, Any]]:
    return FACTS.get(entity, [])


# ---------------------------------------------------------------------------
# AC-1 — build_subgraph: multi-hop + bounds + relations filter
# ---------------------------------------------------------------------------

class TestBuildSubgraph:
    def test_hops_zero_is_seed_only(self):
        sg = build_subgraph("A", _fake_fetch, hops=0, limit=10)
        assert sg["hops"] == 0
        # Only A's own facts (the seed layer) — B, C not discovered.
        preds = {r["predicate"] for r in sg["relations"]}
        assert "works_on" in preds          # A --works_on--> B
        assert "depends_on" not in preds    # B --depends_on--> C (needs hops>=1)

    def test_hops_one_reaches_b_not_c(self):
        sg = build_subgraph("A", _fake_fetch, hops=1, limit=10)
        preds = {r["predicate"] for r in sg["relations"]}
        assert "works_on" in preds
        assert "depends_on" in preds        # B's facts reachable at hop 1
        assert "part_of" not in preds       # C --part_of--> A is hop 2

    def test_hops_two_reaches_c_and_cycles_back(self):
        sg = build_subgraph("A", _fake_fetch, hops=2, limit=10)
        predicates = {r["predicate"] for r in sg["relations"]}
        # All reachable predicates present.  'loves' is A's own seed-level
        # fact — always included regardless of hops.  'part_of' is C's fact
        # (hop 2).  No infinite loop despite A<->C cycle.
        assert predicates == {"works_on", "loves", "depends_on", "part_of"}
        # A is in the entity set (seed + C's object).
        assert "A" in sg["entities"]

    def test_hops_is_clamped_to_two(self):
        # 50 should behave identically to 2 after clamping (no crash, no hang).
        sg = build_subgraph("A", _fake_fetch, hops=50, limit=10)
        assert sg["hops"] == 2
        assert {r["predicate"] for r in sg["relations"]} == {
            "works_on", "loves", "depends_on", "part_of"}

    def test_relations_filters_by_predicate(self):
        sg = build_subgraph("A", _fake_fetch, hops=2, limit=10,
                            relations=["works_on"])
        for r in sg["relations"]:
            assert r["predicate"] == "works_on"

    def test_limit_caps_total_relations(self):
        # Give the builder a bigger graph, cap hard at 2.
        big: Dict[str, List[Dict[str, Any]]] = {
            "A": [
                {"direction": "outgoing", "subject": "A",
                 "predicate": f"rel{i}", "object": f"n{i}", "confidence": 0.5}
                for i in range(6)
            ],
        }
        sg = build_subgraph("A", lambda e: big.get(e, []), hops=1, limit=2)
        assert sg["count"] == 2
        assert len(sg["relations"]) == 2
        assert sg["complete"] is False       # truncated marker

    def test_unknown_entity_returns_empty_subgraph(self):
        sg = build_subgraph("Zz", _fake_fetch, hops=2, limit=10)
        assert sg["count"] == 0
        assert sg["relations"] == []
        # Seed still appears (we did look it up).
        assert sg["entities"] == ["Zz"]
        assert sg["complete"] is True

    def test_render_subgraph_produces_text(self):
        sg = build_subgraph("A", _fake_fetch, hops=1, limit=10)
        text = render_subgraph(sg)
        assert text
        assert "KG Subgraph: A" in text
        assert "works_on" in text
        # The free-text description "D (free text)" is an entity but long —
        # it should still appear in the entity list (render does not filter).
        assert "D (free text)" in text

    def test_render_empty(self):
        text = render_subgraph({"entity": "E", "hops": 0, "entities": ["E"],
                                "relations": [], "source_memories": [],
                                "count": 0, "complete": True})
        assert "E" in text
        assert "no relations found" in text


# ---------------------------------------------------------------------------
# AC-2 — _McpClient.kg_subgraph + MemPalaceMemorySource.recall_kg
# ---------------------------------------------------------------------------

@pytest.fixture()
def mp_source():
    """A disconnected in-process MemPalaceMemorySource (no MCP round-trip)."""
    return MemPalaceMemorySource(name="fake-mempalace", config={})


class TestMcpClientKgSubgraph:
    def test_disconnected_client_returns_none_on_seed(self, mp_source, monkeypatch):
        # Seed unreachable — kg_query returns None -> kg_subgraph -> None.
        monkeypatch.setattr(_McpClient, "kg_query", lambda self, e: None)
        assert mp_source._client.kg_subgraph("A", hops=1) is None

    def test_cached_client_wires_up_to_kg_subgraph(self, mp_source, monkeypatch):
        # Fake the kg_query to return FACTS-shaped data; verify the client
        # assembles a well-formed subgraph.
        monkeypatch.setattr(
            _McpClient, "kg_query",
            lambda self, e: FACTS.get(e, []),
        )
        sg = mp_source._client.kg_subgraph("A", hops=2, limit=10)
        assert sg is not None
        assert sg["entity"] == "A"
        assert "works_on" in {r["predicate"] for r in sg["relations"]}


class TestMemPalaceRecallKg:
    @staticmethod
    def _fake_subgraph():
        return {
            "entity": "A", "hops": 1,
            "entities": ["A", "B"],
            "relations": [
                {"from": "A", "to": "B", "predicate": "works_on",
                 "confidence": 0.9, "direction": "outgoing"},
            ],
            "source_memories": [], "count": 1, "complete": True,
        }

    def test_recall_kg_returns_channel_kg_dicts(self, mp_source, monkeypatch):
        # skip_mcp means is_alive=False; force alive=True so the check passes.
        mp_source._connected = True
        mp_source._client._connected = True
        fake = self.__class__._fake_subgraph()   # capture once, closure-safe
        monkeypatch.setattr(
            _McpClient, "kg_subgraph",
            lambda self, entity, hops, limit, relations, _f=fake: _f,
        )
        out = mp_source.recall_kg("A", hops=1, limit=10)
        assert isinstance(out, list)
        assert out
        entry = out[0]
        assert entry["channel"] == "kg"
        assert entry["score"] == pytest.approx(0.9)
        assert entry["source"] == "fake-mempalace"
        assert "works_on" in entry["key"]

    def test_recall_kg_none_on_unreachable(self, mp_source, monkeypatch):
        mp_source._connected = True
        mp_source._client._connected = True
        monkeypatch.setattr(
            _McpClient, "kg_subgraph",
            lambda self, entity, hops, limit, relations: None,
        )
        # Source-level None is allowed: the orchestrator treats it as "skip".
        assert mp_source.recall_kg("A", hops=1) is None

    def test_recall_kg_deduplicates_duplicates(self, mp_source, monkeypatch):
        mp_source._connected = True
        mp_source._client._connected = True
        dupes = [
            {"from": "A", "to": "B", "predicate": "works_on", "confidence": 0.9,
             "direction": "outgoing"},
            {"from": "A", "to": "B", "predicate": "works_on", "confidence": 0.9,
             "direction": "outgoing"},   # exact dupe -> dropped
            {"from": "A", "to": "C", "predicate": "owns", "confidence": 0.7,
             "direction": "outgoing"},
        ]
        monkeypatch.setattr(
            _McpClient, "kg_subgraph",
            lambda self, entity, hops, limit, relations: {
                "entity": entity, "hops": hops, "entities": [entity, "B", "C"],
                "relations": dupes, "source_memories": [],
                "count": len(dupes), "complete": True,
            },
        )
        out = mp_source.recall_kg("A", hops=1, limit=10)
        assert out is not None
        keys = {d["key"] for d in out}
        # Only two distinct triples (deduped).
        assert len(keys) == 2


# ---------------------------------------------------------------------------
# AC-3 — orchestrator.recall_kg as a DISTINCT channel from search()
# ---------------------------------------------------------------------------

class TestOrchestratorRecallKgDistinctChannel:
    """The acceptance criterion is that KG is a *separate* channel.

    We assert two things:
      (a) ``orch.recall_kg`` returns dicts stamped with ``channel=="kg"``.
      (b) ``orch.search`` is *not* polluted by KG results even when a KG
          source returns data.
    """

    def _build_orch(self, mp_source):
        orch = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {"memory_dir": tempfile.mkdtemp(prefix="mc_t_")},
            "enforce_on_read": False,
            "enforce_on_write": False,
        })
        for name in ("mempalace", "session_history"):
            orch.disable_source(name)
        orch.register_source(mp_source, priority=10)
        # skip_mcp makes _client.is_alive False; the orchestrator's
        # _check_source_available gate needs it True for the recall_kg path
        # to be exercised.  Per-test patches happen on top.
        mp_source._connected = True
        mp_source._client._connected = True
        return orch

    def test_recall_kg_returns_kg_channel(self, mp_source, monkeypatch):
        orch = self._build_orch(mp_source)
        monkeypatch.setattr(
            _McpClient, "kg_subgraph",
            lambda self, entity, hops, limit, relations: {
                "entity": entity, "hops": hops, "entities": [entity, "B"],
                "relations": [
                    {"from": entity, "to": "B", "predicate": "works_on",
                     "confidence": 0.9, "direction": "outgoing"}],
                "source_memories": [], "count": 1, "complete": True,
            },
        )
        out = orch.recall_kg("A", hops=1, limit=10)
        assert isinstance(out, list)
        assert out, "expected at least one KG relation"
        assert all(r["channel"] == "kg" for r in out)

    def test_search_is_not_affected_by_kg(self, mp_source, monkeypatch):
        orch = self._build_orch(mp_source)
        # Seed one text fact into hermes_default so search has a genuine hit.
        src = orch.memory_sources.get("hermes_default")
        assert src is not None
        src.save("note_x", "banana yellow fruit")

        # KG source returns relations for the SAME query, but via its channel.
        monkeypatch.setattr(
            _McpClient, "kg_subgraph",
            lambda self, entity, hops, limit, relations: {
                "entity": entity, "hops": hops, "entities": [entity, "K"],
                "relations": [
                    {"from": entity, "to": "K", "predicate": "related_to",
                     "confidence": 0.9, "direction": "outgoing"}],
                "source_memories": [], "count": 1, "complete": True,
            },
        )
        # search() should return only the vector/keyword hit, NOT the KG
        # relation — the KG channel is separate.
        res = orch.search("banana")
        # Every row from search() should have channel=="vector", "keyword",
        # or unset — never "kg".
        for row in res:
            assert row.get("channel") != "kg", \
                "KG result leaked into the search() channel"

    def test_recall_kg_skips_unavailable_sources(self, mp_source, monkeypatch):
        orch = self._build_orch(mp_source)
        # KG source unreachable -> recall_kg returns empty, no crash.
        monkeypatch.setattr(_McpClient, "kg_subgraph",
                            lambda self, e, h, l, r: None)
        out = orch.recall_kg("A", hops=1)
        assert out == []


# ---------------------------------------------------------------------------
# AC-4 — CLI (memchorus-recall kg)
# ---------------------------------------------------------------------------

class RecallingSource:
    """Stand-in for ``orchestrator`` / ``mempalace`` in CLI tests."""

    def __init__(self, result: Optional[List[Dict[str, Any]]] = None,
                 err: Exception = None):
        self._result = result
        self._err = err

    def recall_kg(self, entity, hops, limit, relations):
        if self._err:
            raise self._err
        return self._result


class TestRecallCli:
    def test_run_recall_kg_ok(self, capsys):
        src = RecallingSource(result=[
            {"key": "A --[works_on]--> B", "content": {"from": "A", "to": "B",
             "predicate": "works_on", "direction": "outgoing"},
             "source": "fake", "channel": "kg", "score": 0.9}])
        assert _run_recall_kg(src, "A", 1, 10, None, as_json=False) == 0
        out = capsys.readouterr().out
        assert "1 relations:" in out
        assert "works_on" in out

    def test_run_recall_kg_json(self, capsys):
        src = RecallingSource(result=[
            {"key": "k", "content": {"from": "A", "to": "B",
             "predicate": "p", "direction": "outgoing"},
             "source": "fake", "channel": "kg", "score": 0.5}])
        assert _run_recall_kg(src, "A", 1, 10, None, as_json=True) == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed, list) and parsed[0]["channel"] == "kg"

    def test_run_recall_kg_unreachable(self, capsys):
        src = RecallingSource(result=None)   # None from source => unreachable
        assert _run_recall_kg(src, "A", 1, 10, None, as_json=False) == 1
        err = capsys.readouterr().err
        assert "unreachable" in err

    def test_run_recall_kg_exception(self, capsys):
        src = RecallingSource(err=RuntimeError("boom"))
        assert _run_recall_kg(src, "A", 1, 10, None, as_json=False) == 1
        err = capsys.readouterr().err
        assert "boom" in err

    def test_run_recall_kg_empty_list(self, capsys):
        # An empty list is still a valid (reachable) response.
        src = RecallingSource(result=[])
        assert _run_recall_kg(src, "A", 1, 10, None, as_json=False) == 0
        out = capsys.readouterr().out
        assert "no relations found" in out

    def test_render_text(self):
        rows = [
            {"key": "A --[p1]--> B", "content": {"from": "A", "to": "B",
             "predicate": "p1", "direction": "outgoing"},
             "source": "s", "channel": "kg", "score": 0.7},
        ]
        text = _render_text(rows)
        assert "1 relations:" in text
        assert "p1" in text

    def test_render_text_no_results(self):
        assert _render_text([]) == "(no relations found)"


# ---------------------------------------------------------------------------
# AC-5 — Hit-rate tracker integration (KG recalls count like vector recalls)
# ---------------------------------------------------------------------------

class TestHitRateTrackerIntegration:
    def test_tracker_records_kg_keys(self):
        tmpdir = tempfile.mkdtemp(prefix="mc_kg_hr_")
        try:
            trk = HitRateTracker.get_instance(tmpdir)
            # Simulate: KG recall returned two keys.
            trk.record_recallhit("A --[works_on]--> B")
            trk.record_recallhit("B --[depends_on]--> C")
            stats_a = trk.get_hit_stats("A --[works_on]--> B")
            stats_b = trk.get_hit_stats("B --[depends_on]--> C")
            assert stats_a["total_recalls"] == 1
            assert stats_b["total_recalls"] == 1
        finally:
            HitRateTracker.reset(tmpdir)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_orchestrator_recall_kg_records_into_tracker(self, monkeypatch):
        """The orchestrator must call record_recallhit for every KG relation it
        surfaces — exactly the same path as vector recall hits."""
        tmpdir = tempfile.mkdtemp(prefix="mc_kg_hr_orch_")
        calls: List[str] = []
        real_tracker = HitRateTracker.get_instance(tmpdir)

        # Patch the free function the orchestrator uses to resolve the tracker.
        import memchorus.orchestrator as orch_mod
        monkeypatch.setattr(orch_mod, "_get_hit_rate_tracker",
                            lambda: real_tracker)
        # Spy on the tracker instance to record which keys were logged.
        orig = real_tracker.record_recallhit
        def _wrapper(k):
            calls.append(k)
            return orig(k)
        real_tracker.record_recallhit = _wrapper  # type: ignore[method-assign]

        try:
            mp = MemPalaceMemorySource(name="fake", config={})
            mp._connected = True
            mp._client._connected = True
            fake = {
                "entity": "A", "hops": 1, "entities": ["A", "X", "Y"],
                "relations": [
                    {"from": "A", "to": "X", "predicate": "p1",
                     "confidence": 0.9, "direction": "outgoing"},
                    {"from": "A", "to": "Y", "predicate": "p2",
                     "confidence": 0.8, "direction": "outgoing"},
                ],
                "source_memories": [], "count": 2, "complete": True,
            }
            monkeypatch.setattr(
                _McpClient, "kg_subgraph",
                lambda self, entity, hops, limit, relations, _f=fake: _f,
            )
            orch = MemoryOrchestrator({
                "default_source": "hermes_default",
                "hermes_default_config": {"memory_dir": tmpdir},
                "enforce_on_read": False, "enforce_on_write": False,
            })
            orch.disable_source("mempalace")
            orch.disable_source("session_history")
            orch.register_source(mp, priority=10)

            out = orch.recall_kg("A", hops=1, limit=10)
            assert out and all(r["channel"] == "kg" for r in out)
            # The tracker must have logged each relation's key.
            returned_keys = {r["key"] for r in out}
            assert len(calls) >= 2
            assert returned_keys <= set(calls), (
                f"expected the tracker to record {returned_keys!r}; "
                f"only got {calls!r}")
            # And _recent_recall_keys should hold them for later useful/stale marks.
            assert orch._recent_recall_keys
            assert orch._recent_recall_keys[0] in returned_keys
        finally:
            HitRateTracker.reset()
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC-6 — Backwards compatibility: existing kg_query(entity) continues to work
# ---------------------------------------------------------------------------

class TestBackwardsCompatKgQuery:
    """The original single-entity kg_query path must still behave the same."""

    def kg_client(self, monkeypatch):
        client = _McpClient(timeout=1)
        return client

    def _patch_call_tool(self, client, monkeypatch, payload):
        """Patch ``call_tool`` directly so we can return the raw MCP response,
        which is what ``kg_query`` expects to de-envelope."""
        calls: List[Any] = []

        def _fake(call, args=None):
            calls.append((call, args))
            # The raw MCP response is {"result": {json-string: ...}}
            inner = {"entity": args.get("entity"),
                     "active_facts": [
                         {"direction": "outgoing", "subject": args["entity"],
                          "predicate": "p", "object": "X", "confidence": 0.9}]}
            return {"result": json.dumps(inner)}

        monkeypatch.setattr(client, "call_tool", _fake)

    def test_kg_query_still_returns_list_of_facts(self, monkeypatch):
        client = self.kg_client(monkeypatch)
        self._patch_call_tool(client, monkeypatch, None)
        out = client.kg_query("A")
        assert isinstance(out, list)
        assert out, "expected at least one fact"
        # Each row should be a dict of the raw fact shape.
        assert out[0]["predicate"] == "p"
        assert out[0]["object"] == "X"

    def test_kg_query_disconnected_returns_none(self):
        client = _McpClient(timeout=1)
        # Not connected (no MCP server in test env) — call_tool returns None,
        # so kg_query returns the "unreachable" sentinel None (mirroring the
        # kg_subgraph unreachable contract).
        assert client.kg_query("A") is None

    def test_kg_query_connected_empty_data_returns_list(self, monkeypatch):
        client = _McpClient(timeout=1)

        def _fake(call, args=None):
            # Connected, but the entity has no facts (empty active_facts).
            return {"result": json.dumps(
                {"entity": args["entity"], "active_facts": []})}

        monkeypatch.setattr(client, "call_tool", _fake)
        assert client.kg_query("A") == []
