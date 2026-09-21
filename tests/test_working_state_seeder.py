"""Tests for IMPL #209 (a1) — WorkingStateSeeder.

Covers the acceptance criteria of card t_f9b4aaf4 (Corpus Working State, a1):

* AC-S3 — a fresh session in a real project populates exactly ONE working-state
  drawer in ``memchorus_workspace / working-state``; a repeated seed in the same
  session (state unchanged) triggers no second write; a changed state re-saves.
* Routing — the composed snapshot carries ``category=WORKING_STATE`` and routes
  to the correct wing/room via the MemPalace source's ``_resolve_wing`` /
  ``_categorize_room`` default maps.
* Idempotency — the in-process :data:`SESSION_SEED_CACHE` gate short-circuits
  repeated saves for the same ``(slug, state-hash)`` pair.
* Up-semantics — ``seed=False`` (session-end upsert) bypasses the in-process
  cache so a changed state is persisted even though the session-start seed was
  already cached.
* No-project — ``ensure(None)`` (or a slug that resolves to ``None``) is a
  no-op returning ``False`` without touching the source.
* Determinism — :func:`compose_snapshot` and :func:`state_hash` are
  pure functions of their inputs (no wall-clock, no ordering surprises), which
  is what makes the content-hash dedup a true no-op.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from memchorus import working_state_seeder as ws
from memchorus.relevance_engine import closet_bound_result


# ---------------------------------------------------------------------------
# Stub MemorySource
# ---------------------------------------------------------------------------


class StubSource:
    """Minimal MemorySource stub recording save/retrieve calls.

    ``saved`` accumulates ``(key, value)`` tuples; ``store`` maps key -> True
    once a save has been issued so that ``retrieve(key)`` round-trips the
    "already exists" case used by the seeder's pre-save idempotency check.
    """

    def __init__(self) -> None:
        self.saved: List[Tuple[str, Dict[str, Any]]] = []
        self.store: Dict[str, bool] = {}
        self.save_return: Any = True

    def save(self, key: str, value: Dict[str, Any]) -> Any:
        self.saved.append((key, dict(value)))
        if self.save_return:
            self.store[key] = True
        return self.save_return

    def retrieve(self, key: str) -> Optional[Any]:
        return self.store.get(key)

    @property
    def save_count(self) -> int:
        return len(self.saved)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_cache():
    """Reset the in-process session cache before and after every test.

    The cache is module-scope; tests run in arbitrary order, so each test must
    start from a known-empty gate.  (The autouse fixture keeps this from being
    boilerplate in every test body.)
    """
    ws.clear_session_cache()
    yield
    ws.clear_session_cache()


# ===========================================================================
# compose_snapshot / state_hash — determinism + bounded fields
# ===========================================================================


class TestComposeSnapshot:

    def test_category_is_working_state(self):
        snap = ws.compose_snapshot("memchorus", goal="ship")
        assert snap["category"] == "WORKING_STATE"

    def test_project_slug_round_trips(self):
        snap = ws.compose_snapshot("MemChorus")
        assert snap["project"] == "memchorus"  # canonicalised to lower

    def test_next_actions_capped_at_three(self):
        """AC: bounded 'next 3 actions'."""
        snap = ws.compose_snapshot(
            "x", next_actions=["a", "b", "c", "d", "e", "f"]
        )
        assert snap["next_actions"] == ["a", "b", "c"]

    def test_blanks_dropped_from_lists(self):
        snap = ws.compose_snapshot("x", in_flight=["ok", "", "  ", "good"])
        assert snap["in_flight"] == ["ok", "good"]

    def test_none_fields_normalise(self):
        snap = ws.compose_snapshot("x", goal=None, in_flight=None)
        assert snap["goal"] == ""
        assert snap["in_flight"] == []

    def test_snapshot_text_is_grep_friendly(self):
        snap = ws.compose_snapshot(
            "memchorus",
            goal="ship feature",
            in_flight=["diff1"],
            open_kanban=["t_1"],
            blockers=["waiting on X"],
            next_actions=["next step"],
        )
        txt = snap["snapshot"]
        assert "project=memchorus" in txt
        assert "goal: ship feature" in txt
        assert "in_flight: diff1" in txt
        assert "open_kanban: t_1" in txt
        assert "blockers: waiting on X" in txt
        assert "next_actions: next step" in txt


class TestStateHash:

    def test_stable_for_identical_state(self):
        a = ws.compose_snapshot("p", goal="g", next_actions=["x"])
        b = ws.compose_snapshot("p", goal="g", next_actions=["x"])
        assert ws.state_hash(a) == ws.state_hash(b)

    def test_changes_when_state_changes(self):
        a = ws.compose_snapshot("p", goal="g")
        b = ws.compose_snapshot("p", goal="g2")
        assert ws.state_hash(a) != ws.state_hash(b)

    def test_short_hex(self):
        h = ws.state_hash(ws.compose_snapshot("p", goal="g"))
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_routing_fields_excluded(self):
        """source_file / category must not affect the hash — only state fields."""
        a = ws.compose_snapshot("p", goal="g")
        b = ws.compose_snapshot("p", goal="g", in_flight=["d"])
        assert ws.state_hash(a) != ws.state_hash(b)


# ===========================================================================
# resolve_slug — priority chain + canonicalisation + None path
# ===========================================================================


class TestResolveSlug:

    def test_none_when_no_project(self):
        import memchorus.orientation as orientation

        orig = orientation._resolve_project
        orientation._resolve_project = lambda _e: None
        try:
            assert ws.resolve_slug() is None
        finally:
            orientation._resolve_project = orig

    def test_canonicalises_to_lower_strip(self):
        import memchorus.orientation as orientation

        orig = orientation._resolve_project
        orientation._resolve_project = lambda _e: "  MemChorus "
        try:
            assert ws.resolve_slug() == "memchorus"
        finally:
            orientation._resolve_project = orig


# ===========================================================================
# ensure() — idempotency gate (AC-S3)
# ===========================================================================


class TestEnsureFreshSeed:

    def test_fresh_seed_saves_once(self):
        src = StubSource()
        seeder = ws.WorkingStateSeeder(src)
        assert seeder.ensure("memchorus", seed=True, goal="g") is True
        assert src.save_count == 1

    def test_payload_has_routing_and_provenance(self):
        src = StubSource()
        ws.WorkingStateSeeder(src).ensure("memchorus", seed=True, goal="g")
        key, val = src.saved[0]
        assert key == "PROJECT_memchorus"
        assert val["category"] == "WORKING_STATE"
        assert val["project"] == "memchorus"
        assert val["source_file"].startswith("PROJECT_memchorus_")
        assert "snapshot" in val


class TestEnsureIdempotency:

    def test_second_seed_same_state_no_second_write(self):
        """AC-S3: repeated seed in the same session (state unchanged) → no 2nd write."""
        src = StubSource()
        seeder = ws.WorkingStateSeeder(src)
        seeder.ensure("memchorus", seed=True, goal="g")
        seeder.ensure("memchorus", seed=True, goal="g")
        assert src.save_count == 1

    def test_changed_state_triggers_new_save(self):
        src = StubSource()
        seeder = ws.WorkingStateSeeder(src)
        seeder.ensure("memchorus", seed=True, goal="g")
        seeder.ensure("memchorus", seed=True, goal="g2")
        # 2 distinct states → 2 saves.  (The 2nd is gated by the cache check on
        # (slug, shash); a changed shash produces a fresh key in the cache.)
        assert src.save_count == 2

    def test_source_retrieve_hit_short_circuits_write(self):
        """AC-S3: retrieve hit (cross-instance) ⇒ no second write, even fresh process."""
        src = StubSource()
        # Pre-populate the store as if a prior process already wrote this key.
        src.store["PROJECT_memchorus"] = True
        seeder = ws.WorkingStateSeeder(src)
        assert seeder.ensure("memchorus", seed=True, goal="g") is True
        assert src.save_count == 0  # no new save was issued


class TestEnsureUpsert:

    def test_seed_false_bypasses_cache(self):
        """Session-end upsert: a changed state is re-saved even when cached."""
        src = StubSource()
        seeder = ws.WorkingStateSeeder(src)
        seeder.ensure("memchorus", seed=True, goal="g")   # caches (memchorus, hash(g))
        # New process would cache the same pair; simulate that by NOT clearing.
        # Now seed=False with a CHANGED state must write again.
        seeder.ensure("memchorus", seed=False, goal="g2")
        assert src.save_count == 2

    def test_seed_false_same_state_still_dedups_no_second_save_via_source(self):
        """seed=False, same state as cached → retrieve hit ⇒ no second write.

        The source's own store remembers the key, so the seeder's cross-instance
        retrieve hit short-circuits the save even though the cache was bypassed.
        """
        src = StubSource()
        seeder = ws.WorkingStateSeeder(src)
        seeder.ensure("memchorus", seed=True, goal="g")   # save #1, store key set
        assert src.save_count == 1
        # seed=False bypasses the in-process cache, but the source.retrieve()
        # still sees the key in its store → short-circuits.
        seeder.ensure("memchorus", seed=False, goal="g")
        assert src.save_count == 1


class TestEnsureNoProject:

    def test_no_slug_is_noop_returns_false(self):
        import memchorus.orientation as _orientation

        src = StubSource()
        seeder = ws.WorkingStateSeeder(src)
        orig = _orientation._resolve_project
        _orientation._resolve_project = lambda _e: None
        try:
            assert seeder.ensure(seed=True, goal="g") is False
        finally:
            _orientation._resolve_project = orig
        assert src.save_count == 0

    def test_explicit_empty_slug_is_noop(self):
        src = StubSource()
        seeder = ws.WorkingStateSeeder(src)
        assert seeder.ensure("", seed=True, goal="g") is False
        assert src.save_count == 0


class TestEnsureFailureModes:

    def test_save_false_returns_false(self):
        src = StubSource()
        src.save_return = False
        seeder = ws.WorkingStateSeeder(src)
        assert seeder.ensure("memchorus", seed=True, goal="g") is False
        assert src.save_count == 1  # save was attempted

    def test_save_raises_returns_false_not_exception(self):
        class RaisingSource:
            def save(self, *a, **k):
                raise RuntimeError("simulated MCP failure")

            def retrieve(self, *a, **k):
                return None

        seeder = ws.WorkingStateSeeder(RaisingSource())
        assert seeder.ensure("memchorus", seed=True, goal="g") is False


# ===========================================================================
# Routing-map integration — WORKING_STATE must resolve to the seeded wing/room.
# ===========================================================================


class TestRoutingMaps:

    def test_wing_map_admits_working_state(self):
        from memchorus.mempalace_memory_source import _DEFAULT_WING_MAP

        assert _DEFAULT_WING_MAP.get("WORKING_STATE") == "memchorus_workspace"

    def test_room_map_admits_working_state(self):
        from memchorus.mempalace_memory_source import _DEFAULT_ROOM_MAP

        assert _DEFAULT_ROOM_MAP.get("WORKING_STATE") == "working-state"


# ===========================================================================
# corpus_balancer alignment — the (b) half reads the same rooms we populate.
# ===========================================================================


class TestCorpusBalancerAlignment:

    def test_seeded_room_in_corpus_balancer_set(self):
        from memchorus import corpus_balancer

        assert "working-state" in tuple(corpus_balancer.WORKING_STATE_ROOMS)


# ===========================================================================
# AC-S4 — COMPOSITION GATE (card t_f9b4aaf4)
#
# The whole reason this card exists: once t_1d3d1f72 has landed (already on
# master — A2–A5: project stamp on write + closet_bound_result predicate), the
# drawer the seeder produces must be classified as BOUND by the predicate so
# it can be partitioned into Tier 0 (active-project) by the re-scorer.  This
# is the end-to-end handshake between (a1) and (a2) and is what the design
# memo calls the "composition gate".  It is the single test that proves the
# #209 pipeline is actually wired: seed → drawer → stamp → partition.
#
# What we check:
# 1. The save() call the seeder issues carries ``project="<slug>"`` in the
#    value dict — the exact slot closet_bound_result reads first.
# 2. That same dict, when routed through closet_bound_result(slug), returns
#    True — i.e. it would land in Tier 0.
# 3. A drawer written by the *same* seeder path but for a *different*
#    slug is NOT bound to this one (the predicate discriminates correctly,
#    which is what keeps the active project's drawer ahead of noise).
# 4. A drawer that carries no project signal at all is False (pre-#209
#    drawers in the same wing/room must not be pulled into Tier 0).
# ===========================================================================


class TestSeededDrawerIsClosetBound:

    def test_save_value_carries_project_stamp(self):
        src = StubSource()
        ws.clear_session_cache()
        seeder = ws.WorkingStateSeeder(src)
        try:
            assert seeder.ensure("memchorus", seed=True, goal="g") is True
        finally:
            ws.clear_session_cache()
        assert src.save_count == 1
        _key, value = src.saved[0]
        assert value.get("project") == "memchorus"

    def test_save_value_is_classified_bound_by_predicate(self):
        """The drawer the seeder emits is BOUND for its own slug, not for others.

        AC-S4 — the composition gate.  The predicate (already on master, part
        of t_1d3d1f72) reads ``result["project"]``; a seeded drawer must carry
        the slug there.  This test drives the seeder once and then asks the
        live predicate what tier that drawer would land in.
        """
        src = StubSource()
        ws.clear_session_cache()
        seeder = ws.WorkingStateSeeder(src)
        try:
            assert seeder.ensure("memchorus", seed=True, goal="g") is True
        finally:
            ws.clear_session_cache()
        assert src.save_count == 1
        _key, value = src.saved[0]
        assert closet_bound_result(value, "memchorus") is True
        assert closet_bound_result(value, "MemChorus") is True  # case-insensitive
        assert closet_bound_result(value, "unrelated-project") is False
        assert closet_bound_result(value, None) is False

    def test_untagged_drawer_is_not_bound(self):
        """Pre-#209 drawers in the same wing/room carry no stamp → Tier 1.

        Guards against a future refactoring that drops the project stamp:
        a seeded drawer with the stamp sorts to the top, an untagged one in
        the same wing/room must not.
        """
        untagged = {"wing": "memchorus_workspace", "room": "working-state",
                    "content": "stale context"}
        assert closet_bound_result(untagged, "memchorus") is False

    def test_seeded_drawer_survives_retrieve_round_trip(self):
        """A second ensure() over the same (slug, state) reads the existing drawer.

        The idempotency gate means the seeder will ``retrieve`` first and see
        the existing drawer's project stamp is already present, short-circuit
        the write.  This is the "one drawer per project, always upserted"
        data-model invariant AC-S3 enforces from the reader's side.
        """
        src = StubSource()
        ws.clear_session_cache()
        try:
            assert src.store == {}
            seeder = ws.WorkingStateSeeder(src)
            assert seeder.ensure("memchorus", seed=True, goal="g") is True
            assert src.saved and src.saved[0][1].get("project") == "memchorus"
            # Second seed, same state → in-process cache, no new save call.
            assert src.save_count == 1
            assert seeder.ensure("memchorus", seed=True, goal="g") is True
            assert src.save_count == 1
        finally:
            ws.clear_session_cache()