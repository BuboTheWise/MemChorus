"""
Pillar 3 — Tunnels / Diary / Events reserved augmentation slots (issue #225).

North-star spec §6 (Memchorus-Recall-Loop-NorthStar-Spec.md).

Contract:

- Three sources, DETERMINISTIC trigger table (spec §6.1):
    Tunnels:      T.domain set AND a tunnel to a different domain exists AND the
                  task text references the bridge domain.
    Diary:        T.kind in {synthesis, project_start, post_action}.
    Events+Artifacts: T.kind in {review, handoff, integration} OR text references
                      a correlation_id / patch.ready / artifact id.
- Budget (spec §6.2): at most 1 reserved slot for implied (K=3 → 2 main + 1
  reserved); at most 2 for explicit. Controlled by RESERVED_SLOTS
  (``RECALL_CONFIG.reserved_slots_implied`` / ``reserved_slots_explicit``).
- Additive (spec §6.4): reserved items never displace ranked items by
  out-scoring; they reduce the slots available to main ranked items.
- Provenance (spec §6.5): every reserved item carries ``source`` in
  {tunnel, diary, event, artifact} plus a non-empty provenance string.
- Layout (spec §6.3): rendered block uses section labels
  [RECALL/MAIN], [BRIDGE/<domain>], [SESSION LOG], [COORDINATION].
- No cross-task carryover: a tunnel only fires when THIS task's domain+terms
  reference the bridge domain.
- Deterministic: same task + same fetched data → same output (order included).
- Graceful degradation: any fetch failure or missing fetcher → empty list from
  that source, never an escape; the rest of the pipeline still runs.

RED→GREEN: these tests import ``memchorus.augmentation`` which does not exist
on current master, so the whole file fails collection until the module lands.
"""

from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures (deterministic data blobs, no network dependencies)
# ---------------------------------------------------------------------------

_TASK: Dict[str, Any] = {
    "kind": "review",  # T.kind — drives the events trigger
    "domain": "memchorus",  # T.domain — used by the tunnel trigger
    "text": (
        "Review the handoff for the auth refactor. "
        "correlation_id=abc123. patch.ready for the mempalace module."
    ),
    "explicit": False,
}

_TUNNEL_ROW: Dict[str, Any] = {
    "wing_a": "memchorus",
    "room_a": "recall-config",
    "wing_b": "mempalace",
    "room_b": "diary-entries",
}

_DIARY_ROW: Dict[str, Any] = {
    "entry": "SESSION:2026-09-28|built.pillar1+pillar2|★★★",
    "timestamp": 1790000000,
}

_EVENT_ROW: Dict[str, Any] = {
    "event_id": "evt-0042",
    "type": "patch.ready",
    "correlation_id": "abc123",
    "writer": "worker",
    "summary": "pillar 2 temporal gate merged and green",
}

_ARTIFACT_ROW: Dict[str, Any] = {
    "id": 97,
    "filename": "findings-round3.md",
    "content_type": "text/markdown",
    "size": 2048,
    "by": "default",
}


class _FakeFetcher:
    """Mimics the ``AugmentationSource`` protocol.

    ``fetch(source) -> List[dict]`` returns rows for that source. Each call is
    tracked so tests can assert which sources were actually fetched (and which
    the deterministic trigger table skipped).  ``fail`` maps a source to an
    Exception to raise on fetch for graceful-degradation branches.
    """

    def __init__(
        self,
        tunnels: Optional[List[Dict[str, Any]]] = None,
        diary: Optional[List[Dict[str, Any]]] = None,
        events: Optional[List[Dict[str, Any]]] = None,
        artifacts: Optional[List[Dict[str, Any]]] = None,
        fail: Optional[Dict[str, Exception]] = None,
    ) -> None:
        self.calls: List[str] = []
        self._fail = dict(fail or {})
        self._data = {
            "tunnel": list(tunnels or []),
            "diary": list(diary or []),
            "event": list(events or []),
            "artifact": list(artifacts or []),
        }

    def __call__(self, source: str) -> List[Dict[str, Any]]:
        """AugmentationSource protocol: fetch(source)."""
        return self.fetch(source)

    def fetch(self, source: str) -> List[Dict[str, Any]]:
        self.calls.append(source)
        exc = self._fail.get(source)
        if exc is not None:
            raise exc
        rows = [dict(r) for r in self._data.get(source, [])]
        for i, row in enumerate(rows):
            row.setdefault("source", source)
            row.setdefault("provenance", f"{source}#{i + 1}")
        return rows


def _fk(**kw: Any) -> _FakeFetcher:
    return _FakeFetcher(**kw)


# ---------------------------------------------------------------------------
# 1. Trigger table (spec §6.1)
# ---------------------------------------------------------------------------


class TestTriggerTable:
    def test_review_kind_fires_events(self) -> None:
        from memchorus.augmentation import triggers_for

        fired = triggers_for(_TASK)
        assert "event" in fired, "T.kind=review must fire the events/artifacts source"
        assert "diary" not in fired, "review ∉ {synthesis, project_start, post_action}"

    def test_synthesis_kind_fires_diary(self) -> None:
        from memchorus.augmentation import triggers_for

        task = dict(_TASK)
        task["kind"] = "synthesis"
        task["text"] = "synthesise the recall findings"
        assert "diary" in triggers_for(task)

    def test_project_start_kind_fires_diary(self) -> None:
        from memchorus.augmentation import triggers_for

        task = dict(_TASK)
        task["kind"] = "project_start"
        task["text"] = "start the memchorus project"
        assert "diary" in triggers_for(task)

    def test_post_action_kind_fires_diary(self) -> None:
        from memchorus.augmentation import triggers_for

        task = dict(_TASK)
        task["kind"] = "post_action"
        task["text"] = "wrap up the recall changes"
        assert "diary" in triggers_for(task)

    def test_handoff_integration_kinds_fire_events(self) -> None:
        from memchorus.augmentation import triggers_for

        for k in ("handoff", "integration"):
            task = dict(_TASK)
            task["kind"] = k
            task["text"] = "plain text, no id references"
            assert "event" in triggers_for(task), f"T.kind={k} must fire events"

    def test_correlation_id_in_text_fires_events(self) -> None:
        from memchorus.augmentation import triggers_for

        task = dict(_TASK)
        task["kind"] = "planning"
        task["text"] = "pick up where correlation_id=zzz was left"
        assert "event" in triggers_for(task)

    def test_patch_ready_in_text_fires_events(self) -> None:
        from memchorus.augmentation import triggers_for

        task = dict(_TASK)
        task["kind"] = "planning"
        task["text"] = "the patch.ready event already recorded this"
        assert "event" in triggers_for(task)

    def test_artifact_id_in_text_fires_events(self) -> None:
        from memchorus.augmentation import triggers_for

        task = dict(_TASK)
        task["kind"] = "planning"
        task["text"] = "attach findings-round3.md (artifact #97)"
        assert "event" in triggers_for(task)

    def test_tunnel_requires_domain_plus_bridge_reference(self) -> None:
        from memchorus.augmentation import triggers_for

        # domain set, but task text does not reference the bridge domain
        t1 = dict(_TASK)
        t1["text"] = "do the auth refactor"
        assert "tunnel" not in triggers_for(t1)

        # No domain on the task at all → no tunnel (no cross-task carryover)
        t2 = dict(_TASK)
        t2.pop("domain")
        t2["text"] = "touched the mempalace wing"
        assert "tunnel" not in triggers_for(t2)

        # domain set AND text references the bridge domain → tunnel fires
        t3 = dict(_TASK)
        t3["domain"] = "memchorus"
        t3["text"] = "the mempalace bridge needs attention"
        assert "tunnel" in triggers_for(t3)

    def test_no_trigger_means_no_source(self) -> None:
        from memchorus.augmentation import triggers_for

        assert (
            triggers_for(
                {
                    "kind": "planning",
                    "domain": None,
                    "text": "nothing to see",
                    "explicit": False,
                }
            )
            == set()
        ), "no signal → no reserved slots"


# ---------------------------------------------------------------------------
# 2. Budget (spec §6.2)
# ---------------------------------------------------------------------------


class TestBudget:
    def test_implied_budget_is_one(self) -> None:
        from memchorus.augmentation import reserved_budget

        assert reserved_budget(explicit=False) == 1

    def test_explicit_budget_is_two(self) -> None:
        from memchorus.augmentation import reserved_budget

        assert reserved_budget(explicit=True) == 2

    def test_no_triggered_sources_no_reserved(self) -> None:
        from memchorus.augmentation import augment

        task = {
            "kind": "planning",
            "domain": None,
            "text": "nothing",
            "explicit": False,
        }
        assert augment(task=task, fetcher=_fk(), explicit=False) == []


# ---------------------------------------------------------------------------
# 3. Additive semantics (spec §6.4)
# ---------------------------------------------------------------------------


class TestAdditiveSemantics:
    def test_reserved_items_labelled_and_flagged(self) -> None:
        from memchorus.augmentation import augment

        items = augment(task=_TASK, fetcher=_fk(events=[_EVENT_ROW]), explicit=False)
        assert len(items) == 1, "implied budget = 1 → exactly one reserved item"
        it = items[0]
        assert it["source"] in ("event", "artifact")
        assert it["provenance"]
        assert it.get("reserved") is True

    def test_additive_combine_preserves_main_prefix(self) -> None:
        from memchorus.augmentation import combine, augment

        reserved = augment(task=_TASK, fetcher=_fk(events=[_EVENT_ROW]), explicit=False)
        main = ["item-A", "item-B"]  # stand-in for select()'s chosen list
        combined = combine(main, reserved)
        assert combined[:2] == main
        assert len(combined) == 3
        assert combined[2].get("reserved") is True


# ---------------------------------------------------------------------------
# 4. Layout + provenance (spec §6.3)
# ---------------------------------------------------------------------------


class TestLayout:
    def test_bridge_section_present_for_tunnel(self) -> None:
        from memchorus.augmentation import render_augmentation_block

        task = dict(_TASK)
        task["kind"] = "synthesis"
        task["text"] = "the mempalace bridge needs a synthesis pass"
        task["domain"] = "memchorus"
        block = render_augmentation_block(
            task=task,
            fetcher=_fk(tunnels=[_TUNNEL_ROW], diary=[_DIARY_ROW]),
            explicit=True,
        )
        assert "[BRIDGE" in block, "tunnel items render in a [BRIDGE/<domain>] section"

    def test_session_log_section_present_for_diary(self) -> None:
        from memchorus.augmentation import render_augmentation_block

        task = dict(_TASK)
        task["kind"] = "synthesis"
        block = render_augmentation_block(
            task=task, fetcher=_fk(diary=[_DIARY_ROW]), explicit=True
        )
        assert "[SESSION LOG" in block, "diary items render in a [SESSION LOG] section"

    def test_coordination_section_present_for_event(self) -> None:
        from memchorus.augmentation import render_augmentation_block

        block = render_augmentation_block(
            task=_TASK,
            fetcher=_fk(events=[_EVENT_ROW], artifacts=[_ARTIFACT_ROW]),
            explicit=True,
        )
        assert "[COORDINATION" in block, (
            "event/artifact items render in a [COORDINATION] section"
        )

    def test_render_includes_provenance(self) -> None:
        from memchorus.augmentation import render_augmentation_block

        block = render_augmentation_block(
            task=_TASK, fetcher=_fk(events=[_EVENT_ROW]), explicit=True
        )
        # provenance is either a labelled field or an inline source#id marker
        assert ("provenance" in block.lower()) or "#" in block


# ---------------------------------------------------------------------------
# 5. Graceful degradation
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_fetch_failure_swallowed(self) -> None:
        from memchorus.augmentation import augment

        fetcher = _fk(events=[_EVENT_ROW], fail={"event": RuntimeError("mcp down")})
        out = augment(task=_TASK, fetcher=fetcher, explicit=False)
        assert out == [], "fetcher failure → no reserved items, no escape"
        assert "event" in fetcher.calls, "source was attempted before failing"

    def test_none_fetcher_degrades(self) -> None:
        from memchorus.augmentation import augment

        assert augment(task=_TASK, fetcher=None, explicit=False) == []

    def test_empty_fetcher_result_is_fine(self) -> None:
        from memchorus.augmentation import augment

        fetcher = _fk()
        assert augment(task=_TASK, fetcher=fetcher, explicit=False) == []
        assert "event" in fetcher.calls  # triggered
        assert "tunnel" not in fetcher.calls  # not triggered (no bridge ref in text)


# ---------------------------------------------------------------------------
# 6. Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_call_identical_output(self) -> None:
        from memchorus.augmentation import augment

        task = dict(_TASK)
        task["text"] = "mempalace bridge + patch.ready"
        f1 = _fk(tunnels=[_TUNNEL_ROW], events=[_EVENT_ROW])
        f2 = _fk(tunnels=[_TUNNEL_ROW], events=[_EVENT_ROW])
        a = augment(task=task, fetcher=f1, explicit=True)
        b = augment(task=task, fetcher=f2, explicit=True)
        assert a == b, (
            "augment() is pure: identical task+data → identical reserved items"
        )


# ---------------------------------------------------------------------------
# 7. AutoRecallEngine integration
# ---------------------------------------------------------------------------


class TestEngineIntegration:
    def test_engine_exposes_reserved_slots_headroom(self) -> None:
        from memchorus.auto_recall_engine import AutoRecallEngine

        engine = AutoRecallEngine(orchestrator=None, trigger=None)
        assert engine.reserved_slots_headroom(explicit=False) == 1
        assert engine.reserved_slots_headroom(explicit=True) == 2

    def test_engine_augment_degrades_without_fetcher(self) -> None:
        from memchorus.auto_recall_engine import AutoRecallEngine

        engine = AutoRecallEngine(orchestrator=None, trigger=None)
        out = engine.augment(
            {
                "kind": "synthesis",
                "domain": "memchorus",
                "text": "synthesis",
                "explicit": False,
            }
        )
        assert out == [], "no fetcher wired → empty reserved list, no escape"


# ---------------------------------------------------------------------------
# 8. Pipeline integration (reserved_slots parameter on select)
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    def test_select_reserved_slots_headroom(self) -> None:
        """select() on PR #232 accepts ``reserved_slots`` and caps ``chosen``.

        RED on current master (no select yet); GREEN when PR #232 merges.
        Skipped (not failed) if select is not importable in master yet.
        """
        import memchorus.auto_recall_engine as are
        from memchorus.auto_recall_engine import TaskContext

        if not hasattr(are, "select"):
            pytest.skip(
                "select() not on master yet (PR #232 open) — "
                "integration verified in VERIFY/REVIEW task"
            )

        # Build the task from the canonical TaskContext dataclass so the
        # stub always satisfies the full pillar-1 select() contract
        # (base_weights / active_project / as_of / domain / recency_tau_days),
        # regardless of when the two pillars were merged.
        pool = [
            ({"key": f"k{i}", "content": f"content {i} " + "x" * 40, "timestamp": 0})
            for i in range(6)
        ]
        task = TaskContext(query="recall")
        full = are.select(task, pool, K=4, B_tokens=100000, reserved_slots=None)
        reserved = are.select(task, pool, K=4, B_tokens=100000, reserved_slots=1)
        assert len(reserved.chosen) <= 3, (
            f"reserved_slots=1 must cap chosen at K-1=3, got {len(reserved.chosen)}"
        )
        # ``chosen`` items are SelectedCandidate wrappers carrying the raw
        # candidate dict as ``.result`` — read the key from there.
        full_keys = [c.result.get("key") for c in full.chosen]
        reserved_keys = [c.result.get("key") for c in reserved.chosen]
        assert all(k in full_keys for k in reserved_keys)
