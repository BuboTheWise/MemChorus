"""(#209 working-state surface (b)) unit tests for CorpusBalancer.compute.

Gen-2 contract: three read-only metrics (M1 active-project ratio, M2
settled-knowledge ratio, M3 closet health) computed purely from a standing
``mempalace_list_drawers`` MCP tool, classified into ``ok`` / ``warn`` /
``crit`` per the §4 threshold table in
``DESIGN-(b)-corpus-imbalance-diagnostic.md``.

AC-S1 anchor cases (from the TRIAGE-209 decision memo §6):

  * default palace  — M1≈1.7% < 5%,  M2≈81% ≥ 50%       → WARN
  * profile_b palace  — M1==0,         M2≈98%, clo...n  → CRIT
  * mode=archive    — deliberately lessons-only profile → OK (suppressed)

All tests are fully offline and deterministic: the balance balancer never
touches the live orchestrator; a fake source whose ``call_tool`` returns the
canned per-wing/per-room ``total`` field the real MemPalace tool returns.
"""

from __future__ import annotations

import pytest

from memchorus.corpus_balancer import (
    CLOSETS_COLLECTION_NAME,
    CorpusBalancer,
    DEFAULT_PROJECT_MIN_RATIO,
    WORKING_STATE_ROOMS,
    render_header_line,
)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #

class _FakeSource:
    """Stand-in for ``MemPalaceMemorySource``.

    The balancer probes via ``mempalace_list_drawers``; each call carries
    a ``wing=`` or ``room=`` filter (or neither, for the unfiltered corpus
    total) plus a ``limit``.  The fake returns the matching ``total`` from
    the canned ``wing_counts`` / ``room_counts`` maps (or ``total`` for the
    unfiltered probe) and counts every call so cache probes can be asserted.
    """

    def __init__(
        self,
        total: int,
        wing_counts=None,
        room_counts=None,
        raise_on=None,
    ) -> None:
        self.total = total
        self.wing_counts = dict(wing_counts or {})
        self.room_counts = dict(room_counts or {})
        # set of argument-filter keys whose probe should raise (e.g. {"room"})
        self.raise_on = set(raise_on or ())
        self.calls = 0

    def call_tool(self, name, arguments=None):
        assert name == "mempalace_list_drawers", name
        arguments = arguments or {}
        if self.raise_on:
            if any(k in arguments for k in self.raise_on):
                raise ConnectionError("MCP server down")
        n = self._lookup(arguments)
        return {"wing": arguments.get("wing"), "room": arguments.get("room"),
                "total": n, "count": min(1, n), "drawers": []}

    def _lookup(self, arguments):
        if "wing" in arguments:
            return int(self.wing_counts.get(arguments["wing"], 0))
        if "room" in arguments:
            return int(self.room_counts.get(arguments["room"], 0))
        return int(self.total)


# Sentinel so we can distinguish "do not inject a closet-presence resolver"
# (fall back to the default) from "inject a resolver that reports unknown".
_UNSET = object()


def _balancer(
    source: _FakeSource,
    config: dict | None = None,
    closet_presence=_UNSET,
    closet_stats=None,
    now=None,
    ttl_seconds=60.0,
) -> CorpusBalancer:
    # Sentinel so we can distinguish "do not inject a closet-presence resolver"
    # (fall back to the default) from "inject a resolver that reports unknown".
    if closet_presence is _UNSET:
        presence_fn = None
    else:
        value = closet_presence  # type: ignore[assignment]  # bool | None

        def presence_fn() -> "bool | None":  # type: ignore[no-redef]
            return value
    return CorpusBalancer(
        get_source=lambda: source,
        get_config=lambda: config or {},
        now=lambda: (now if now is not None else 1_700_000_000.0),
        closet_presence=presence_fn,
        closet_counter_source=(
            (lambda: closet_stats or {}) if closet_stats is not None else None
        ),
        ttl_seconds=ttl_seconds,
    )


# --------------------------------------------------------------------------- #
# AC-S1 anchor scenarios                                                       #
# --------------------------------------------------------------------------- #

def test_acs1_default_palace_warn():
    # 578 drawers; ~10 working-state / project drawers (≈1.7%); ~470 settled
    # (≈81%).  M1 below the 5% floor AND M2 above the 50% ceiling → WARN.
    src = _FakeSource(
        total=578,
        room_counts={"working-state": 6, "current-status": 4, "tasks": 0},
        wing_counts={
            "memchorus_general": 400,
            "memchorus_learning": 40,
            "memchorus_decisions": 30,
            "self-improvement": 0,
            # project wings contribute nothing active
        },
    )
    b = _balancer(src, closet_presence=True)
    rpt = b.compute("memchorus", skip_cache=True)

    assert rpt["level"] == "warn"
    assert "m1_below_floor" in rpt["thresholds_hit"]
    assert "m2_above_threshold" in rpt["thresholds_hit"]
    assert rpt["m1"]["total"] == 10
    assert rpt["m1"]["ratio"] == pytest.approx(10 / 578, abs=1e-3)
    assert rpt["m2"]["total"] == 470
    assert rpt["m2"]["ratio"] == pytest.approx(470 / 578, abs=1e-3)
    assert render_header_line(rpt) is not None  # WARN is surfaced


def test_acs1_profile_b_crit():
    # 216 drawers; zero active-project state (M1==0); 212 settled (≈98%);
    # the closets collection is MISSING → CRIT (the strongest tier).
    src = _FakeSource(
        total=216,
        room_counts={"working-state": 0, "current-status": 0, "tasks": 0},
        wing_counts={
            "memchorus_general": 200,
            "memchorus_learning": 12,
            "memchorus_decisions": 0,
            "self-improvement": 0,
        },
    )
    b = _balancer(src, closet_presence=False)
    rpt = b.compute("profile_b", skip_cache=True)

    assert rpt["level"] == "crit"
    assert "m1_zero_active" in rpt["thresholds_hit"]
    # the amplifier is recorded because closets are provably absent
    assert "m3_closets_missing" in rpt["thresholds_hit"]
    assert rpt["m1"]["total"] == 0
    assert rpt["m1"]["ratio"] == 0.0
    assert rpt["m2"]["total"] == 212
    assert "missing" in rpt["reason"].lower()


def test_acs1_archive_mode_ok():
    # Identical census to the crit case, but mode=archive short-circuits to OK
    # and suppresses the header line entirely.
    src = _FakeSource(
        total=216,
        room_counts={"working-state": 0, "current-status": 0, "tasks": 0},
        wing_counts={"memchorus_general": 200, "memchorus_learning": 12},
    )
    b = _balancer(src, config={"balance": {"mode": "archive"}}, closet_presence=False)
    rpt = b.compute("profile_b", skip_cache=True)

    assert rpt["level"] == "ok"
    assert "mode_archive" in rpt["thresholds_hit"]
    assert "archive" in rpt["reason"].lower()
    assert render_header_line(rpt) is None  # archived profiles stay silent


# --------------------------------------------------------------------------- #
# Classification boundaries                                                    #
# --------------------------------------------------------------------------- #

def test_healthy_corpus_is_ok():
    # Active-project state (project wing) dominates → M1 above the floor → OK.
    src = _FakeSource(
        total=100,
        wing_counts={"memchorus": 60, "memchorus_general": 20},
        room_counts={"working-state": 15, "current-status": 5, "tasks": 0},
    )
    b = _balancer(src, closet_presence=True)
    rpt = b.compute("memchorus", skip_cache=True)
    assert rpt["level"] == "ok"
    assert render_header_line(rpt) is None


def test_m1_zero_is_crit_even_when_m2_low():
    # CRIT is driven by M1==0 alone — a thin settled corpus does not rescue it.
    src = _FakeSource(
        total=40,
        room_counts={"working-state": 0, "current-status": 0, "tasks": 0},
        wing_counts={"memchorus_general": 30},
    )
    b = _balancer(src, closet_presence=True)
    rpt = b.compute("memchorus", skip_cache=True)
    assert rpt["level"] == "crit"
    assert "m1_zero_active" in rpt["thresholds_hit"]


def test_warn_requires_both_m1_low_and_m2_high():
    # M1 thin but settled share below the ceiling → NOT warn (OK).
    src = _FakeSource(
        total=100,
        room_counts={"working-state": 3, "current-status": 0, "tasks": 0},
        wing_counts={"memchorus_general": 10, "memchorus_learning": 5},
    )
    b = _balancer(src, closet_presence=True)
    rpt = b.compute("memchorus", skip_cache=True)
    assert rpt["level"] == "ok"  # M2 = 15/100 = 15% < 50%


# --------------------------------------------------------------------------- #
# M2 census — settled wings AND rooms both count                               #
# --------------------------------------------------------------------------- #

def test_m2_counts_settled_wings_and_rooms():
    src = _FakeSource(
        total=200,
        room_counts={"working-state": 2, "lessons-learned": 80, "corrections": 20},
        wing_counts={"memchorus_general": 70, "memchorus_decisions": 0},
    )
    b = _balancer(src, closet_presence=True)
    rpt = b.compute("memchorus", skip_cache=True)
    # 80 (lessons-learned room) + 20 (corrections room) + 70 (general wing)
    assert rpt["m2"]["total"] == 170
    assert rpt["m2"]["ratio"] == pytest.approx(170 / 200)
    # the white-listed wings/rooms appear in the census so a reviewer can audit
    assert rpt["m2"]["wing_census"].get("memchorus_general") == 70
    assert rpt["m2"]["room_census"].get("lessons-learned") == 80


# --------------------------------------------------------------------------- #
# M3 — closet presence / amplifier                                             #
# --------------------------------------------------------------------------- #

def test_m3_closet_missing_amplifier_on_crit():
    src = _FakeSource(
        total=100,
        room_counts={"working-state": 0, "current-status": 0, "tasks": 0},
        wing_counts={"memchorus_general": 90},
    )
    b = _balancer(src, closet_presence=False,
                  closet_stats={"queries_seen": 42, "bound_results_surfaced": 0})
    rpt = b.compute("memchorus", skip_cache=True)
    assert rpt["level"] == "crit"
    assert rpt["m3"]["closets_present"] is False
    assert rpt["m3"]["closet_collection"] == CLOSETS_COLLECTION_NAME
    assert rpt["m3"]["closet_stats"]["bound_results_surfaced"] == 0


def test_m3_closet_presence_unknown_does_not_force_missing_flag():
    # Presence unknown (None) — CRIT still trips on M1==0, but the
    # "missing" amplifier is NOT asserted because we can't prove absence.
    src = _FakeSource(
        total=50,
        room_counts={"working-state": 0, "current-status": 0, "tasks": 0},
        wing_counts={"memchorus_general": 40},
    )
    b = _balancer(src, closet_presence=None)
    rpt = b.compute("memchorus", skip_cache=True)
    assert rpt["level"] == "crit"
    assert "m1_zero_active" in rpt["thresholds_hit"]
    assert "m3_closets_missing" not in rpt["thresholds_hit"]


# --------------------------------------------------------------------------- #
# Degradation — MCP down / unreadable                                          #
# --------------------------------------------------------------------------- #

def test_mcp_down_degrades_to_partial_ok_silent():
    src = _FakeSource(
        total=500,
        raise_on={"room"},  # room probes raise; total probe still succeeds
    )
    b = _balancer(src, closet_presence=True)
    rpt = b.compute("memchorus", skip_cache=True)
    assert rpt["partial"] is True
    assert rpt["errors"]  # recorded probe failures
    # Not classifiable to a confident level → header stays silent.
    assert render_header_line(rpt) is None


def test_no_source_at_all_never_raises():
    b = CorpusBalancer(
        get_source=lambda: None,
        get_config=lambda: {},
        now=lambda: 1_700_000_000.0,
    )
    # must not raise even though no source is available
    rpt = b.compute("memchorus", skip_cache=True)
    assert isinstance(rpt, dict)
    assert rpt["level"] == "ok"
    assert rpt["partial"] is True
    assert render_header_line(rpt) is None


# --------------------------------------------------------------------------- #
# Config thresholds                                                            #
# --------------------------------------------------------------------------- #

def test_custom_thresholds_respected():
    # A 50/50 split is normally healthy, but a strict project_min_ratio of 0.6
    # plus a low settled ceiling flags it as WARN.
    src = _FakeSource(
        total=100,
        room_counts={"working-state": 5, "current-status": 0, "tasks": 0},
        wing_counts={"memchorus_general": 30, "memchorus_learning": 15},
    )
    config = {"balance": {"project_min_ratio": 0.6, "settled_threshold": 0.2}}
    b = _balancer(src, config=config, closet_presence=True)
    rpt = b.compute("memchorus", skip_cache=True)
    assert rpt["m1"]["project_min_ratio"] == pytest.approx(0.6)
    assert rpt["m2"]["settled_threshold"] == pytest.approx(0.2)
    assert rpt["level"] == "warn"


def test_disabled_config_block_reads_safely():
    b = CorpusBalancer(
        get_source=lambda: _FakeSource(total=10),
        get_config=lambda: {"balance": "not-a-dict"},  # malformed
        now=lambda: 1_700_000_000.0,
    )
    assert b.cfg_block() == {}


def test_default_project_min_ratio_is_five_percent():
    assert DEFAULT_PROJECT_MIN_RATIO == pytest.approx(0.05)


def test_working_state_rooms_are_configurable_surface():
    # The room tier is the v1 signal — sanity-check the constant is stable.
    assert "working-state" in WORKING_STATE_ROOMS
    assert "current-status" in WORKING_STATE_ROOMS
    assert "tasks" in WORKING_STATE_ROOMS


# --------------------------------------------------------------------------- #
# TTL cache                                                                    #
# --------------------------------------------------------------------------- #

def test_within_ttl_serves_cache_and_does_not_reprobe():
    calls = {"n": 0}

    class _CountingSource:
        def call_tool(self, name, arguments=None):
            calls["n"] += 1
            arguments = arguments or {}
            n = {"working-state": 10, "memchorus_general": 80}
            if "wing" in arguments:
                return {"total": n.get(arguments["wing"], 0)}
            return {"total": 100}

    clock = {"t": 1_700_000_000.0}
    b = CorpusBalancer(
        get_source=lambda: _CountingSource(),
        get_config=lambda: {},
        now=lambda: clock["t"],
        ttl_seconds=60.0,
    )
    b.compute("memchorus")
    first = calls["n"]
    assert first > 0
    # same clock, within TTL → served from cache, no new MCP probes
    b.compute("memchorus")
    assert calls["n"] == first


def test_past_ttl_recomputes_and_reprobes():
    calls = {"n": 0}

    class _CountingSource:
        def call_tool(self, name, arguments=None):
            calls["n"] += 1
            return {"total": 100}

    clock = {"t": 1_700_000_000.0}
    b = CorpusBalancer(
        get_source=lambda: _CountingSource(),
        get_config=lambda: {},
        now=lambda: clock["t"],
        ttl_seconds=60.0,
    )
    b.compute("memchorus")
    first = calls["n"]
    clock["t"] += 120.0  # beyond TTL
    b.compute("memchorus")
    assert calls["n"] > first


def test_skip_cache_bypasses_lru():
    calls = {"n": 0}

    class _CountingSource:
        def call_tool(self, name, arguments=None):
            calls["n"] += 1
            return {"total": 100}

    b = CorpusBalancer(
        get_source=lambda: _CountingSource(),
        get_config=lambda: {},
        now=lambda: 1_700_000_000.0,
    )
    b.compute("memchorus")
    b.compute("memchorus", skip_cache=True)
    assert calls["n"] >= 2


# --------------------------------------------------------------------------- #
# Report shape completeness                                                    #
# --------------------------------------------------------------------------- #

def test_report_carries_full_contract():
    src = _FakeSource(
        total=100,
        room_counts={"working-state": 5, "current-status": 0, "tasks": 0},
        wing_counts={"memchorus_general": 80, "memchorus": 15},
    )
    b = _balancer(src, closet_presence=True)
    rpt = b.compute("memchorus", skip_cache=True)
    for key in (
        "level", "reason", "active_project", "mode", "m1", "m2", "m3",
        "total_corpus", "thresholds_hit",
    ):
        assert key in rpt, f"missing {key}"
    assert rpt["active_project"] == "memchorus"
    assert rpt["total_corpus"] == 100
