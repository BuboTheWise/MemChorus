# -*- coding: utf-8 -*-
"""IMPL #209 (a1) — hooks-level (a1) acceptance test.

AC-S3 "hooks wire-up" coverage:

  * on_session_start invokes the working-state seeder with seed=True so a fresh
    session in a real project seeds exactly ONE drawer.
  * on_session_end invokes the seeder with seed=False (upsert mode) through
    the same drawer — the single-drawer invariant.
  * The hook is a silent no-op when there is no orchestrator, no mempalace
    source, or no resolvable slug. A seeder exception is swallowed.

Unit-level seeder behaviour is covered by tests/test_working_state_seeder.py.
This file proves the *wiring* — that the hooks actually reach the seeder at
the correct call sites with the correct seed-mode polarity.
"""
from __future__ import annotations


import pytest

from memchorus import hooks as hooks_mod
from memchorus import working_state_seeder as ws
from memchorus.hooks import MemChorusHooks


class _FakeSource:
    def __init__(self) -> None:
        self.saves = []
        self.store = {}

    def save(self, key, value):
        self.saves.append({"key": key, "value": value})
        self.store[key] = value
        return True

    def retrieve(self, key):
        return self.store.get(key)


class _FakeOrchestrator:
    def __init__(self, source):
        self.memory_sources = {"mempalace": source}

    def resolve_project_record(self, name):
        return {
            "location": {"canonical_root": "/tmp/proj", "source": "ssot:test"},
            "standard": {"skill": "test", "doc_path": "x.md", "gist": "", "topics": []},
            "reconciled": [],
        }

    def search(self, *a, **kw):
        return [{"score": 0.9, "content": "orient-marker", "key": "project:memchorus"}]


@pytest.fixture(autouse=True)
def _reset_seeder_state():
    ws.clear_session_cache()
    yield
    ws.clear_session_cache()


def _noop_bootstrap(monkeypatch):
    monkeypatch.setattr(hooks_mod, "_trigger_memchorus_bootstrap", lambda: None)


def _install_fake_orch(monkeypatch, source):
    fake = _FakeOrchestrator(source)
    monkeypatch.setattr(hooks_mod, "_get_orchestrator", lambda: fake)
    return fake


class TestOnSessionStartSeeds:
    def test_seed_true_saves_exactly_once(self, monkeypatch):
        from memchorus import orientation as orient
        src = _FakeSource()
        _install_fake_orch(monkeypatch, src)
        orig = orient._resolve_project
        monkeypatch.setattr(orient, "_resolve_project", lambda *a, **kw: "memchorus")
        monkeypatch.setattr(orient, "orientation_search", lambda *a, **kw: [])
        try:
            h = MemChorusHooks()
            h.on_session_start()
            assert src.saves, "on_session_start did not seed (no save)"
            assert len(src.saves) == 1
            # The composed snapshot carries the routing *category* token,
            # WORKING_STATE, which the source's _DEFAULT_WING_MAP /
            # _DEFAULT_ROOM_MAP consume to resolve the actual
            # memchorus_workspace / working-state wing:room.  (The room
            # name itself is NOT the category — see test_category_is_working_state
            # and TestRoutingMaps, which lock the category→map contract.)
            assert src.saves[0]["value"].get("category") == "WORKING_STATE"
            n_after_first = len(src.saves)
            h.on_session_start()  # same unchanged state
            assert len(src.saves) == n_after_first, (
                "second on_session_start for same state must not re-save "
                f"({n_after_first} -> {len(src.saves)})"
            )
        finally:
            monkeypatch.setattr(orient, "_resolve_project", orig)

    def test_seed_true_noop_when_no_source(self, monkeypatch):
        from memchorus import orientation as orient
        src = _FakeSource()
        _install_fake_orch(monkeypatch, None)
        orig = orient._resolve_project
        monkeypatch.setattr(orient, "_resolve_project", lambda *a, **kw: "memchorus")
        monkeypatch.setattr(orient, "orientation_search", lambda *a, **kw: [])
        try:
            h = MemChorusHooks()
            h.on_session_start()
        finally:
            monkeypatch.setattr(orient, "_resolve_project", orig)
        assert src.saves == [], "no source -> no save"

    def test_seed_true_noop_when_no_slug(self, monkeypatch):
        from memchorus import orientation as orient
        src = _FakeSource()
        _install_fake_orch(monkeypatch, src)
        orig = orient._resolve_project
        monkeypatch.setattr(orient, "_resolve_project", lambda *a, **kw: None)
        monkeypatch.setattr(orient, "orientation_search", lambda *a, **kw: [])
        try:
            h = MemChorusHooks()
            h.on_session_start()
        finally:
            monkeypatch.setattr(orient, "_resolve_project", orig)
        assert src.saves == [], "no slug -> no save"


class TestOnSessionEndUpserts:
    def test_seed_false_saves(self, monkeypatch):
        from memchorus import orientation as orient
        src = _FakeSource()
        _install_fake_orch(monkeypatch, src)
        _noop_bootstrap(monkeypatch)
        h = MemChorusHooks()
        orig = orient._resolve_project
        monkeypatch.setattr(orient, "_resolve_project", lambda *a, **kw: "memchorus")
        try:
            h.on_session_end()
        finally:
            monkeypatch.setattr(orient, "_resolve_project", orig)
        assert src.saves, "on_session_end did not call source.save() with a valid slug"
        for saved in src.saves:
            assert "category" in saved["value"], "save payload missing routing field"

    def test_noop_when_no_orchestrator(self, monkeypatch):
        monkeypatch.setattr(hooks_mod, "_get_orchestrator", lambda: None)
        h = MemChorusHooks()
        h.on_session_end()  # must not raise

    def test_noop_when_no_slug(self, monkeypatch):
        from memchorus import orientation as orient
        src = _FakeSource()
        _install_fake_orch(monkeypatch, src)
        _noop_bootstrap(monkeypatch)
        h = MemChorusHooks()
        orig = orient._resolve_project
        monkeypatch.setattr(orient, "_resolve_project", lambda *a, **kw: None)
        try:
            h.on_session_end()
        finally:
            monkeypatch.setattr(orient, "_resolve_project", orig)
        assert src.saves == [], "no slug -> no save"


class TestSeederWiringNeverRaises:
    def test_seeder_exception_is_swallowed(self, monkeypatch):
        from memchorus import orientation as orient
        src = _FakeSource()
        _install_fake_orch(monkeypatch, src)
        _noop_bootstrap(monkeypatch)
        h = MemChorusHooks()
        orig = orient._resolve_project
        monkeypatch.setattr(orient, "_resolve_project", lambda *a, **kw: "memchorus")
        monkeypatch.setattr(orient, "orientation_search", lambda *a, **kw: [])
        try:
            def _boom(self, seed=None, **kw):
                raise RuntimeError("mcp down")
            monkeypatch.setattr(ws.WorkingStateSeeder, "ensure", _boom)
            h.on_session_start()  # must not raise
            h.on_session_end()    # must not raise
        finally:
            monkeypatch.setattr(orient, "_resolve_project", orig)
