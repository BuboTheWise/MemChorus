"""#226 — write-time surface-routing classifier (spec `MemChorus-Surface-Routing-Spec`).

Two layers under test:

1.  The **pure classifier** (:mod:`memchorus.surface_routing`) — ``route()`` /
    ``routing_decision()`` with a fully-injected :class:`RouteContext`, exactly
    as spec §4 / §6. No MCP, no clock (``as_of`` is injected), no I/O.  This is
    where spec §6's AC-1…AC-7 live: each row is an explicit
    ``(content, context) → expected surface`` case with the rule that fires.
2.  The **write-path wiring** — ``MemoryOrchestrator.save()`` must attach the
    decision's three routing fields (``routing_kind`` / ``emission_kind`` /
    ``surface_rule``) onto the *persisted* payload so a reader can see *why*
    the fact is on its surface (spec §4.2 / §5.3 / §7.3).

RED→GREEN (development-process): the pure classifier cases were RED before
``surface_routing.py`` existed and GREEN after; the save-wiring cases were RED
before ``routing_kind`` was attached and GREEN after.  Every test here is
assertable without a live MCP backend, a real clock, or any network.

Emission-orthogonality (AC-8) is asserted in BOTH layers: ``route()`` decides
`routing_kind`, ``emission_kind()`` is an independent axis, and a save that
records both stores them as two separate fields.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from typing import Any, Dict, List

import pytest

from memchorus.surface_routing import (
    EMISSION_JSON,
    EMISSION_STR,
    SURFACE_DRAWER,
    SURFACE_KG,
    SURFACE_MEMORY,
    EMISSION_KINDS,
    SURFACES,
    FactKind,
    RouteContext,
    emission_kind,
    get_validity_fields,
    route,
    routing_decision,
)


# =========================================================================== #
# Spec §6 — AC rows against the PURE route() function
# --------------------------------------------------------------------------- #
# These are the canonical acceptance criteria. Each uses the spec's stated
# "context that matters" verbatim (FactKind + has_validity_window + window
# flag + as_of), so the test pins the *shape of fact* the classifier must
# interpret, independent of live _infer_profile heuristics.
# =========================================================================== #

_NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)

AC_ROWS = [
    # (id, content, context-kw, expected_surface, expected_rule, note)
    (
        "AC-1",
        "OPSEC zero-tol: no agent names in commit trailers or diffs",
        {
            "fact_kind": FactKind.STANDING_PREFERENCE,
            "is_standing_preference": True,
            "has_validity_window": False,
        },
        SURFACE_MEMORY,
        "R1",
        "standing preference — the only legitimate MEMORY case",
    ),
    (
        "AC-2",
        {
            "subject": "MemChorus",
            "predicate": "ci_pins",
            "object": "ruff==0.16.6",
            "valid_from": "2026-09-20",
            "valid_to": None,
        },
        {
            "fact_kind": FactKind.LONG_LIVED_KNOWLEDGE,
            "has_validity_window": True,
            "as_of": _NOW,
        },
        SURFACE_KG,
        "R2",
        "textbook KG: typed triple + temporal window",
    ),
    (
        "AC-3",
        {
            "subject": "acme-org/project",
            "predicate": "branch_naming",
            "object": "feat-<issue>-<slug>",
            "superseded_by": "kg-abc123",
        },
        {
            "fact_kind": FactKind.LONG_LIVED_KNOWLEDGE,
            "has_validity_window": True,
            "as_of": _NOW,
        },
        SURFACE_KG,
        "R2",
        "KG with a supersession pointer the recall reader needs on the KG",
    ),
    (
        "AC-4",
        "Benchmark run: 46 files changed, 20 tests pass, 1 skipped",
        {
            "fact_kind": FactKind.VERBATIM_ARTIFACT,
            "has_validity_window": False,
        },
        SURFACE_DRAWER,
        "R3",
        "verbatim artefact — the conservative default",
    ),
    (
        "AC-5",
        {"text": "CI LINT gotcha: ci.yml pins ruff==0.16.6"},
        {
            "fact_kind": FactKind.VERBATIM_ARTIFACT,
            "has_validity_window": False,
            "as_of": _NOW,
        },
        SURFACE_DRAWER,
        "R3",
        "note ABOUT a rule-of-state, not one itself — stays DRAWER",
    ),
    (
        "AC-6",
        {
            "subject": "MemChorus",
            "predicate": "ci_pins",
            "object": "ruff==0.15.x (legacy)",
            "valid_from": "2026-08-01",
            "valid_to": "2026-08-15",
        },
        {
            "fact_kind": FactKind.LONG_LIVED_KNOWLEDGE,
            "has_validity_window": True,
            "as_of": _NOW,
        },
        SURFACE_DRAWER,
        "R4",
        "expired at write time → historical record on DRAWER, not KG",
    ),
    (
        "AC-7",
        {
            "subject": "AgentA",
            "predicate": "installed_from",
            "object": "https://github.com/acme-org/project.git@f7735d2",
            "valid_from": None,
            "superseded_by": None,
        },
        {
            "fact_kind": FactKind.LONG_LIVED_KNOWLEDGE,
            "has_validity_window": False,
            "is_relationship_shape": True,
        },
        SURFACE_KG,
        "R2",
        "typed shape alone is sufficient for KG (no window needed)",
    ),
]


@pytest.mark.parametrize(
    "content,context_kw,expected,rule,note",
    [
        (c, kw, exp, rule, note)
        for (_id, c, kw, exp, rule, note) in AC_ROWS
    ],
    ids=[r[0] for r in AC_ROWS],
)
def test_route_acceptance_criteria(content, context_kw, expected, rule, note):
    """spec §6 — route(content, context) == expected surface (rule fires)."""
    content: Any
    rule: str
    ctx = RouteContext(**context_kw)
    result = routing_decision(content, ctx)
    assert result.surface == expected, (
        f"{note}: expected {expected} rule={rule}, "
        f"got {result.surface} rule={result.rule}"
    )
    assert result.rule == rule
    # Public `route()` wrapper returns the bare string.
    assert route(content, RouteContext(**context_kw)) == expected


def test_route_ac7_relationship_shape_is_independent_of_profile():
    """spec §6.3 — AC-7 must reach KG on shape ALONE, not on a profile hint.

    The most likely-to-be-missed AC: a typed relation routes to KG even when
    the caller did not attach a temporal window.  We assert this twice —
    once via the ctx flag, once via pure _is_structural_relationship on the
    content (which routing_decision also reads).
    """
    content = {
        "subject": "AgentA",
        "predicate": "installed_from",
        "object": "gh@sha",
        "valid_from": None,
        "superseded_by": None,
    }
    # Flag-driven.
    assert (
        route(content, RouteContext(is_relationship_shape=True, fact_kind=FactKind.LONG_LIVED_KNOWLEDGE))
        == SURFACE_KG
    )
    # Shape-driven (the context carries NO relationship flag, but the content
    # is a structural triple).
    assert route(content) == SURFACE_KG


# =========================================================================== #
# R0 — explicit caller surface (defer + veto)
# =========================================================================== #


def test_r0_explicit_memory_is_deferred():
    assert route("whatever", RouteContext(caller_explicit=SURFACE_MEMORY)) == SURFACE_MEMORY


def test_r0_explicit_kg_is_deferred_when_current():
    content = {"subject": "s", "predicate": "p", "object": "o"}
    assert (
        route(content, RouteContext(caller_explicit=SURFACE_KG, as_of=_NOW))
        == SURFACE_KG
    )


def test_r0_explicit_kg_vetoed_when_expired():
    """R0 trusts the caller except R4 can veto an expired KG write."""
    content = {
        "subject": "s",
        "predicate": "p",
        "object": "o",
        "valid_from": "2026-08-01",
        "valid_to": "2026-08-02",
    }
    result = routing_decision(
        content, RouteContext(caller_explicit=SURFACE_KG, as_of=_NOW)
    )
    assert result.surface == SURFACE_DRAWER
    assert result.rule == "R4"


def test_r0_not_applicable_when_caller_explicit_none():
    """No caller surface → R1/R2/R3 decide; a verbatim artefact falls to DRAWER."""
    assert route("benchmark: 10 files changed") == SURFACE_DRAWER
    # The decision must be R3 (fallback), not R0.
    assert routing_decision("benchmark").rule == "R3"


# =========================================================================== #
# R2 vs R3 boundary — prose mentioning entities is NOT a relationship
# =========================================================================== #


def test_r3_prose_mentioning_two_entities_stays_drawer():
    """spec §6.3 — the shape must be structural, not a lexical mention."""
    content = "Max loves chess (they play it every Friday)"
    assert (
        route(
            content,
            RouteContext(
                fact_kind=FactKind.VERBATIM_ARTIFACT, is_relationship_shape=False
            ),
        )
        == SURFACE_DRAWER
    )


def test_r2_temporal_branch_requires_long_lived_profile():
    """R2's temporal branch needs has_validity_window AND LONG_LIVED_KNOWLEDGE.

    A standing preference with a window is STILL a preference (R1 wins, §5.2) —
    so a STANDING_PREFERENCE fact carries a window but resolves MEMORY.
    """
    standing = RouteContext(
        fact_kind=FactKind.STANDING_PREFERENCE,
        is_standing_preference=True,
        has_validity_window=True,
    )
    assert (
        routing_decision("preference with a stray window", standing)
        .surface
        == SURFACE_MEMORY
    )
    # But a VERBATIM fact with a window and NO relationship shape does NOT reach
    # R2's temporal branch (requires LONG_LIVED_KNOWLEDGE) → DRAWER.
    verbatim_window = RouteContext(
        fact_kind=FactKind.VERBATIM_ARTIFACT,
        has_validity_window=True,
    )
    assert (
        routing_decision(
            {"valid_from": "2026-09-01", "note": "x"}, verbatim_window
        )
        .surface
        == SURFACE_DRAWER
    )


# =========================================================================== #
# R4 — write-time expiry veto (boundary + boundary-adjacent)
# =========================================================================== #


def test_r4_not_yet_valid_vetoes_kg():
    content = {
        "subject": "s",
        "predicate": "p",
        "object": "o",
        "valid_from": "2026-10-01",  # opens AFTER as_of
        "valid_to": "2027-10-01",
    }
    result = routing_decision(
        content, RouteContext(fact_kind=FactKind.LONG_LIVED_KNOWLEDGE, as_of=_NOW)
    )
    assert result.surface == SURFACE_DRAWER
    assert result.rule == "R4"


def test_r4_current_window_is_not_vetoed():
    content = {
        "subject": "s",
        "predicate": "p",
        "object": "o",
        "valid_from": "2026-09-01",
        "valid_to": "2026-12-01",
    }
    result = routing_decision(
        content, RouteContext(fact_kind=FactKind.LONG_LIVED_KNOWLEDGE, as_of=_NOW)
    )
    assert result.surface == SURFACE_KG
    assert result.rule == "R2"


def test_r4_unparseable_window_is_not_a_veto():
    """A malformed boundary must not veto R2 — it is treated as no boundary."""
    content = {
        "subject": "s",
        "predicate": "p",
        "object": "o",
        "valid_to": "not-a-date",
    }
    result = routing_decision(
        content, RouteContext(fact_kind=FactKind.LONG_LIVED_KNOWLEDGE, as_of=_NOW)
    )
    # Still has the relationship shape → R2 (unparseable valid_to is not a veto).
    assert result.surface == SURFACE_KG
    assert result.rule == "R2"


def test_r4_no_as_of_means_no_expiry_check():
    """as_of=None → live write treats the fact as current; R2 stands."""
    content = {
        "subject": "s",
        "predicate": "p",
        "object": "o",
        "valid_from": "2026-08-01",
        "valid_to": "2026-08-02",
    }
    result = routing_decision(
        content,
        RouteContext(
            fact_kind=FactKind.LONG_LIVED_KNOWLEDGE,
            has_validity_window=True,
            # as_of intentionally None.
        ),
    )
    assert result.surface == SURFACE_KG


# =========================================================================== #
# First-match-wins ordering (R4 > R1 > R2 > R3 > R0 precedence in §4.1)
# =========================================================================== #


def test_first_match_wins_r1_beats_r2():
    """A standing preference is MEMORY even if it also looks like a relation."""
    content = {
        "subject": "s",
        "predicate": "p",
        "object": "o",
        "valid_to": "2026-08-02",  # would otherwise be R4-vetoed
    }
    result = routing_decision(
        content,
        RouteContext(
            fact_kind=FactKind.STANDING_PREFERENCE,
            is_standing_preference=True,
            is_relationship_shape=True,
            has_validity_window=True,
            as_of=_NOW,
        ),
    )
    assert (result.surface, result.rule) == (SURFACE_MEMORY, "R1")


def test_first_match_wins_r2_beats_r3():
    """A typed relation routes to KG rather than falling to the drawer."""
    content = {"subject": "s", "predicate": "p", "object": "o"}
    assert route(content, RouteContext(fact_kind=FactKind.LONG_LIVED_KNOWLEDGE)) == SURFACE_KG


def test_first_match_wins_r0_beats_all():
    """An explicit caller surface wins over any shape/window inference."""
    content = {"subject": "s", "predicate": "p", "object": "o", "valid_to": "2026-08-02"}
    assert (
        route(content, RouteContext(caller_explicit=SURFACE_MEMORY, as_of=_NOW))
        == SURFACE_MEMORY
    )


# =========================================================================== #
# Output contract (spec §4.2) + emission orthogonality (spec §5.3 / AC-8)
# =========================================================================== #


def test_route_returns_bare_surface_string_never_none():
    for content in (
        "a preference",
        {"subject": "s", "predicate": "p", "object": "o"},
        {"text": "note"},
        "benchmark",
        42,
        None,
    ):
        surface = route(content)
        assert surface in SURFACES
        assert surface in (SURFACE_MEMORY, SURFACE_DRAWER, SURFACE_KG)


def test_ac8_orthogonality_routing_and_emission_are_independent():
    """spec §6.2 — a fact can be routing=KG AND emission=str (and any pair)."""
    from memchorus.surface_routing import EMISSION_JSON  # noqa: F401

    # A typed relation is a json-encoded body.
    rel = {"subject": "s", "predicate": "p", "object": "o"}
    assert emission_kind(rel) == EMISSION_JSON

    # A prose body is a str-encoded body.
    assert emission_kind("OPSEC no agent names") == EMISSION_STR
    # A text-wrapping dict is STILL str-encoded (the body is prose).
    assert emission_kind({"text": "CI LINT gotcha"}) == EMISSION_STR

    # A KG-routed fact can be str-encoded (a rule-of-state written as prose):
    # routing=KG (shape) AND emission=str (body is a string).
    prose_state = {
        "text": "The pinned ruff version is 0.16.6",
        "valid_from": "2026-09-20",
    }
    ctx = RouteContext(
        fact_kind=FactKind.LONG_LIVED_KNOWLEDGE, has_validity_window=True
    )
    assert route(prose_state, ctx) == SURFACE_KG
    assert emission_kind(prose_state) == EMISSION_STR


def test_emission_kind_all_values_are_valid():
    for sample in (
        "str body",
        123,
        1.5,
        True,
        None,
        ["a", "b"],
        {"subject": "s", "predicate": "p", "object": "o"},
        {"text": "prose"},
        {"content": "prose body inside a wrapper"},
        {"data": "non-wrapper key"},
    ):
        assert emission_kind(sample) in EMISSION_KINDS


# =========================================================================== #
# Pure-helper contracts (validity-window extraction / parsing / structural shape)
# =========================================================================== #


def test_get_validity_fields_extracts_all_three():
    content = {
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "superseded_by": "kg-xyz",
    }
    vf = get_validity_fields(content)
    assert vf == {
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "superseded_by": "kg-xyz",
    }


def test_get_validity_fields_missing_are_none():
    assert get_validity_fields({"subject": "s"}) == {
        "valid_from": None,
        "valid_to": None,
        "superseded_by": None,
    }
    assert get_validity_fields("just a string") == {
        "valid_from": None,
        "valid_to": None,
        "superseded_by": None,
    }


def test_parse_dt_handles_date_datetime_and_iso_string():
    from memchorus.surface_routing import _parse_dt

    assert _parse_dt(None) is None
    assert _parse_dt("not a date") is None
    assert _parse_dt("2026-09-20") is not None
    # ISO with Z-suffix parses fine.
    assert _parse_dt("2026-09-20T00:00:00Z") is not None
    # datetime objects pass through.
    assert _parse_dt(datetime(2026, 9, 20, tzinfo=timezone.utc)) is not None
    # A bare date is promoted to a datetime at midnight.
    assert _parse_dt(date(2026, 9, 20)) is not None


def test_is_structural_relationship_shape_vs_lexical():
    from memchorus.surface_routing import _is_structural_relationship

    assert _is_structural_relationship(
        {"subject": "s", "predicate": "p", "object": "o"}
    ) is True
    assert _is_structural_relationship(
        {"relation": "owns", "owner": "AgentA", "asset": "MemChorus"}
    ) is True
    assert _is_structural_relationship([["a", "b"], ["c", "d"]]) is True
    # A plain prose dict is NOT structural.
    assert _is_structural_relationship({"note": "Max loves chess"}) is False
    assert _is_structural_relationship("Max loves chess") is False


# =========================================================================== #
# Layer 2 — live save() wiring (spec §7.3): routing_kind on the PERSISTED payload
# --------------------------------------------------------------------------- #
# A real MemoryOrchestrator + HermesDefaultMemorySource, no mempalace, no MCP:
# the classifier runs inside save() and attaches routing_kind / emission_kind /
# surface_rule onto whatever payload is persisted, readable back via retrieve().
# =========================================================================== #
#
# The spec's AC surface is the *pure classifier*, asserted above.  This section
# verifies the wiring: the save path must record the decision it made so the
# reader can see *why* the fact is where it is.
# --------------------------------------------------------------------------- #


def _make_orchestrator(tmpdir: str):
    # Deferred import — avoid pulling hermes_memory_source into the module
    # scope (it has filesystem side effects at __init__).
    from memchorus.hermes_memory_source import HermesDefaultMemorySource
    from memchorus.orchestrator import MemoryOrchestrator, MemoryProfile

    orch = MemoryOrchestrator()
    # Keep the test hermetic: disable the mempalace-backed source entirely so
    # save() only ever routes to the in-memory source — no MCP, no live backend.
    orch.disable_source("mempalace")
    src = HermesDefaultMemorySource(data_dir=tmpdir)
    orch.register_source(src)
    return orch, src, MemoryProfile


def test_save_wiring_routing_kind_on_payload(tmp_path):
    orch, src, _ = _make_orchestrator(str(tmp_path))

    # AC-4 — a verbatim artefact (no profile hint) → DRAWER, R3, str.
    assert orch.save(
        "ac-4", "Benchmark run: 46 files changed, 20 tests pass, 1 skipped"
    )
    payload = src.retrieve("ac-4")
    assert payload["routing_kind"] == SURFACE_DRAWER
    assert payload["emission_kind"] == EMISSION_STR
    assert payload["surface_rule"] == "R3"
    # The body must remain recoverable (wrapped under a non-routing key).
    body_key = next((k for k in ("_content", "text", "body", "content", "value") if k in payload), None)
    assert body_key is not None
    assert "46 files changed" in str(payload[body_key])


def test_save_wiring_standing_preference_to_memory(tmp_path):
    orch, src, MemoryProfile = _make_orchestrator(str(tmp_path))

    # AC-1 — explicit USER_PREFERENCE hint → MEMORY, R1, str.
    assert orch.save(
        "ac-1",
        "OPSEC zero-tol: no agent names in commit trailers or diffs",
        profile=MemoryProfile.USER_PREFERENCE,
    )
    payload = src.retrieve("ac-1")
    assert payload["routing_kind"] == SURFACE_MEMORY
    assert payload["emission_kind"] == EMISSION_STR
    assert payload["surface_rule"] == "R1"
    body_key = next((k for k in ("_content", "text", "body", "content", "value") if k in payload), None)
    assert body_key is not None
    assert "OPSEC" in str(payload[body_key])


def test_save_wiring_typed_triple_to_kg(tmp_path):
    orch, src, _ = _make_orchestrator(str(tmp_path))

    # AC-2 — typed triple with a window → KG, R2, json.  This is the
    # "textbook KG case the current write path gets wrong."
    triple = {
        "subject": "MemChorus",
        "predicate": "ci_pins",
        "object": "ruff==0.16.6",
        "valid_from": "2026-09-20",
        "valid_to": None,
    }
    assert orch.save("ac-2", triple)
    payload = src.retrieve("ac-2")
    # Dict body preserves structure — subject/predicate/object must survive.
    assert payload.get("subject") == "MemChorus"
    assert payload.get("predicate") == "ci_pins"
    assert payload.get("object") == "ruff==0.16.6"
    assert payload["routing_kind"] == SURFACE_KG
    assert payload["emission_kind"] == EMISSION_JSON
    assert payload["surface_rule"] == "R2"


def test_save_wiring_text_with_window_is_kg(tmp_path):
    orch, src, _ = _make_orchestrator(str(tmp_path))

    # AC-5/§6.3 boundary — prose with a temporal window that scopes it in time
    # is a *rule-of-state* (note in time) → KG (R2 temporal branch).
    prose_with_window = {
        "text": "CI LINT gotcha: ci.yml pins ruff==0.16.6",
        "valid_from": "2026-09-20",
    }
    assert orch.save("ac5-window", prose_with_window)
    payload = src.retrieve("ac5-window")
    assert payload["routing_kind"] == SURFACE_KG
    assert payload["emission_kind"] == EMISSION_STR
    assert payload["surface_rule"] in ("R2", "R4")
    # If it's R4, the fact must be DRAWER (vetoed).  If R2, KG.
    if payload["surface_rule"] == "R4":
        assert payload["routing_kind"] == SURFACE_DRAWER
    else:
        assert payload["routing_kind"] == SURFACE_KG
        assert payload["surface_rule"] == "R2"


def test_save_wiring_text_without_window_stays_drawer(tmp_path):
    orch, src, _ = _make_orchestrator(str(tmp_path))

    # AC-5 — the SAME prose but WITHOUT a temporal window → DRAWER (R3),
    # a verbatim artefact, correctly-shaped as a body the agent can search.
    prose_no_window = {"text": "CI LINT gotcha: ci.yml pins ruff==0.16.6"}
    assert orch.save("ac5-plain", prose_no_window)
    payload = src.retrieve("ac5-plain")
    assert payload["routing_kind"] == SURFACE_DRAWER
    assert payload["emission_kind"] == EMISSION_STR
    assert payload["surface_rule"] == "R3"


def test_save_wiring_expired_triple_vetoes_kg(tmp_path):
    orch, src, _ = _make_orchestrator(str(tmp_path))

    # AC-6 — a typed triple whose validity window already closed at write time
    # must be vetoes R2 (R4) → DRAWER, with the body still recoverable.
    expired_triple = {
        "subject": "MemChorus",
        "predicate": "ci_pins",
        "object": "ruff==0.15.x (legacy)",
        "valid_from": "2026-08-01",
        "valid_to": "2026-08-15",
    }
    assert orch.save("ac-6", expired_triple)
    payload = src.retrieve("ac-6")
    assert payload["routing_kind"] == SURFACE_DRAWER
    assert payload["emission_kind"] == EMISSION_JSON
    assert payload["surface_rule"] == "R4"
    # Structure must survive the vetoes path.
    assert payload.get("subject") == "MemChorus"
    # The reason field (spec §7.3 structured log) is present.
    assert payload.get("surface_reason") is not None


def test_save_wiring_supersession_pointer_to_kg(tmp_path):
    orch, src, _ = _make_orchestrator(str(tmp_path))

    # AC-3 — a triple with a supersession pointer must land on KG (not a
    # verbatim drawer), because the recall reader needs kg_subgraph traversal
    # to resolve the pointer.
    triple = {
        "subject": "acme-org/project",
        "predicate": "branch_naming",
        "object": "feat-<issue>-<slug>",
        "superseded_by": "kg-abc123",
    }
    assert orch.save("ac-3", triple)
    payload = src.retrieve("ac-3")
    assert payload["routing_kind"] == SURFACE_KG
    assert payload["emission_kind"] == EMISSION_JSON
    assert payload["surface_rule"] == "R2"
    assert payload.get("superseded_by") == "kg-abc123"


def test_save_wiring_routing_never_fails_a_save(tmp_path):
    """If _derive_surface raises, save() must still succeed (spec §7.3)."""
    orch, src, _ = _make_orchestrator(str(tmp_path))

    # Inject a deterministic failure in _derive_surface to prove the save path
    # does not crash — routing metadata is best-effort (see §7.3 and the
    # try/except in orchestrator.save).
    orig = orch._derive_surface
    def _broken(*a, **kw):
        raise RuntimeError("routing derivation blew up")
    orch._derive_surface = _broken  # type: ignore[assignment]
    try:
        assert orch.save("no-routing", "a message", profile=None)
        payload = src.retrieve("no-routing")
        assert payload is not None
        # The save itself is independent of the routing metadata — the body
        # must still be persistable and recoverable, whether the source stores
        # it as the raw value or wrapped in a dict.
        if isinstance(payload, dict):
            assert any(k in payload for k in ("_content", "text", "body", "content", "value"))
        else:
            assert str(payload)
    finally:
        orch._derive_surface = orig


# =========================================================================== #
# Determinism / purity (spec §4 — same input ⇒ same output, no side effects)
# =========================================================================== #


def test_route_is_deterministic_and_pure():
    """route() is a pure function: same input ⇒ same output, no clock, no I/O."""
    args = [
        ({"subject": "s", "predicate": "p", "object": "o"},
         RouteContext(fact_kind=FactKind.LONG_LIVED_KNOWLEDGE)),
        ("a preference", RouteContext(is_standing_preference=True)),
        ({"text": "note"}, RouteContext(fact_kind=FactKind.VERBATIM_ARTIFACT)),
    ]
    for content, ctx in args:
        first = route(content, RouteContext(**(dict(
            fact_kind=ctx.fact_kind,
            has_validity_window=ctx.has_validity_window,
            is_standing_preference=ctx.is_standing_preference,
            is_relationship_shape=ctx.is_relationship_shape,
            as_of=ctx.as_of,
        ))))
        second = route(content, RouteContext(**(dict(
            fact_kind=ctx.fact_kind,
            has_validity_window=ctx.has_validity_window,
            is_standing_preference=ctx.is_standing_preference,
            is_relationship_shape=ctx.is_relationship_shape,
            as_of=ctx.as_of,
        ))))
        assert first == second


def test_route_does_not_mutate_input_or_context_beyond_expected():
    """route() mutates context.surface/.rule/.reason (the decision channel)
    and leaves the input content untouched."""
    content = {"subject": "s", "predicate": "p", "object": "o"}
    content_before = dict(content)
    ctx = RouteContext(fact_kind=FactKind.LONG_LIVED_KNOWLEDGE)
    route(content, ctx)
    assert content == content_before


# =========================================================================== #
# OPSEC — no credentials / tokens / connection strings in test fixtures
# =========================================================================== #


@pytest.mark.parametrize(
    "field",
    ["subject", "predicate", "object", "valid_from", "valid_to", "superseded_by"],
)
def test_no_opsec_tokens_in_routing_fixtures(field):
    """Fixtures here are structural (subject/predicate/object).  None of them
    contain mail domains, absolute paths from a host, or credentials."""
    for value in AC_ROWS:
        content = value[1]
        if isinstance(content, dict) and field in content and content[field] is not None:
            s = str(content[field]).lower()
            assert "gmail.com" not in s
            assert "bubo@" not in s
            assert "secret=" not in s
            assert "password" not in s


# =========================================================================== #
# Sanity: the whole AC battery in one table for at-a-glance review
# =========================================================================== #


def test_ac_table_summary_all_green(tmp_path):
    """One-shot assertion: every AC row resolves to the same expected surface
    through a single real orchestrator (no per-row save calls in this test).

    This is the *integration* version of the spec §6 table.  The pure
    classifier is asserted row-by-row above; this is a belt-and-braces check
    that the live wiring produces the same surface distribution.
    """
    orch, src, MemoryProfile = _make_orchestrator(str(tmp_path))

    mapping = {
        "ac-1": ("MEMORY", "str", "R1",
                 ("OPSEC zero-tol: no agent names in commit trailers "
                  "or diffs", MemoryProfile.USER_PREFERENCE)),
        "ac-2": ("KG", "json", "R2",
                 ({"subject": "MemChorus", "predicate": "ci_pins",
                   "object": "ruff==0.16.6", "valid_from": "2026-09-20",
                   "valid_to": None}, None)),
        "ac-3": ("KG", "json", "R2",
                 ({"subject": "acme-org/project", "predicate": "branch_naming",
                   "object": "feat-<issue>-<slug>", "superseded_by": "kg-abc123"}, None)),
        "ac-4": ("DRAWER", "str", "R3",
                 ("Benchmark run: 46 files changed, 20 tests pass, 1 skipped", None)),
        "ac-5": ("DRAWER", "str", "R3",
                 ({"text": "CI LINT gotcha: ci.yml pins ruff==0.16.6"}, None)),
        "ac-6": ("DRAWER", "json", "R4",
                 ({"subject": "MemChorus", "predicate": "ci_pins",
                   "object": "ruff==0.15.x", "valid_from": "2026-08-01",
                   "valid_to": "2026-08-15"}, None)),
        "ac-7": ("KG", "json", "R2",
                 ({"subject": "AgentA", "predicate": "installed_from",
                   "object": "gh@sha", "valid_from": None,
                   "superseded_by": None}, None)),
    }
    for key, (exp_surface, exp_emission, exp_rule, (value, profile)) in mapping.items():
        assert orch.save(key, value, profile=profile)
        payload = src.retrieve(key)
        assert payload["routing_kind"] == exp_surface, f"{key}: {payload}"
        assert payload["emission_kind"] == exp_emission, f"{key}: {payload}"
        assert payload["surface_rule"] == exp_rule, f"{key}: {payload}"


__all__ = ["AC_ROWS"]
