"""(#209 working-state surface (b)) end-to-end hook injection tests.

These exercise the REAL ``MemChorusHooks.on_pre_llm_call`` path with a fake
orchestrator, proving the corpus-imbalance note lands INSIDE the
``[MemChorus Memory Recall]`` block (AC-S2) and that the diagnostic degrades
silently when disabled or when the census cannot be read (recall is never
harmed).

The balancer's default resolvers read from ``memchorus.get_orchestrator()`` —
the same fake orchestrator the hooks path patches — so a single
``memory_sources["mempalace"] = FakeSource`` wiring serves both the hook flow
and the balancer census.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from memchorus import hooks as _hooks


@pytest.fixture(autouse=True)
def _reset_suppression():
    """Hermetic cross-turn suppression state per test.

    The hooks module keeps a process-wide (per-profile) LRU+TTL window of
    recently-injected (key, content_hash) pairs used to collapse a re-render
    into a "↳ (shown earlier)" marker. That window is global to the worker,
    so a key that a *previous* test rendered can be seen as "already shown"
    here even though this test rendered it first. Reset it before each test
    so recall items always render in full.
    """
    _hooks._clear_suppression_windows()
    try:
        yield
    finally:
        _hooks._clear_suppression_windows()


# --------------------------------------------------------------------------- #
# Fake census source (mirrors the unit-test fake: wing/room/total lookup)      #
# --------------------------------------------------------------------------- #

class _CensusSource:
    def __init__(self, total: int, wing_counts=None, room_counts=None) -> None:
        self.total = total
        self.wing_counts = dict(wing_counts or {})
        self.room_counts = dict(room_counts or {})

    def call_tool(self, name, arguments=None):
        assert name == "mempalace_list_drawers", name
        arguments = arguments or {}
        if "wing" in arguments:
            n = int(self.wing_counts.get(arguments["wing"], 0))
        elif "room" in arguments:
            n = int(self.room_counts.get(arguments["room"], 0))
        else:
            n = int(self.total)
        return {"total": n, "count": min(1, n), "drawers": []}


def _make_orch(
    balance_cfg: Optional[dict],
    census: Optional[object] = None,
    search_items=None,
) -> MagicMock:
    orch = MagicMock()
    orch.config = {"balance": balance_cfg or {}}
    orch.source = census
    orch.memory_sources = (
        {"mempalace": census} if census is not None else {}
    )
    orch.search.return_value = list(search_items or [])
    if search_items is None:
        orch.search.return_value = [
            {"key": "learned-thing", "content": "a recalled lesson"}
        ]
    # start with no cached balancer so the hook creates one
    orch._corpus_balancer = None  # type: ignore[attr-defined]
    return orch


def _run_hook(orch: MagicMock, **kwargs):
    from memchorus.hooks import MemChorusHooks

    # Both entry points in the flow resolve through these two symbols:
    #   - hooks._get_orchestrator (on_pre_llm_call + _try_feedback_loop)
    #   - memchorus.get_orchestrator (balancer default resolvers)
    with patch("memchorus.hooks._get_orchestrator", return_value=orch), \
         patch("memchorus.get_orchestrator", return_value=orch):
        hooks = MemChorusHooks()
        return hooks.on_pre_llm_call(**kwargs)


# --------------------------------------------------------------------------- #
# AC-S2 — note inside the recall block                                        #
# --------------------------------------------------------------------------- #

def test_balance_note_injected_inside_recall_block_when_warn():
    # M1 thin (10/578), M2 high (470/578) → WARN → note present, inside the
    # recall block, and the recalled lesson still appears after it.
    census = _CensusSource(
        total=578,
        wing_counts={"memchorus_general": 400, "memchorus_learning": 70},
        room_counts={"working-state": 10, "current-status": 0, "tasks": 0},
    )
    orch = _make_orch({"enabled": True}, census,
                      [{"key": "lesson", "content": "retriable: check git"}])
    result = _run_hook(orch, user_message="please plan the next steps")

    assert result is not None
    ctx = result["context"]
    open_tag = ctx.index("[MemChorus Memory Recall]")
    close_tag = ctx.index("[/MemChorus Memory Recall]")
    # The balance note must sit BETWEEN the tags, not outside them.
    inner = ctx[open_tag + len("[MemChorus Memory Recall]"):close_tag]
    assert "balance WARN" in inner
    # The recalled lesson is still present inside the same block.
    assert "retriable: check git" in inner
    # Order: the nudge precedes the soft-recall items (nudge first).
    assert inner.index("balance WARN") < inner.index("retriable: check git")


def test_balance_note_injected_inside_recall_block_when_crit():
    census = _CensusSource(
        total=216,
        wing_counts={"memchorus_general": 200, "memchorus_learning": 12},
        room_counts={"working-state": 0, "current-status": 0, "tasks": 0},
    )
    orch = _make_orch({"enabled": True}, census)
    # Even with zero soft-recall items, a CRIT must still emit the block so
    # the nudge is never lost on an all-lessons profile.
    result = _run_hook(orch, user_message="summarise the week")
    assert result is not None
    ctx = result["context"]
    assert "CRIT" in ctx
    assert "[MemChorus Memory Recall]" in ctx
    assert "balance CRIT" in ctx


def test_balance_ok_produces_no_note_but_recall_still_delivered():
    # healthy corpus (M1 60% present) → OK → no balance line, but the recall
    # block still delivers the lesson.
    census = _CensusSource(
        total=100,
        wing_counts={"memchorus_general": 20, "memchorus_project": 60},
        room_counts={"working-state": 30, "tasks": 10, "current-status": 20},
    )
    orch = _make_orch(
        {"enabled": True}, census,
        [{"key": "note", "content": "remember to sync"}],
    )
    result = _run_hook(orch, user_message="what should I do next")
    assert result is not None
    ctx = result["context"]
    assert "remember to sync" in ctx
    assert "balance WARN" not in ctx
    assert "balance CRIT" not in ctx


# --------------------------------------------------------------------------- #
# Gate / degradation                                                         #
# --------------------------------------------------------------------------- #

def test_balance_disabled_produces_no_note():
    census = _CensusSource(
        total=216,
        wing_counts={"memchorus_general": 200},
        room_counts={"working-state": 0, "current-status": 0, "tasks": 0},
    )
    orch = _make_orch({"enabled": False}, census,
                      [{"key": "x", "content": "a lesson"}])
    result = _run_hook(orch, user_message="plan it")
    # Recall still delivered, but no balance note because opt-in is OFF.
    assert result is not None
    assert "balance WARN" not in result["context"]
    assert "balance CRIT" not in result["context"]
    # The balancer should never have attached a report to the orchestrator
    # when disabled (short-circuits before compute()).
    assert getattr(orch, "_corpus_balancer", None) is None


def test_balance_enabled_missing_in_config_defaults_to_off():
    census = _CensusSource(
        total=216,
        wing_counts={"memchorus_general": 200},
        room_counts={"working-state": 0, "tasks": 0, "current-status": 0},
    )
    orch = _make_orch({}, census, [{"key": "x", "content": "a lesson"}])
    result = _run_hook(orch, user_message="go")
    assert result is not None
    assert "balance" not in result["context"]


def test_balance_census_error_degrades_silently():
    # A source whose call_tool raises → partial census → no confident level →
    # no note, but the hook still returns a clean recall block (never None
    # because soft items exist).
    class _Broken:
        def call_tool(self, name, arguments=None):
            raise ConnectionError("MCP down")

    orch = _make_orch({"enabled": True}, _Broken(),
                      [{"key": "x", "content": "a lesson"}])
    result = _run_hook(orch, user_message="go")
    assert result is not None
    ctx = result["context"]
    assert "balance WARN" not in ctx
    assert "balance CRIT" not in ctx
    # The lesson is still delivered.
    assert "a lesson" in ctx


def test_balance_reuses_session_scoped_balancer_across_calls():
    census = _CensusSource(
        total=216,
        wing_counts={"memchorus_general": 200},
        room_counts={"working-state": 0, "tasks": 0, "current-status": 0},
    )
    orch = _make_orch({"enabled": True}, census)

    from memchorus.hooks import MemChorusHooks

    with patch("memchorus.hooks._get_orchestrator", return_value=orch), \
         patch("memchorus.get_orchestrator", return_value=orch):
        hooks = MemChorusHooks()
        hooks.on_pre_llm_call(user_message="first call")
        first = getattr(orch, "_corpus_balancer", None)
        assert first is not None
        second_call = hooks.on_pre_llm_call(user_message="second call")
        # Same balancer instance is reused (shares the TTL cache).
        assert getattr(orch, "_corpus_balancer", None) is first
        assert second_call is not None
