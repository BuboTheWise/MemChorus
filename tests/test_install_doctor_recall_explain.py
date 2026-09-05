"""Acceptance tests for the ``memchorus-doctor --recall`` explainability surface
(IMPL #173 / Kanban t_8900e358).

The feature wires the *live* recall pipeline into the doctor CLI so an operator
can ask "why did recall inject (or drop) this?" and get a per-candidate score
explainability report plus a read-only render simulation that is byte-identical
to what the live agent would inject.

Locked in here:

  (a) CLI surface — ``main(["--recall", "<q>"])`` dispatches to the recall
      path, exit codes (0 path-ran / 1 pipeline-failed / 2 usage error), and the
      human render;
  (b) ``--json`` stable schema — top-level keys, per-result keys, render and
      suppression sub-schemas;
  (c) byte-identical block — with ``MEMCHORUS_RECALL_SHOW_DROPPED`` unset,
      the doctor's simulated render is byte-for-byte the live injection block
      (a fresh suppression window produces identical output);
  (d) DEBUG explainability — ``memchorus.hooks`` logs a ``recall.render`` line
      per render, a ``recall.dropped_by_budget`` line when the char budget
      drops entries, and a ``recall.suppressed`` line per suppression mark.
"""

from __future__ import annotations

import json
import logging
import os
from unittest import mock

import pytest

import memchorus
import memchorus.hooks as hooks
import memchorus.install_doctor as doctor
from memchorus.install_doctor import main

_TEST_PROFILE = "recall-explain-test"


def _breakdown(quality, recency, source_prior, boost=1.0,
               auto=1.0, pen_factor=1.0, pen_matches=None,
               final=None, raw=None):
    contribs = {
        "quality": round(quality * 0.5, 6),
        "recency": round(recency * 0.3, 6),
        "source_type": round(source_prior * 0.2, 6),
    }
    contribs["total"] = round(sum(contribs.values()), 6)
    r = round(contribs["total"] * boost, 6)
    return {
        "quality": quality,
        "recency": recency,
        "source_prior": source_prior,
        "weights": {"quality": 0.5, "recency": 0.3, "source_type": 0.2},
        "contributions": contribs,
        "calibration_boost": boost,
        "auto_provenance_penalty": auto,
        "penalty": {"factor": pen_factor, "matches": pen_matches or []},
        "raw": raw if raw is not None else r,
        "final": final if final is not None else round(r * auto * pen_factor, 6),
    }


class _FakeOrchestrator:
    """Deterministic live-path stand-in: returns two ranked candidates, one of
    which carries an auto-provenance penalty + a matched penalty pattern."""

    def search(self, query, limit=10, **kwargs):
        return [
            {
                "key": "alpha-note",
                "source": "mempalace",
                "score": 0.91,
                "content": "Alpha deployment note: pinned the OTel family at 1.39.1.",
                "score_breakdown": _breakdown(0.85, 0.7, 0.5, boost=1.5, final=0.91),
            },
            {
                "key": "beta-changelog",
                "source": "mempalace",
                "score": 0.42,
                "content": "changelog v1.2.3 — version bump only",
                "score_breakdown": _breakdown(
                    0.6, 0.4, 0.3, auto=0.5, pen_factor=0.7,
                    pen_matches=[["changelog", 0.7]], final=0.42,
                ),
            },
        ]


class _RaisingOrchestrator:
    def search(self, query, limit=10, **kwargs):
        raise RuntimeError("boom: index unavailable")


@pytest.fixture(autouse=True)
def _isolated_recall_env(monkeypatch):
    """Per-test isolation: dedicated profile, clean suppression window, no env
    overrides (incl. MEMCHORUS_RECALL_SHOW_DROPPED)."""
    monkeypatch.setenv("HERMES_PROFILE", _TEST_PROFILE)
    for var in (
        "MEMCHORUS_RECALL_MAX_CHARS",
        "MEMCHORUS_SUPPRESSION_WINDOW",
        "MEMCHORUS_SUPPRESSION_TTL",
        "MEMCHORUS_RECALL_SHOW_DROPPED",
    ):
        monkeypatch.delenv(var, raising=False)
    hooks._clear_suppression_windows()
    yield
    hooks._clear_suppression_windows()


def _register(monkeypatch, orch):
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **k: orch)


# ---------------------------------------------------------------------------
# (a) CLI surface
# ---------------------------------------------------------------------------

def test_recall_cli_human_render_ok(capsys, monkeypatch):
    _register(monkeypatch, _FakeOrchestrator())
    code = main(["--recall", "deployment note", "--limit", "7"])

    assert code == 0
    out = capsys.readouterr().out
    # Header names the query + limit
    assert "deployment note" in out
    assert "limit 7" in out
    # Both candidates rendered with their real schema-derived fields
    assert "alpha-note" in out
    assert "beta-changelog" in out
    assert "contributions:" in out
    # Schema-derived penalty lines (2-tuples: label[factor])
    assert "auto_prov_x=0.500" in out
    assert "penalties matched" in out and "changelog[0.7]" in out
    # Read-only render simulation block
    assert "would be injected" in out
    # Live suppression-window snapshot
    assert f"profile={_TEST_PROFILE}" in out


def test_recall_cli_no_orchestrator_exits_1_and_prints_diagnostic(
        capsys, monkeypatch):
    _register(monkeypatch, None)
    code = main(["--recall", "anything"])
    assert code == 1
    out = capsys.readouterr().out
    assert "No MemoryOrchestrator is registered" in out


def test_recall_cli_search_error_exits_1(capsys, monkeypatch):
    _register(monkeypatch, _RaisingOrchestrator())
    code = main(["--recall", "anything"])
    assert code == 1
    out = capsys.readouterr().out
    assert "Search failed:" in out
    assert "boom: index unavailable" in out


def test_recall_cli_missing_query_is_usage_error(capsys):
    code = main(["--recall"])
    assert code == 2
    assert "--recall requires a query string" in capsys.readouterr().out


def test_recall_cli_bad_limit_is_usage_error(capsys, monkeypatch):
    _register(monkeypatch, _FakeOrchestrator())
    code = main(["--recall", "q", "--limit", "not-a-number"])
    assert code == 2
    assert "--limit must be an integer" in capsys.readouterr().out


def test_recall_cli_limit_is_propagated_to_search(monkeypatch):
    """The operator-chosen --limit must be the limit the live path sees
    (not the CLI default), so an operator can widen or narrow the report."""
    captured: dict = {}

    class _RecordingOrch:
        def search(self, query, limit=10, **kw):
            captured["limit"] = limit
            return [{"key": "k", "source": "s", "score": 0.5, "content": "c"}]

    _register(monkeypatch, _RecordingOrch())
    main(["--recall", "q", "--limit", "3"])
    # ``main`` returns the recall exit code (0 for ok); captured must be 3.
    assert captured == {"limit": 3}


# ---------------------------------------------------------------------------
# (b) --json stable schema
# ---------------------------------------------------------------------------

def test_recall_json_stable_schema(capsys, monkeypatch):
    _register(monkeypatch, _FakeOrchestrator())
    code = main(["--recall", "deployment note", "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)

    # Top-level shape is the contract — assert the exact key set.
    assert set(doc) == {
        "query", "limit", "status", "reason",
        "results", "render", "suppression",
    }
    assert doc["status"] == "ok"
    assert doc["query"] == "deployment note"

    # Per-result contract.
    for r in doc["results"]:
        assert set(r) == {
            "key", "source", "score",
            "score_breakdown", "disposition", "content_preview",
        }
        assert r["disposition"] in (
            "injected", "dropped_by_budget", "suppressed_shown_earlier"
        )
    by_key = {r["key"]: r for r in doc["results"]}
    assert set(by_key) == {"alpha-note", "beta-changelog"}

    # score_breakdown sub-schema (as emitted by the scorer — stable keys).
    bd = by_key["beta-changelog"]["score_breakdown"]
    assert set(bd) == {
        "quality", "recency", "source_prior",
        "weights", "contributions",
        "calibration_boost", "auto_provenance_penalty",
        "penalty", "raw", "final",
    }
    assert set(bd["penalty"]) == {"factor", "matches"}
    assert bd["penalty"]["matches"] == [["changelog", 0.7]]

    # Render sub-schema.
    rd = doc["render"]
    assert set(rd) == {"rendered", "injected", "dropped", "full_body_mark"}
    for item in rd["injected"]:
        assert set(item) == {"key", "score", "content", "suppressed"}
    for drop in rd["dropped"]:
        assert set(drop) == {"key", "score", "reason"}

    # Suppression sub-schema.
    sp = doc["suppression"]
    assert set(sp) == {
        "profile", "window_size", "ttl_seconds",
        "configured", "entries_total", "in_window", "expired",
    }
    assert sp["profile"] == _TEST_PROFILE


def test_recall_json_no_orchestrator_schema(capsys, monkeypatch):
    _register(monkeypatch, None)
    code = main(["--recall", "x", "--json"])
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "no_orchestrator"
    assert doc["reason"]
    assert doc["results"] == []
    assert doc["render"] is None
    assert doc["suppression"] is None


# ---------------------------------------------------------------------------
# (c) byte-identical block lock
# ---------------------------------------------------------------------------

def test_render_simulation_is_byte_identical_to_live_block(monkeypatch):
    """With the show-dropped flag unset, the doctor's read-only simulation
    reproduces the live injection block byte-for-byte (fresh window)."""
    assert os.environ.get("MEMCHORUS_RECALL_SHOW_DROPPED") is None

    items = [
        {"key": "k1", "content": "first body", "score": 0.9},
        {"key": "k2", "content": "second body", "score": 0.5},
    ]
    simulated = hooks.simulate_recall_render([dict(x) for x in items])["rendered"]
    live = hooks._format_context_block(items)
    assert simulated == live
    # Fences intact.
    assert simulated.startswith("[MemChorus injected context]\n")
    assert simulated.endswith("\n[/MemChorus injected block]")


def test_render_simulation_is_byte_identical_under_budget_drop(monkeypatch):
    """Byte-lock must also hold when the char budget drops entries: the
    simulated block (with its truncation note) equals what the live path
    injects."""
    monkeypatch.setenv("MEMCHORUS_RECALL_MAX_CHARS", "200")
    items = [
        {"key": "big-a", "content": "A" * 100, "score": 0.9},
        {"key": "big-b", "content": "B" * 100, "score": 0.8},
        {"key": "big-c", "content": "C" * 100, "score": 0.7},
    ]
    simulated = hooks.simulate_recall_render([dict(x) for x in items])
    live = hooks._format_context_block(items)

    assert simulated["rendered"] == live
    assert len(simulated["dropped"]) >= 1
    trunc_note = "truncated, budget exceeded"
    assert trunc_note in simulated["rendered"]
    assert trunc_note in live


def test_simulate_does_not_mutate_suppression_window(monkeypatch):
    """Read-only guarantee: N simulated renders still yield the full body in
    the following live render (no accidental suppression collapse)."""
    items = [{"key": "once", "content": "one two three four five", "score": 0.9}]
    for _ in range(3):
        hooks.simulate_recall_render([dict(items[0])])
    live = hooks._format_context_block([dict(items[0])])
    assert "one two three four five" in live
    assert "shown earlier" not in live


# ---------------------------------------------------------------------------
# (d) DEBUG explainability lines
# ---------------------------------------------------------------------------

def test_debug_render_line_emitted_per_render(caplog):
    with caplog.at_level(logging.DEBUG, logger="memchorus.hooks"):
        hooks._format_context_block(
            [{"key": "d1", "content": "debug body one", "score": 0.75}]
        )
    lines = [r.getMessage() for r in caplog.records]
    assert any(l.startswith("recall.render: injected=1 dropped=0") for l in lines)
    rendered = [l for l in lines if l.startswith("recall.render:")][0]
    assert "candidates=[" in rendered and "d1=0.7500" in rendered


def test_debug_budget_drop_line_names_dropped_entries(caplog, monkeypatch):
    monkeypatch.setenv("MEMCHORUS_RECALL_MAX_CHARS", "200")
    with caplog.at_level(logging.DEBUG, logger="memchorus.hooks"):
        hooks._format_context_block(
            [
                {"key": "b-a", "content": "A" * 80, "score": 0.9},
                {"key": "b-b", "content": "B" * 80, "score": 0.8},
                {"key": "b-c", "content": "C" * 80, "score": 0.7},
            ]
        )
    lines = [r.getMessage() for r in caplog.records]
    dropped = [l for l in lines if l.startswith("recall.dropped_by_budget:")]
    assert dropped, "expected a recall.dropped_by_budget DEBUG line"
    # Named, non-empty drop detail (concrete key=score pairs, not a bare prefix).
    detail = dropped[0][len("recall.dropped_by_budget:"):].strip()
    assert detail and "=" in detail
    assert any(f"{k}=" in detail for k in ("b-a", "b-b", "b-c"))


def test_debug_suppression_mark_per_collapsed_entry(caplog):
    """Each entry collapsed by the GH-141 window emits its own
    ``recall.suppressed`` DEBUG line (one per mark, not one per block)."""
    items_spec = [("s-1", "suppressed one", 0.9),
                  ("s-2", "suppressed two", 0.8),
                  ("s-3", "brand new body", 0.7)]
    items = [{"key": k, "content": c, "score": s} for k, c, s in items_spec]
    hooks._format_context_block(items)  # turn N — full bodies, marks window
    with caplog.at_level(logging.DEBUG, logger="memchorus.hooks"):
        second = hooks._format_context_block(items)  # turn N+1
    assert "shown earlier" in second
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    # Per-entry granularity: every key collapsed in turn N+1 has its own line.
    # All three entries are unchanged and the fresh window holds them, so all
    # three must collapse individually.
    expected_suppressed = [k for k, _c, _s in items_spec]
    marks = [l for l in lines if l.startswith("recall.suppressed:")]
    assert len(marks) == len(expected_suppressed)
    for key in expected_suppressed:
        assert any(f"key={key}" in m for m in marks)
    # And a matching recall.render line carries the (suppressed) flag.
    renders = [l for l in lines if l.startswith("recall.render:")]
    assert renders and "(suppressed)" in renders[0]
