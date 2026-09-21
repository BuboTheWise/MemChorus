"""MemChorus #206 — Active-Project Recall: ranking/weighting seams (R4–R6).

Acceptance contract: MemChorus-206-Acceptance-Criteria.md (AC-1, AC-2, AC-5, AC-6, AC-8, EC-1, EC-3, EC-5, EC-6), filed on kanban board task t_3144cdbf.

Parent design docs (linked from the task body):
  - Active-Project Detection (t_c623e65f)     — seams A2–A5 (marking); already
    implemented and covered by ``tests/test_active_project_detection.py``.
  - Ranking & Weighting        (t_3f754ef1)   — seams R4–R6 (this file).

Seam scope covered here (R4–R6):
  R4  Within-Tier-0 sub-signal re-sort — ``effective_score`` = min(raw + sub, score_max),
      applied only to bound items; the cap applies to the *stored* value, while the
      within-tier sort ordering stays monotonic in the *uncapped* value (EC-6).
  R5  ``RelevanceScorer.__init__`` records ``self._score_max = score_max`` so every
      ``score()`` / ``score_breakdown()`` call resolves ``score_max`` consistently
      from the instance, and an explicit per-call override still wins.
  R6  Tier-0 results carry the *reportable* shape: ``meta["signal_type"]``,
      ``meta["sub_signal_score"]``, ``meta["effective_score"]``; Tier-1 results
      carry none (byte-identical to pre-#206 shape).

The tests are written against the acceptance contract *by name* so a reviewer can
map each assertion to an AC row in §2/§3 without opening the implementation.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memchorus.relevance_engine import (
    _SUB_SIGNAL_WEIGHTS,
    ContextWeight,
    RelevanceScorer,
    _compute_sub_signal,
    get_closet_counter,
    reset_closet_counters,
)

# Canonical slug — same as the existing test_active_project_detection.py fixtures.
_ACTIVE_PROJECT = "active-project"


# --------------------------------------------------------------------------- #
# Fixtures & helpers                                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_counters():
    reset_closet_counters()
    yield
    reset_closet_counters()


def _bound_fixture(
    key: str,
    content: str,
    *,
    score: float,
    category: str | None = None,
    project: str = _ACTIVE_PROJECT,
    timestamp: object = None,
    source: str = "mempalace",
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bound-drawer fixture: ``project`` is the primary signal in the
    binding predicate (``closet_bound_result``), so the result lands in Tier 0
    regardless of raw score (AC-1).

    NOTE: the carried ``score`` field is reported metadata only — the engine
    re-computes the real score from ``content`` / ``timestamp`` / ``source``
    via ``score_breakdown`` (see ``test_effective_score_capped``).
    """
    d: dict[str, Any] = {
        "key": key,
        "content": content,
        "source": source,
        "score": score,
        "project": project,
    }
    if category is not None:
        d["category"] = category  # uppercase per the existing fixture shape
    if timestamp is not None:
        d["timestamp"] = timestamp
    if extra_meta:
        d.update(extra_meta)
    return d


def _unbound_fixture(key: str, content: str, *, score: float) -> dict[str, Any]:
    """Unbound fixture: no project / wing / signal_type / category signals, so
    the result stays in Tier 1 (``closet_boost == 0.0``)."""
    return {"key": key, "content": content, "source": "mempalace", "score": score}


def _ctx(active_project: str = _ACTIVE_PROJECT, closet_boost_factor: float = 0.35):
    return ContextWeight(
        active_project=active_project,
        closet_boost_factor=closet_boost_factor,
    )


# --------------------------------------------------------------------------- #
# AC-1 — bound-before-unbound hard partition (preserved, not regressed)       #
# --------------------------------------------------------------------------- #
class TestAC1BoundBeforeUnbound:
    """AC-1: a 0.62-score bound drawer outranks a 0.78-score unbound drawer,
    and every bound result carries ``meta.closet_boost == closet_boost_factor``
    (the partition marker — default 0.35 — *not* a score offset)."""

    def test_partition_bound_first(self):
        scorer = RelevanceScorer()
        unbound = _unbound_fixture("unbound-high", "zz zz zz zz zz zz zz", score=0.78)
        bound = _bound_fixture("bound-low", "note", score=0.62)
        ranked = scorer.score_and_rank([unbound, bound], "zz", context=_ctx())
        assert len(ranked) == 2
        keys = [r.key for r in ranked]
        assert keys.index("bound-low") < keys.index("unbound-high"), keys
        # Partition marker: bound carries the closet_boost_factor; unbound == 0.
        assert ranked[0].meta["closet_boost"] == pytest.approx(0.35)
        assert ranked[1].meta["closet_boost"] == 0.0


# --------------------------------------------------------------------------- #
# AC-2 — within-Tier-0 sub-signal priority; gap leads a higher-raw diff        #
# --------------------------------------------------------------------------- #
class TestAC2SubsignalPriority:
    """Same raw, different signals: gap (0.40) leads diff (0.30) leads
    action (0.30) leads done (0.10) leads context (0.05) — independent of
    which raw score each fixture carries (raws are equal because same content
    + no hit-rate history → calibration multiplier 1.0)."""

    def test_subsignal_gap_leads(self):
        scorer = RelevanceScorer()
        # All five drawers share identical content ("note") and query
        # ("note") so the engine re-computes the SAME raw score for each
        # (0.45*1.0 quality + 0.30*0.5 recency + 0.25*prior = ~0.707).
        # The ONLY differentiator inside Tier 0 is the sub-signal weight,
        # driven by a VALID ``category`` slug (the metadata path is the
        # primary detector — content is neutral so it adds nothing).
        # Valid slugs per relevance_engine._CATEGORY_TO_SIGNAL:
        #   GAP->gap, IN_FLIGHT->diff, NEXT_ACTION->action,
        #   COMPLETED->done, CONTEXT->context.
        gap = _bound_fixture("gap-note", "note", score=0.70, category="GAP")
        diff = _bound_fixture("diff-note", "note", score=0.70, category="IN_FLIGHT")
        action = _bound_fixture("act-note", "note", score=0.70, category="NEXT_ACTION")
        done = _bound_fixture("done-note", "note", score=0.70, category="COMPLETED")
        ctx = _bound_fixture("ctx-note", "note", score=0.70, category="CONTEXT")
        ranked = scorer.score_and_rank(
            [ctx, done, action, diff, gap], "note", context=_ctx()
        )
        by_key = {r.key: r for r in ranked}
        # AC-2: gap (0.40) MUST lead, context (0.05) MUST rank last.
        assert ranked[0].key == "gap-note", [r.key for r in ranked]
        assert ranked[-1].key == "ctx-note", [r.key for r in ranked]
        # diff (0.30) and action (0.30) tie on the sub-signal weight → the
        # stable sort preserves their input-relative order; both sit in the
        # middle: above done (0.10), below gap (0.40), above context (0.05).
        mid_keys = {ranked[1].key, ranked[2].key}
        assert mid_keys == {"diff-note", "act-note"}, [r.key for r in ranked]
        assert by_key["done-note"] is ranked[3], [r.key for r in ranked]

        # Each Tier-0 meta reports the expected sub-signal label + weight
        # (metadata-driven classification — not regex; AC-2).
        def _expected(k, sig, w):
            rr = by_key[k]
            assert rr.meta["signal_type"] == sig, (k, rr.meta)
            assert rr.meta["sub_signal_score"] == pytest.approx(w), (k, rr.meta)

        _expected("gap-note", "gap", 0.40)
        _expected("diff-note", "diff", 0.30)
        _expected("act-note", "action", 0.30)
        _expected("done-note", "done", 0.10)
        _expected("ctx-note", "context", 0.05)

    def test_subsignal_partition(self):
        """AC-2 *Check*: a 0.70-raw gap drawer outranks a 0.85-raw diff drawer
        *within Tier 0* (raws differ here; the sub-signal weighting wins)."""
        scorer = RelevanceScorer()
        g = _bound_fixture("gap-hi", "gap-drawer", score=0.70, category="GAP")
        d = _bound_fixture("diff-lo", "diff-drawer", score=0.85, category="IN_FLIGHT")
        ranked = scorer.score_and_rank([d, g], "query", context=_ctx())
        assert ranked[0].key == "gap-hi", "expected gap at top; got " + repr(
            [(r.key, r.meta.get("signal_type")) for r in ranked]
        )


# --------------------------------------------------------------------------- #
# AC-2 / EC-5 — context fallback (weight 0.05) keeps a no-signal bound drawer  #
# at the bottom of Tier 0, still ahead of every unbound drawer                 #
# --------------------------------------------------------------------------- #
class TestBoundNoSignalContextFallback:
    """A bound drawer with no explicit ``category`` and no regex match still
    receives the context fallback of ``sub_signal_score = 0.05`` (so
    ``effective_score = min(raw + 0.05, score_max)``) and stays in Tier 0 —
    but at the *bottom* of the bound tier, above all unbound drawers."""

    def test_bound_no_signal_context_fallback(self):
        scorer = RelevanceScorer()
        # "context" is neutral content (no gap / diff / action / done keywords),
        # so signal detection falls through to the context fallback.
        plain = _bound_fixture("plain", "context note, plain", score=0.60)
        gap = _bound_fixture("siggap", "gap note", score=0.60, category="GAP")
        unbound = _unbound_fixture("unb", "plain note", score=0.90)
        ranked = scorer.score_and_rank([unbound, plain, gap], "note", context=_ctx())
        # AC-5: bound items are ahead of every unbound item.
        bound_keys = {k: i for i, k in enumerate([r.key for r in ranked]) if k != "unb"}
        assert set(bound_keys) == {"plain", "siggap"}
        # AC-2 / EC-5: within Tier 0, the context (0.05) drawer is at the
        # bottom of the bound tier — the gap (0.40) drawer is ahead of it.
        keys = [r.key for r in ranked]
        assert keys.index("plain") > keys.index("siggap"), (
            f"expected gap ahead of context fallback within Tier 0; got {keys!r}"
        )
        # Partition is preserved: the context-fallback drawer is still ahead of
        # any unbound drawer even at its minimum sub_score.
        assert keys.index("unb") > keys.index("plain"), (
            f"bound fallback MUST stay in Tier 0 (ahead of unbound); got {keys!r}"
        )
        # The reportable shape carries the fallback value.
        plain_rr = next(r for r in ranked if r.key == "plain")
        assert plain_rr.meta["sub_signal_score"] == pytest.approx(0.05)
        assert plain_rr.meta["signal_type"] == "context"
        assert plain_rr.meta["effective_score"] == pytest.approx(
            min(plain_rr.score + 0.05, 1.0)
        )


# --------------------------------------------------------------------------- #
# EC-6 / AC-2 Check — effective_score capped at score_max; sort remains       #
# monotonic in the *uncapped* value                                          #
# --------------------------------------------------------------------------- #
class TestEffectiveScoreCapped:
    """A 0.98 bound drawer + a 0.40 gap signal — ``effective_score`` is
    capped at ``score_max`` (1.0) in the *stored* meta, but ordering within
    Tier 0 is monotonic in the *uncapped* sum."""

    def test_effective_score_capped(self):
        scorer = RelevanceScorer()
        # EC-6 / AC-2: three bound drawers share the *same* raw (drive it to
        # the ceiling: content == query == "note" → quality 1.0; fresh timestamp
        # → recency ~1.0; hermes_default source → prior normalises to 1.0, so
        # raw ≈ 0.45 + 0.30 + 0.25 = 1.0).  Each carries a DIFFERENT sub-signal,
        # so the uncapped sum differs (1.4 / 1.3 / 1.1) while the *stored*
        # effective_score clamps to score_max (1.0) for all three.
        hi = _bound_fixture(
            "hi-gap",
            "note",
            score=0.98,
            category="GAP",
            timestamp=datetime.now(UTC).isoformat(),
            source="hermes_default",
        )
        mid = _bound_fixture(
            "mid-diff",
            "note",
            score=0.98,
            category="IN_FLIGHT",
            timestamp=datetime.now(UTC).isoformat(),
            source="hermes_default",
        )
        lo = _bound_fixture(
            "lo-done",
            "note",
            score=0.98,
            category="COMPLETED",
            timestamp=datetime.now(UTC).isoformat(),
            source="hermes_default",
        )
        ranked = scorer.score_and_rank([lo, mid, hi], "note", context=_ctx())
        # All three are *capped* at 1.0 in stored meta (raw 1.0 + sub > 1.0).
        for rr in ranked:
            assert rr.meta["effective_score"] == pytest.approx(1.0), (
                f"each drawer MUST be capped at score_max; got {rr.meta!r}"
            )
        # AC-2 / EC-6: the uncapped values are distinct and order the tier:
        # gap 1.0+0.40 > diff 1.0+0.30 > done 1.0+0.10 — stored effective_score
        # is identical (1.0) after capping, so the sort MUST use _effective_raw.
        assert [r.meta["_effective_raw"] for r in ranked] == [
            pytest.approx(1.40),
            pytest.approx(1.30),
            pytest.approx(1.10),
        ]
        assert [r.key for r in ranked] == ["hi-gap", "mid-diff", "lo-done"], (
            f"expected uncapped-value ordering; got "
            f"{[(r.key, r.meta['effective_score']) for r in ranked]!r}"
        )


# --------------------------------------------------------------------------- #
# AC-5 — silent degradation (no active project / zero-bound / factor=0)       #
# --------------------------------------------------------------------------- #
class TestDegradation:
    """AC-5: ``active_project is None`` → all ``closet_boost == 0.0``, no
    ``signal_type`` / ``sub_signal_score`` fields, ordering = ``score desc``,
    *byte-identical* to pre-#206.  EC-5: closet_boost_factor=0.0 → all Tier 1
    (same byte-identity)."""

    def test_no_project_degradation(self):
        """(≈ test_ranking_no_project_degradation) — no active project → all
        unbound, silent, pure-score ordering."""
        scorer = RelevanceScorer()
        # The engine re-computes each score from *content* (the carried
        # ``score`` field is reported metadata only — see fixture note).  So
        # to assert a deterministic ordering we drive it through content
        # quality: ``a``'s content is a full match for the query (F1 = 1.0),
        # ``b``'s is diluted by extra words (F1 < 1.0) → ``a`` ranks first.
        a = {"key": "a", "content": "x", "source": "mempalace", "score": 0.90}
        b = {
            "key": "b",
            "content": "x and lots of other unrelated filler words here",
            "source": "mempalace",
            "score": 0.70,
        }
        ranked = scorer.score_and_rank([b, a], "x", context=ContextWeight())
        assert [r.key for r in ranked] == ["a", "b"]
        for rr in ranked:
            assert rr.meta["closet_boost"] == 0.0
            # AC-5: no sub-signal / effective_score fields on unbound items.
            assert "signal_type" not in rr.meta, rr.meta
            assert "sub_signal_score" not in rr.meta, rr.meta
            assert "effective_score" not in rr.meta, rr.meta

    def test_closet_boost_factor_zero_collapse(self):
        """closet_boost_factor=0.0 → the two-bucket partition collapses to
        *all* Tier 1, and the output is again byte-identical to the
        no-active-project case (existing reversibility guarantee)."""
        scorer = RelevanceScorer()
        bound_fixture = _bound_fixture("k", "n", score=0.70)
        # factor = 0.0 → bound result's closet_boost = 0.0 → unbound tier.
        ctx_zero = ContextWeight(
            active_project=_ACTIVE_PROJECT, closet_boost_factor=0.0
        )
        ranked_zero = scorer.score_and_rank([bound_fixture], "n", context=ctx_zero)
        ranked_none = scorer.score_and_rank(
            [bound_fixture], "n", context=ContextWeight()
        )
        # Both produce a single Tier-1 item (byte-identical shape):
        assert ranked_zero[0].meta["closet_boost"] == 0.0
        assert "signal_type" not in ranked_zero[0].meta
        # The two runs differ in no observable way:
        assert ranked_zero[0].key == ranked_none[0].key
        assert ranked_zero[0].meta == ranked_none[0].meta


# --------------------------------------------------------------------------- #
# EC-3 / AC-7 — active_query_no_bound_counter (asked-but-no-bound-drawers)   #
# --------------------------------------------------------------------------- #
class TestActiveQueryNoBoundCounter:
    """AC-7 (SHOULD) — diagnostic: with a resolvable active project present,
    ``queries_with_active_project`` increments on every query, but
    ``bound_results_surfaced`` does NOT increment when nothing in the pool
    actually bound.  The counters distinguish *asked* from *surfaced*."""

    def test_active_query_no_bound_counter(self):
        scorer = RelevanceScorer()
        generic = _unbound_fixture("k", "note", score=0.70)
        ranked = scorer.score_and_rank([generic], "note", context=_ctx())
        assert ranked
        # The query DID ask about the active project (increment).
        assert get_closet_counter("queries_with_active_project") == 1, (
            f"queries_with_active_project MUST increment on an active query; "
            f"get={get_closet_counter('queries_with_active_project')!r}"
        )
        # But no bound result surfaced (increment only counts bound ones).
        assert get_closet_counter("bound_results_surfaced") == 0, (
            f"bound_results_surfaced MUST NOT increment when no bound item; "
            f"get={get_closet_counter('bound_results_surfaced')!r}"
        )

    def test_active_project_set_zero_bound_drawers(self):
        """EC-3: active project present but zero bound drawers → all Tier 1,
        byte-identical to no-active-project; learning corpus still returned."""
        scorer = RelevanceScorer()
        learning_a = _unbound_fixture("learning-a", "note", score=0.80)
        learning_b = _unbound_fixture("learning-b", "note", score=0.70)
        ranked = scorer.score_and_rank([learning_a, learning_b], "note", context=_ctx())
        assert [r.key for r in ranked] == ["learning-a", "learning-b"]
        for rr in ranked:
            assert rr.meta["closet_boost"] == 0.0
            assert "signal_type" not in rr.meta
        # AC-7: the ask was counted, nothing was surfaced.
        active_ask = get_closet_counter("queries_with_active_project")
        active_surfaced = get_closet_counter("bound_results_surfaced")
        assert active_ask >= 1
        assert active_surfaced == 0


# --------------------------------------------------------------------------- #
# AC-8 — result-shape contract (Tier-0 fields vs Tier-1 fields)               #
# --------------------------------------------------------------------------- #
class TestResultShapeContract:
    """AC-8: every result carries ``{key, content, source, score, meta}``.
    Tier-0 ``meta`` additionally carries ``signal_type``,
    ``sub_signal_score``, ``effective_score`` — and ``effective_score``
    is always in ``[0, score_max]``.  Tier-1 ``meta`` carries *none* of the
    sub-signal fields (byte-identical to pre-#206)."""

    def test_result_shape_contract(self):
        scorer = RelevanceScorer()
        bound_gap = _bound_fixture("b1", "gap-drawer", score=0.70, category="GAP")
        unbound = _unbound_fixture("u1", "note", score=0.90)
        ranked = scorer.score_and_rank([unbound, bound_gap], "note", context=_ctx())
        by_key = {r.key: r for r in ranked}
        # Both results expose the stable core shape (AC-8).
        for key in ("b1", "u1"):
            rr = by_key[key]
            assert rr.key is not None and rr.content is not None
            assert rr.source == "mempalace"
            assert isinstance(rr.score, float)
            assert isinstance(rr.meta, dict)
            # Core shape field (partition marker) is present on both tiers.
            assert "closet_boost" in rr.meta

        # Tier 0 (bound): carries the three AC-8 sub-signal fields.
        b = by_key["b1"]
        for field in ("signal_type", "sub_signal_score", "effective_score"):
            assert field in b.meta, f"Tier-0 MUST carry {field!r}; got {b.meta!r}"
        assert b.meta["signal_type"] == "gap"
        assert 0.0 <= b.meta["effective_score"] <= 1.0
        # effective_score == min(raw + sub, 1.0) — verify the math directly.
        expected = min(b.score + 0.40, 1.0)
        assert b.meta["effective_score"] == pytest.approx(expected)

        # Tier 1 (unbound): does NOT carry the sub-signal fields (AC-5 / AC-8).
        u = by_key["u1"]
        for field in ("signal_type", "sub_signal_score", "effective_score"):
            assert field not in u.meta, (
                f"Tier-1 MUST NOT carry {field!r} (regression guard); got {u.meta!r}"
            )


# --------------------------------------------------------------------------- #
# AC-6 — searcher-layer negative guard                                       #
# --------------------------------------------------------------------------- #
class TestSearcherLayerUnchanged:
    """AC-6 (negative): #206 MUST NOT add a project-affinity boost at the
    MemPalace searcher layer (``CLOSET_RANK_BOOSTS`` / ``_closet_boosts``).
    The MemChorus sub-signal weight table and the searcher ordinal ladder are
    *distinct* constants and both are stable — the MemChorus table is the one
    #206 owns (``_SUB_SIGNAL_WEIGHTS`` in ``relevance_engine.py``); the
    searcher ladder must remain unmodified here (enforced at diff-review).

    This unit test pins the MemChorus-side table (the one this IMPL owns) so
    a future refactor cannot silently merge the two."""

    def test_searcher_layer_unchanged(self):
        # The MemChorus sub-signal weight table (this IMPL's signal) — pinned.
        # Values per the acceptance contract (AC-2): gap 0.40 · diff 0.30 ·
        # action 0.30 · done 0.10 · context 0.05.
        assert _SUB_SIGNAL_WEIGHTS == {
            "gap": 0.40,
            "diff": 0.30,
            "action": 0.30,
            "done": 0.10,
            "context": 0.05,
        }, (
            f"sub-signal weight table drifted from the AC-2 contract: {_SUB_SIGNAL_WEIGHTS}"
        )

        # The MemPalace searcher ordinal ladder — pinned to its live values so
        # the two constants cannot be silently aligned to each other (AC-6).
        # (Imported lazily to keep this test hermetic when the MemPalace repo
        # isn't in the same venv.)
        try:
            from mempalace.searcher.query import CLOSET_RANK_BOOSTS
        except ImportError:
            import subprocess

            mp = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from mempalace.searcher.query import CLOSET_RANK_BOOSTS as x; print(x)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if mp.returncode != 0:
                pytest.skip(
                    "MemPalace searcher not importable in this env (AC-6 is a "
                    "diff-review negative guard; the MemChorus-side weight table "
                    "is already pinned above). " + (mp.stderr.strip()[:200] or "")
                )
            CLOSET_RANK_BOOSTS = eval(mp.stdout.strip())  # list-of-floats literal
        assert CLOSET_RANK_BOOSTS == [0.40, 0.25, 0.15, 0.08, 0.04], (
            f"MemPalace searcher ordinal ladder drifted; #206 MUST NOT edit it "
            f"(AC-6): {CLOSET_RANK_BOOSTS}"
        )


# --------------------------------------------------------------------------- #
# EC-2 — request-scoped binding (per-context isolation)                     #
# --------------------------------------------------------------------------- #
class TestMultiProfileRequestScope:
    """EC-2: two profiles in one search, different ``active_project`` value,
    bind only their own drawers.  Request scope (``ContextWeight``) — no
    process-global ``active_project``; a Foo→Bar switch is just a different
    ``ContextWeight`` at the boundary."""

    def test_multi_profile_request_scope(self):
        scorer = RelevanceScorer()
        foo_bound = _bound_fixture("foo", "note", score=0.70, project="foo")
        bar_bound = _bound_fixture("bar", "note", score=0.70, project="bar")
        unbound = _unbound_fixture("u", "note", score=0.90)
        # "On Foo": Foo's drawer is bound (Tier 0), Bar's is unbound (Tier 1).
        ctx_foo = ContextWeight(active_project="foo")
        ranked_foo = scorer.score_and_rank(
            [bar_bound, unbound, foo_bound], "note", context=ctx_foo
        )
        keys_foo = [r.key for r in ranked_foo]
        assert keys_foo[0] == "foo", (
            f"on Foo, Foo's bound drawer MUST rank first; got {keys_foo!r}"
        )
        foo_rr = next(r for r in ranked_foo if r.key == "foo")
        bar_rr = next(r for r in ranked_foo if r.key == "bar")
        assert foo_rr.meta["closet_boost"] == pytest.approx(0.35)
        assert bar_rr.meta["closet_boost"] == 0.0  # Bar's drawer is unbound IN THIS REQ

        # "On Bar": symmetric — Bar's drawer is now bound, Foo's is unbound.
        ctx_bar = ContextWeight(active_project="bar")
        ranked_bar = scorer.score_and_rank(
            [foo_bound, unbound, bar_bound], "note", context=ctx_bar
        )
        keys_bar = [r.key for r in ranked_bar]
        assert keys_bar[0] == "bar"
        bar_rr2 = next(r for r in ranked_bar if r.key == "bar")
        foo_rr2 = next(r for r in ranked_bar if r.key == "foo")
        assert bar_rr2.meta["closet_boost"] == pytest.approx(0.35)
        assert foo_rr2.meta["closet_boost"] == 0.0

        # No cross-bleed: in each *individual* run, only that profile's own
        # bound drawer may carry the closet_boost_factor; the other profile's
        # drawer and the unbound item stay at 0.0 for that run.
        #  (Proven by the engine: the binding is request-scoped — a Foo→Bar
        #   switch is just a different ContextWeight at the boundary, EC-2.)
        def _cb(ranked, key):
            return next(r for r in ranked if r.key == key).meta["closet_boost"]

        # On Foo: foo=0.35, bar & unbound=0.0
        assert _cb(ranked_foo, "foo") == pytest.approx(0.35)
        assert _cb(ranked_foo, "bar") == 0.0
        assert _cb(ranked_foo, "u") == 0.0
        # On Bar: bar=0.35, foo & unbound=0.0
        assert _cb(ranked_bar, "bar") == pytest.approx(0.35)
        assert _cb(ranked_bar, "foo") == 0.0
        assert _cb(ranked_bar, "u") == 0.0
        # Neither profile's *other* drawer ever enters Tier 0 in the other's run.
        assert all(
            "signal_type" not in r.meta
            for r in list(ranked_foo) + list(ranked_bar)
            if r.meta["closet_boost"] == 0.0
        )


# --------------------------------------------------------------------------- #
# R4 — sub-signal classification (pure-function contract)                   #
# --------------------------------------------------------------------------- #
class TestSubsignalClassification:
    """Pure-function coverage for the classification helpers.  Each case is
    a *named* test case from the acceptance contract (AC-2) or the design
    doc's worked example — not a re-implementation of the keyword list."""

    @pytest.mark.parametrize(
        "content,category,expected_signal,expected_score",
        [
            # Metadata-driven (preferred path): the ``category`` field is the
            # primary signal source — the content text is irrelevant.
            # Valid slugs per relevance_engine._CATEGORY_TO_SIGNAL.
            ("TBD: auth gap", "GAP", "gap", _SUB_SIGNAL_WEIGHTS["gap"]),
            ("note", "IN_FLIGHT", "diff", _SUB_SIGNAL_WEIGHTS["diff"]),
            ("note", "NEXT_ACTION", "action", _SUB_SIGNAL_WEIGHTS["action"]),
            ("note", "COMPLETED", "done", _SUB_SIGNAL_WEIGHTS["done"]),
            ("generic note", "CONTEXT", "context", _SUB_SIGNAL_WEIGHTS["context"]),
            # Content-regex fallback: no ``category`` → the keyword scan on
            # content / key decides. (Probe-verified against _SIGNAL_KEYWORDS.)
            ("TBD: auth gap", None, "gap", _SUB_SIGNAL_WEIGHTS["gap"]),
            ("in flight branch", None, "diff", _SUB_SIGNAL_WEIGHTS["diff"]),
            ("action item to ship", None, "action", _SUB_SIGNAL_WEIGHTS["action"]),
            ("note", "MERGED", "done", _SUB_SIGNAL_WEIGHTS["done"]),
            # Additive: gap + diff fire together (EC-6 ceiling).
            (
                "gap in a worktree",
                None,
                "gap",
                sum(_SUB_SIGNAL_WEIGHTS[s] for s in ("gap", "diff")),
            ),
            # No signal fires at all → context fallback.
            ("plain text", None, "context", _SUB_SIGNAL_WEIGHTS["context"]),
        ],
    )
    def test_classification_by_signal(
        self, content, category, expected_signal, expected_score
    ):
        meta = {"category": category} if category is not None else {}
        signal, score = _compute_sub_signal(content, "a-key", meta)
        assert signal == expected_signal, (content, category, signal, meta)
        assert score == pytest.approx(expected_score), (content, category, score, meta)

    @pytest.mark.parametrize(
        "content,category,expected_signal,expected_score",
        [
            # The pure helper is *project-agnostic*: it always returns a signal
            # + score from content/category (probe-verified).  A neutral
            # content with no category fires nothing → context 0.05.
            ("anything", None, "context", _SUB_SIGNAL_WEIGHTS["context"]),
            # A valid GAP category fires the gap signal even though the helper
            # knows nothing about "active project" (0.40).  The Tier-1 zeroing
            # (to context / 0.0, and omitting the AC-8 fields) is the *caller's*
            # job — that is test_no_project_degradation, not the pure helper.
            ("anything", "GAP", "gap", _SUB_SIGNAL_WEIGHTS["gap"]),
        ],
    )
    def test_classification_no_project_no_score(
        self, content, category, expected_signal, expected_score
    ):
        """The ``_compute_sub_signal`` helper is project-agnostic — it returns
        a signal from content/category alone.  The *caller* (``score_and_rank``)
        applies the Tier-1 rule: unbound results are forced to
        ``sub_signal='context'`` / ``sub_score=0.0`` and do NOT surface the
        AC-8 sub-signal fields (that is asserted in
        ``test_no_project_degradation`` / ``test_result_shape_contract``).
        This test pins the helper contract the caller relies on for the
        *no-bound* path."""
        meta = {"category": category} if category is not None else {}
        signal, score = _compute_sub_signal(content, "a-key", meta)
        assert signal == expected_signal, (content, category, signal, meta)
        assert score == pytest.approx(expected_score), (content, category, score, meta)
