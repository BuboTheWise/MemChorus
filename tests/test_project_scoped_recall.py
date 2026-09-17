"""Tests for #206 (project-scoped recall ranking) + #209 (imbalance diagnostic).

These cover the three acceptance criteria from the issue:

  AC1 — when a query returns results from multiple projects, the *active*
        project's drawers are partitioned to the TOP of the ranked list,
        ordered by similarity within each partition.
  AC2 — the per-result ``closet_boost`` field reflects the actual value
        (``ContextWeight.closet_boost_factor``, default 0.35) on bound
        results and ``0.0`` on unbound ones.
  AC3 — a visible diagnostic (the ``--status`` flag in ``memchorus-doctor``)
        reports the active project, the closet counters, and a wing census.

Plus:
  AC4 — outside a project context (no active_project) the cloak is a no-op:
        all results are unbound, all ``closet_boost`` are 0.0, and the
        ordering is pure similarity (i.e. pre-#206 behaviour).

OPSEC: no ``/home/<user>`` fixture paths — everything lives under ``/tmp``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

# Import the modules under test.  The relevance engine is pure (no I/O), so we
# can exercise the whole scoring path in-process.
from memchorus.relevance_engine import (
    ContextWeight,
    RelevanceScorer,
    closet_bound_result,
    reset_closet_counters,
    _closet_stats,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_counters():
    """Zero the module-level closet counters before AND after each test so
    state does not leak between tests (the counters are process-global)."""
    reset_closet_counters()
    yield
    reset_closet_counters()


def _result(key: str, content: str, **extra: Any) -> Dict[str, Any]:
    """A minimal scored search hit.  ``similarity`` drives the quality dimension."""
    r: Dict[str, Any] = {
        "key": key,
        "content": content,
        "source": "mempalace",
        "similarity": 0.5,
        "created_at": "2026-01-01T00:00:00Z",
    }
    r.update(extra)
    return r


# ---------------------------------------------------------------------------
# #206 AC2 — the cloak value is surfaced per-result and correct
# ---------------------------------------------------------------------------

class TestClosetValue:
    def test_bound_result_gets_configured_boost(self):
        ctx = ContextWeight(active_project="acme", closet_boost_factor=0.35)
        scorer = RelevanceScorer()
        r = _result("acme/notes", "acme deployment plan", wing="acme")
        bd = scorer.score_breakdown(r, "acme plan", ctx)
        assert bd["closet_boost"] == pytest.approx(0.35)
        # The value is also in meta (carried into RankedResult by score_and_rank).

    def test_unbound_result_has_zero_boost(self):
        ctx = ContextWeight(active_project="acme", closet_boost_factor=0.35)
        scorer = RelevanceScorer()
        r = _result("zeta/notes", "zeta rollout note", wing="zeta")
        bd = scorer.score_breakdown(r, "rollout note", ctx)
        assert bd["closet_boost"] == 0.0

    def test_no_active_project_is_always_zero(self):
        # AC4: outside a project context every result is unbound.
        ctx = ContextWeight()
        assert ctx.active_project is None
        scorer = RelevanceScorer()
        for r in [
            _result("acme/notes", "acme plan", wing="acme"),
            _result("zeta/notes", "zeta note", wing="zeta"),
        ]:
            bd = scorer.score_breakdown(r, "plan", ctx)
            assert bd["closet_boost"] == 0.0

    def test_boost_value_matches_factor_even_when_not_default(self):
        ctx = ContextWeight(active_project="acme", closet_boost_factor=0.20)
        scorer = RelevanceScorer()
        r = _result("acme/notes", "acme plan", wing="acme")
        bd = scorer.score_breakdown(r, "acme plan", ctx)
        assert bd["closet_boost"] == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# #206 binding predicate
# ---------------------------------------------------------------------------

class TestBindingPredicate:
    def test_bound_by_wing(self):
        assert closet_bound_result({"wing": "acme"}, "acme") is True

    def test_bound_by_key_prefix(self):
        assert closet_bound_result({"key": "project:acme"}, "acme") is True

    def test_bound_by_compound_key_path(self):
        # The live MemPalace path keys entries as "wing/room".
        assert closet_bound_result({"key": "acme/deployment"}, "acme") is True

    def test_case_insensitive(self):
        assert closet_bound_result({"wing": "ACME"}, "acme") is True
        assert closet_bound_result({"key": "Acme/deploy"}, "acme") is True

    def test_unbound_different_project(self):
        assert closet_bound_result({"wing": "zeta"}, "acme") is False
        assert closet_bound_result({"key": "project:zeta"}, "acme") is False

    def test_no_active_project_is_never_bound(self):
        assert closet_bound_result({"wing": "acme"}, None) is False
        assert closet_bound_result({"wing": "acme"}, "") is False

    def test_non_dict_result_is_unbound(self):
        assert closet_bound_result("not a dict", "acme") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# #206 AC1 — active project's drawers are partitioned to the TOP
# ---------------------------------------------------------------------------

class TestPartitioning:
    def test_bound_results_before_unbound(self):
        """A lower-similarity hit bound to the active project must outrank a
        higher-similarity unbound hit — this is the whole point of #206."""
        ctx = ContextWeight(active_project="acme", closet_boost_factor=0.35)
        scorer = RelevanceScorer()
        # 'acme/...' has LOWER raw similarity (0.30) but is bound.
        acme = _result("acme/notes", "acme plan", wing="acme", similarity=0.30)
        # 'zeta/...' has HIGHER raw similarity (0.60) but is unbound.
        zeta = _result("zeta/notes", "zeta plan", wing="zeta", similarity=0.60)
        ranked = scorer.score_and_rank([zeta, acme], "plan", ctx)
        assert len(ranked) == 2
        keys = [r.key for r in ranked]
        # The bound (acme) result is FIRST despite its lower similarity.
        assert keys[0] == "acme/notes"
        assert keys[1] == "zeta/notes"

    def test_within_partition_still_by_similarity(self):
        """Among bound results, ordering is still by score (descending)."""
        ctx = ContextWeight(active_project="acme", closet_boost_factor=0.35)
        scorer = RelevanceScorer()
        # Bound 'low' = lower text-overlap with the query.
        low = _result("acme/low", "acme", wing="acme")
        # Bound 'high' = much higher text-overlap with the query.
        high = _result("acme/high", "acme deployment plan notes", wing="acme")
        # Unbound 'zeta' = highest overlap, but in the *other* bucket.
        unbound = _result("zeta/notes", "acme deployment plan notes", wing="zeta")
        ranked = scorer.score_and_rank([unbound, low, high], "acme deployment plan", ctx)
        keys = [r.key for r in ranked]
        # Two bound (acme/high, acme/low) first, in score/overlap order, then unbound.
        assert keys[:2] == ["acme/high", "acme/low"]
        assert "zeta/notes" in keys

    def test_no_active_project_is_pure_similarity(self):
        """AC4: without an active project the ordering collapses to plain
        score (pre-#206 behaviour) and all boosts are 0."""
        ctx = ContextWeight()
        scorer = RelevanceScorer()
        acme = _result("acme/notes", "acme", wing="acme")           # lower overlap
        zeta = _result("zeta/notes", "acme deployment plan notes",    # higher overlap
                       wing="zeta")
        ranked = scorer.score_and_rank([acme, zeta], "acme deployment plan", ctx)
        keys = [r.key for r in ranked]
        assert keys == ["zeta/notes", "acme/notes"]  # highest score first
        for r in ranked:
            assert r.meta.get("closet_boost") == 0.0

    def test_closet_boost_field_present_on_ranked_results(self):
        ctx = ContextWeight(active_project="acme", closet_boost_factor=0.35)
        scorer = RelevanceScorer()
        acme = _result("acme/notes", "acme plan", wing="acme")
        zeta = _result("zeta/notes", "zeta plan", wing="zeta")
        ranked = scorer.score_and_rank([acme, zeta], "plan", ctx)
        by_key = {r.key: r for r in ranked}
        assert by_key["acme/notes"].meta.get("closet_boost") == pytest.approx(0.35)
        assert by_key["zeta/notes"].meta.get("closet_boost") == 0.0
        # The score-breakdown (attached by score_and_rank) also carries it.
        assert by_key["acme/notes"].meta["score_breakdown"]["closet_boost"] == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# #209 AC3 — counters feed the diagnostic
# ---------------------------------------------------------------------------

class TestClosetCounters:
    def test_query_with_active_project_is_counted(self):
        ctx = ContextWeight(active_project="acme")
        scorer = RelevanceScorer()
        scorer.score_and_rank([_result("acme/x", "acme", wing="acme")], "acme", ctx)
        stats = _closet_stats()
        assert stats["queries_seen"] == 1
        assert stats["queries_with_active_project"] == 1
        assert stats["bound_results_surfaced"] == 1

    def test_query_without_active_project_not_counted_as_active(self):
        ctx = ContextWeight()
        scorer = RelevanceScorer()
        scorer.score_and_rank([_result("acme/x", "acme", wing="acme")], "acme", ctx)
        stats = _closet_stats()
        assert stats["queries_seen"] == 1
        assert stats["queries_with_active_project"] == 0
        assert stats["bound_results_surfaced"] == 0

    def test_multiple_queries_accumulate(self):
        ctx = ContextWeight(active_project="acme")
        scorer = RelevanceScorer()
        scorer.score_and_rank([_result("acme/a", "a", wing="acme")], "a", ctx)
        scorer.score_and_rank([_result("acme/b", "b", wing="acme")], "b", ctx)
        stats = _closet_stats()
        assert stats["queries_seen"] == 2
        assert stats["queries_with_active_project"] == 2
        assert stats["bound_results_surfaced"] == 2


# ---------------------------------------------------------------------------
# #209 — status_report composes resolver + census + counters (unit level)
# ---------------------------------------------------------------------------

class TestStatusReport:
    def test_status_report_shape_and_no_raise(self):
        """The source's status_report must not raise and must expose the
        documented shape, even when the cache dir is absent (fresh tmp)."""
        import tempfile

        from memchorus.mempalace_memory_source import MemPalaceMemorySource

        with tempfile.TemporaryDirectory() as tmp:
            src = MemPalaceMemorySource(config={"skip_mcp": True, "cache_dir": tmp})
            assert hasattr(src, "status_report")
            report = src.status_report(active_project="acme")
            assert report["active_project"] == "acme"
            assert "closet" in report
            assert "top_wings" in report
            assert "total_drawers" in report
            assert report["status"] in ("ok", "partial", "no_orchestrator")

    def test_status_report_counts_drawers_in_cache(self):
        import tempfile

        from memchorus.mempalace_memory_source import MemPalaceMemorySource

        with tempfile.TemporaryDirectory() as tmp:
            import os as _os
            # Plant a "wing" with two nested drawers, another with one.
            for wing_path, n in (
                (_os.path.join(tmp, "aletheia", "notes"), 2),
                (_os.path.join(tmp, "zeta", "deploy"), 1),
            ):
                _os.makedirs(wing_path, exist_ok=True)
                for i in range(n):
                    with open(_os.path.join(wing_path, f"d{i}.json"), "w") as fh:
                        fh.write("{}")
            src = MemPalaceMemorySource(config={"skip_mcp": True, "cache_dir": tmp})
            report = src.status_report(active_project="aletheia")
            top = {w["wing"]: w["drawers"] for w in report["top_wings"]}
            assert top["aletheia"] == 2
            assert top["zeta"] == 1
            assert report["total_drawers"] == 3
            assert report["status"] == "ok"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
