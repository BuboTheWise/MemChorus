"""test_hooks_recall_loud_degrade.py - GH-#312 loud-degrade contract for recall.

Spec: spec-312-recall-degrade.md, clause 1. When the orchestrator cannot be
resolved in this interpreter, ``on_pre_llm_call`` MUST:

  (a) emit exactly ONE WARNING per process via the gateway-visible logger,
      message ``MemChorus recall disabled: orchestrator unavailable in this
      interpreter (<reason>)`` with the concrete reason;
  (b) still return None (never raise into the LLM call);
  (c) not repeat the warning on subsequent calls in the same process.

Both unavailable paths are covered: ``_get_orchestrator`` returning None, and
``_get_orchestrator``'s import-failure fallback surfacing the real import error.
"""
import logging

import pytest

WARNING_PREFIX = (
    "MemChorus recall disabled: orchestrator unavailable in this interpreter"
)


@pytest.fixture
def hooks_mod(monkeypatch):
    """Import the hooks module under test with a fresh once-per-process flag."""
    import memchorus.hooks as hooks
    monkeypatch.setattr(hooks, "_RECALL_WARNED", False)
    return hooks


def _loud_degrade_warnings(caplog) -> list:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and WARNING_PREFIX in r.getMessage()
    ]


def test_first_unavailable_call_warns_once_and_returns_none(hooks_mod, monkeypatch, caplog):
    """orchestrator None: first call warns exactly once, second stays quiet; both None."""
    monkeypatch.setattr(hooks_mod, "_get_orchestrator", lambda: None)

    with caplog.at_level(logging.WARNING, logger="memchorus.hooks"):
        first = hooks_mod.MemChorusHooks().on_pre_llm_call(user_message="hello")
        second = hooks_mod.MemChorusHooks().on_pre_llm_call(user_message="hello again")

    assert first is None, "unavailable orchestrator must still return None"
    assert second is None, "unavailable orchestrator must still return None"

    warnings = _loud_degrade_warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one loud-degrade WARNING, got: {warnings}"
    assert "no orchestrator instance" in warnings[0]


def test_loud_degrade_warning_emitted_once_across_hook_methods(hooks_mod, monkeypatch, caplog):
    """The once-per-process flag is shared: later unavailable calls never re-warn."""
    monkeypatch.setattr(hooks_mod, "_get_orchestrator", lambda: None)
    hook = hooks_mod.MemChorusHooks()

    with caplog.at_level(logging.WARNING, logger="memchorus.hooks"):
        for _ in range(3):
            assert hook.on_pre_llm_call(user_message="probe") is None
        assert hook.on_post_tool_call(tool_name="terminal", output="ok") is None

    warnings = _loud_degrade_warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one warning across 4 calls, got: {warnings}"


def test_import_failure_path_carries_real_error(hooks_mod, caplog):
    """_get_orchestrator's import-failure fallback surfaces the real import error."""
    def _bootstrap_boom():
        raise ImportError("No module named 'yaml'")

    hooks_mod._trigger_memchorus_bootstrap = _bootstrap_boom

    with caplog.at_level(logging.WARNING, logger="memchorus.hooks"):
        first = hooks_mod._get_orchestrator()
        second = hooks_mod._get_orchestrator()

    assert first is None
    assert second is None

    warnings = _loud_degrade_warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one warning, got: {warnings}"
    assert "No module named 'yaml'" in warnings[0], (
        f"warning must carry the real import error, got: {warnings[0]!r}"
    )
