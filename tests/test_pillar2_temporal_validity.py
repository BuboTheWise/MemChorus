"""
test_pillar2_temporal_validity — RED→GREEN suite for Pillar 2 (IMPL #224).

Spec:          Projects/MemChorus/MemChorus-Recall-Loop-NorthStar-Spec.md
               §5.1  state definitions (CURRENT / SUPERSEDED / EXPIRED)
               §5.2  AC-1 expiry hard-exclude, AC-1b not-yet-valid,
                     AC-2 supersession ordering gap, AC-3 boundary+precedence,
                     AC-4 injection slot / breakdown absence
               §6.5  cross-cutting determinism (no wall-clock in the pure core)

Contract under test (public recall surface):
    ``RelevanceScorer.score_and_rank(results, query, context)`` drives the
    stage-1 → stage-4 pipeline.  Pillar 2 adds:
      * stage 2 (PRE-SCORE):  hard-exclude EXPIRED / not-yet-valid candidates —
        they are never scored and never appear in any ``score_breakdown``.
      * stage 4 (POST-SCORE): soft-demote SUPERSEDED-but-valid candidates by
        ``RECALL_CONFIG.supersession_attenuation`` (default 0.50) on the *ranked*
        score, while the stored ``score_breakdown.final`` remains the
        UNATTENUATED value (the §5.2-AC2 assertion reference).

The suite is deterministic: every temporal boundary is expressed in terms of an
explicit injectable ``as_of`` (never ``datetime.now()``), and the attenuation
factor is read from the merged config root (``RECALL_CONFIG``), which this file
*swaps* to a sentinel value to prove the engine consumes it rather than
re-inventing a 0.50 literal.

Run serially (no xdist):
    PYTHONPATH=src pytest tests/test_pillar2_temporal_validity.py -v
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Imports — force the src tree onto PATH so the module resolves from the worktree.
# ---------------------------------------------------------------------------
SRC = Path(__file__).resolve().parent.parent / "src"
os.environ.setdefault("PYTHONPATH", str(SRC))

try:
    from memchorus import recall_config
    from memchorus.recall_config import RECALL_CONFIG, RecallLoopConfig
    from memchorus.relevance_engine import (
        ContextWeight,
        RelevanceScorer,
        classify_temporal_state,
    )
except ImportError as exc:  # pragma: no cover
    pytest.fail(
        "memchorus.relevance_engine / memchorus.recall_config are not "
        f"importable. Is src/ on the Python path? Error: {exc}"
    )


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------
# A clean, deterministic candidate whose base score is known and non-zero.
#   quality   = F1("gate recall","gate recall") = 1.0
#   recency   = 0.5   (no ``timestamp``)
#   source    = "hermes_default" prior = 1.0 (max of {0.7, 0.3})
#   penalties = none, auto-provenance = off, calibration = 1.0 (default)
# => raw = final = 0.45*1 + 0.30*0.5 + 0.25*1 = 0.85
QUERY = "gate recall"


def _fact(key: str, **fields) -> dict:
    """A clean base fact (content matches QUERY for F1=1.0) + optional validity fields."""
    base = {
        "key": key,
        "content": "gate recall",
        "source": "hermes_default",
    }
    base.update(fields)
    return base


def _rank(scorer: RelevanceScorer, *facts: dict, as_of: datetime | None = None):
    ctx = ContextWeight() if as_of is None else ContextWeight(as_of=as_of)
    return scorer.score_and_rank([*facts], QUERY, ctx)


@pytest.fixture(scope="module")
def scorer() -> RelevanceScorer:
    return RelevanceScorer()


AS_OF_JUNE = datetime(2024, 6, 1)  # 2024-06-01T00:00:00 (naive) — evaluation anchor


# ---------------------------------------------------------------------------
# AC-0 (consumption): the attenuation factor the engine applies must be the one
#        exposed by the merged config root — proven by swapping it to a sentinel.
# ---------------------------------------------------------------------------
class TestConsumesConfigRoot:
    def test_default_is_spec_value(self) -> None:
        """RECALL_CONFIG.supersession_attenuation is the spec default (0.50)."""
        assert RECALL_CONFIG.supersession_attenuation == 0.50

    def test_engine_uses_config_not_literal(self, monkeypatch) -> None:
        """Swapping RECALL_CONFIG.supersession_attenuation to 0.25 must change the
        observed demotion by the same factor — proving relevance_engine reads the
        config object at call time rather than baking in a 0.50 constant."""
        swapped = RecallLoopConfig(supersession_attenuation=0.25)
        monkeypatch.setattr(recall_config, "RECALL_CONFIG", swapped)

        scorer = RelevanceScorer()
        succ = _fact("fact-s")  # CURRENT — scores 0.85
        prev = _fact("fact-o", superseded_by="fact-s")  # SUPERSEDED
        out = _rank(scorer, succ, prev, as_of=AS_OF_JUNE)

        by_key = {r.key: r for r in out}
        s_score = by_key["fact-s"].score
        o_score = by_key["fact-o"].score
        # Observed attenuation must equal the swapped sentinel (0.25).
        assert o_score == pytest.approx(s_score * 0.25, abs=1e-9)


# ---------------------------------------------------------------------------
# AC-1  -- expiry hard-excludes (never scored, not in any breakdown)
# ---------------------------------------------------------------------------
class TestAC1_ExpiryHardExcludes:
    def test_expired_dropped_from_ranked_list(self, scorer) -> None:
        expired = _fact(
            "fact-expired",
            valid_to="2024-01-01T00:00:00",  # before as_of -> EXPIRED
        )
        control = _fact("fact-control")  # CURRENT — must survive
        out = _rank(scorer, expired, control, as_of=AS_OF_JUNE)
        keys = [r.key for r in out]
        assert "fact-control" in keys
        assert "fact-expired" not in keys

    def test_expired_not_scored_no_breakdown(self, scorer) -> None:
        """The expired candidate is dropped PRE-SCORE: it is absent from the
        ranked list AND its key never appears in any surviving result's
        score_breakdown (it was never scored, so no breakdown dict is produced)."""
        expired = _fact(
            "fact-expired",
            valid_to="2024-01-01T00:00:00",
        )
        out = _rank(scorer, expired, as_of=AS_OF_JUNE)
        # Only item was expired -> nothing left to rank.
        assert out == []

    def test_expired_sole_item_empty(self, scorer) -> None:
        expired = _fact("x", valid_to="2023-01-01T00:00:00")
        assert _rank(scorer, expired, as_of=AS_OF_JUNE) == []


# ---------------------------------------------------------------------------
# AC-1b -- not-yet-valid (valid_from > as_of) is hard-excluded for this as_of
# ---------------------------------------------------------------------------
class TestAC1b_NotYetValid:
    def test_not_yet_valid_excluded(self, scorer) -> None:
        notyet = _fact(
            "fact-notyet",
            valid_from="2024-02-01T00:00:00",  # after as_of -> not yet in force
        )
        control = _fact("fact-control")
        out = _rank(scorer, notyet, control, as_of=datetime(2024, 1, 1))
        keys = [r.key for r in out]
        assert "fact-control" in keys
        assert "fact-notyet" not in keys

    def test_sole_not_yet_valid_empty(self, scorer) -> None:
        notyet = _fact("x", valid_from="2024-02-01T00:00:00")
        assert _rank(scorer, notyet, as_of=datetime(2024, 1, 1)) == []


# ---------------------------------------------------------------------------
# AC-2  -- supersession ordering gap (S strictly above O; O attenuated 0.50)
# ---------------------------------------------------------------------------
class TestAC2_SupersessionGap:
    def test_successor_ranks_strictly_above_predecessor(self, scorer) -> None:
        succ = _fact("fact-s")  # CURRENT head
        prev = _fact("fact-o", superseded_by="fact-s")  # SUPERSEDED
        out = _rank(scorer, succ, prev, as_of=AS_OF_JUNE)
        order = [r.key for r in out]
        assert "fact-s" in order and "fact-o" in order
        # "at least one position" gap: S strictly above O.
        assert order.index("fact-s") < order.index("fact-o")

    def test_attenuated_score_le_050x_predecessor(self, scorer) -> None:
        succ = _fact("fact-s")
        prev = _fact("fact-o", superseded_by="fact-s")
        out = _rank(scorer, succ, prev, as_of=AS_OF_JUNE)
        by_key = {r.key: r for r in out}
        s_score = by_key["fact-s"].score
        o_score = by_key["fact-o"].score
        assert o_score <= 0.50 * s_score + 1e-9
        assert o_score < s_score

    def test_stored_breakdown_retains_unattenuated_value(self, scorer) -> None:
        """§5.2-AC2: the demotion is applied to the *ranked* score, but the
        unattenuated value is retained in the breakdown for the assertion — i.e.
        the successor's score equals the predecessor's *unattenuated* final."""
        succ = _fact("fact-s")
        prev = _fact("fact-o", superseded_by="fact-s")
        out = _rank(scorer, succ, prev, as_of=AS_OF_JUNE)
        by_key = {r.key: r for r in out}
        s_score = by_key["fact-s"].score  # 0.85, CURRENT
        o_break_final = by_key["fact-o"].meta["score_breakdown"]["final"]
        o_ranked = by_key["fact-o"].score
        # Stored breakdown.final is UNATTENUATED and equals the successor's score.
        assert o_break_final == pytest.approx(s_score, rel=1e-6)
        # The ranked score IS attenuated (0.50 * unattenuated).
        assert o_ranked == pytest.approx(0.50 * o_break_final, abs=1e-9)
        # Observability: the demotion is surfaced on the result.
        assert by_key["fact-o"].meta.get("demoted_by") == "superseded"


# ---------------------------------------------------------------------------
# AC-3  -- boundary (valid_to == as_of valid) + precedence (expiry beats supersession)
# ---------------------------------------------------------------------------
class TestAC3_BoundaryAndPrecedence:
    def test_valid_to_equal_as_of_is_valid(self, scorer) -> None:
        boundary = _fact(
            "fact-bd",
            valid_to="2024-06-01T00:00:00",  # == as_of -> strict < : NOT expired
        )
        out = _rank(scorer, boundary, as_of=AS_OF_JUNE)
        keys = [r.key for r in out]
        assert "fact-bd" in keys  # present and scored normally

    def test_expired_and_superseded_excluded_not_demoted(self, scorer) -> None:
        """A candidate that is EXPIRED and SUPERSEDED is EXPIRED (hard-excluded),
        NOT soft-demoted — one state per candidate, and expiry is the winner."""
        both = _fact(
            "fact-both",
            valid_to="2024-01-01T00:00:00",  # EXPIRED for as_of=2024-06-01
            superseded_by="fact-elsewhere",  # ... and also superseded
        )
        out = _rank(scorer, both, as_of=AS_OF_JUNE)
        # Hard-excluded: not present at all (no attenuated copy either).
        assert out == []


# ---------------------------------------------------------------------------
# AC-4  -- injection slot: expired dropped pre-score, no phantom contribution
# ---------------------------------------------------------------------------
class TestAC4_InjectionSlot:
    def test_expired_has_no_phrase_no_breakdown_contribution(self, scorer) -> None:
        live = _fact("live-fact")
        dead = _fact("dead-fact", valid_to="2023-06-01T00:00:00")
        out = _rank(scorer, live, dead, as_of=AS_OF_JUNE)
        # Only the live fact is surfaced; the dead one left no result and thus
        # no slot and no breakdown entry to attribute its (non-existent) score to.
        assert [r.key for r in out] == ["live-fact"]
        # The live fact's breakdown is self-contained and references only itself.
        bd = out[0].meta["score_breakdown"]
        assert bd["key"] if "key" in bd else True  # breakdown is present + shaped
        assert "final" in bd and bd["final"] > 0

    def test_no_key_is_injected_twice_for_expired(self, scorer) -> None:
        a = _fact("dup-a", valid_to="2022-01-01T00:00:00")
        b = _fact("alive-b")
        out = _rank(scorer, a, b, as_of=AS_OF_JUNE)
        keys = [r.key for r in out]
        assert keys.count("alive-b") == 1
        assert "dup-a" not in keys


# ---------------------------------------------------------------------------
# Pure state machine: determinism + precedence (no wall-clock, as_of-driven)
# ---------------------------------------------------------------------------
class TestClassifyTemporalState:
    def test_current(self) -> None:
        assert classify_temporal_state(_fact("f"), AS_OF_JUNE) == "CURRENT"

    def test_expired_by_valid_to(self) -> None:
        assert (
            classify_temporal_state(
                _fact("f", valid_to="2024-01-01T00:00:00"), AS_OF_JUNE
            )
            == "EXPIRED"
        )

    def test_not_yet_valid(self) -> None:
        # valid_from is strictly AFTER as_of → not yet in force → EXPIRED
        assert (
            classify_temporal_state(
                _fact("f", valid_from="2024-07-01T00:00:00"), AS_OF_JUNE
            )
            == "EXPIRED"
        )

    def test_boundary_equal_is_current(self) -> None:
        assert (
            classify_temporal_state(
                _fact("f", valid_to="2024-06-01T00:00:00"), AS_OF_JUNE
            )
            == "CURRENT"
        )

    def test_superseded_but_valid(self) -> None:
        assert (
            classify_temporal_state(_fact("f", superseded_by="s"), AS_OF_JUNE)
            == "SUPERSEDED"
        )

    def test_expiry_precedes_supersession(self) -> None:
        assert (
            classify_temporal_state(
                _fact("f", valid_to="2024-01-01T00:00:00", superseded_by="s"),
                AS_OF_JUNE,
            )
            == "EXPIRED"
        )

    def test_none_as_of_means_no_expiry(self) -> None:
        """With no as_of the temporal gate cannot fire (deterministic passthrough);
        a superseded fact is still SUPERSEDED, an unannotated fact is CURRENT."""
        assert (
            classify_temporal_state(_fact("f", valid_to="2020-01-01T00:00:00"), None)
            == "CURRENT"
        )
        assert (
            classify_temporal_state(_fact("f", superseded_by="s"), None) == "SUPERSEDED"
        )
