"""Contract tests for the MemChorus provider's session-boundary behaviour (issue #262).

The bug (#262): the legacy ``memchorus.hooks.on_session_end`` reads
``kwargs.get("conversation_history")`` to drive the auto-tuning feedback loop
(MistakeDetector → ``mark_relevant_injected_as_stale`` / ``_useful`` →
``run_calibration_cycle``). But in a live Hermes run that generic hook is fired
by ``agent.turn_finalizer`` with **only** the ``_DEFAULT_PAYLOADS`` shape —
``session_id/task_id/turn_id/completed/failed/interrupted/turn_exit_reason/model/platform``
— which carries **no** ``conversation_history`` and no message content. So
``user_texts`` was always ``[]``, the ``mark_*`` calls never ran, and
:class:`HitRateTracker` received no production feedback. The old contract tests
passed only because they invented a ``conversation_history`` kwarg Hermes never
sends.

The fix rides the MemoryProvider path landed by #273: the **real** session-end
is :meth:`MemChorusMemoryProvider.on_session_end`, called by
:class:`agent.memory_manager.MemoryManager` with the full conversation as a
``List[Dict]`` of ``{"role", "content"}`` at the CLI ``atexit`` boundary. This
test drives that method with that real payload shape (no invented kwargs) and
asserts the feedback loop actually fires.

It also covers AC-3: orientation / project context must reach the model on the
first turn. In Hermes that happens via :meth:`MemChorusMemoryProvider.
system_prompt_block` (consumed by ``memory_manager.build_system_prompt``) and
:meth:`MemChorusMemoryProvider.prefetch` — both host-supported — so we assert
the block is non-empty and carries the canonical orientation header.

Run with:  pytest tests/test_provider_session_feedback_contract.py -v
"""
import os
import sys
from typing import Any, Dict, List

import pytest

# ── Path setup ──────────────────────────────────────────────────────────
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
_HERMES_AGENT = os.path.expanduser("~/.hermes/hermes-agent")

for _p in (_REPO, _SRC, _HERMES_AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from integrations.provider import MemChorusMemoryProvider  # noqa: E402


# ── Fakes ───────────────────────────────────────────────────────────────
class _FakeOrchestrator:
    """Captures the feedback-loop calls the provider makes at session end.

    Mirrors the surface of :class:`memchorus.orchestrator.MemoryOrchestrator`
    that :meth:`MemChorusMemoryProvider.on_session_end` touches:
    ``mark_relevant_injected_as_stale`` / ``_useful`` /
    ``run_calibration_cycle`` / ``save``. The ``_recent_recall_keys`` buffer is
    non-empty so the ``mark_*`` methods have something to record.
    """

    def __init__(self) -> None:
        self.stale_calls = 0
        self.useful_calls = 0
        self.calibration_calls = 0
        self.stale_return = 3   # pretend 3 recall keys were wired
        self.useful_return = 2  # pretend 2 recall keys were wired

    def mark_relevant_injected_as_stale(self) -> int:
        self.stale_calls += 1
        return self.stale_return

    def mark_relevant_injected_as_useful(self) -> int:
        self.useful_calls += 1
        return self.useful_return

    def run_calibration_cycle(self, min_interval_hours: float = 24.0,
                              force: bool = False) -> Dict[str, Any]:
        self.calibration_calls += 1
        return {"calibrated": True, "profile": "test", "params": {}}


def _make_provider(orch: _FakeOrchestrator) -> MemChorusMemoryProvider:
    """Build a provider wired to the fake orchestrator, no bootstrap."""
    prov = MemChorusMemoryProvider(orchestrator=orch)
    # A project + session so the checkpoint branch is exercised too.
    prov._session_id = "test-session"
    prov._agent_identity = "test-agent"
    return prov


# Real Hermes payload: a List[Dict] of {role, content}. This is EXACTLY the
# shape ``memory_manager.on_session_end`` hands to the provider (verified at
# memory_manager.py:855-860). No ``conversation_history`` kwarg, no invented keys.
_REAL_MESSAGES_NOISE: List[Dict[str, Any]] = [
    {"role": "user", "content": "that's wrong, please fix it"},
    {"role": "assistant", "content": "I'll fix it now."},
    {"role": "user", "content": "no, that's not right either"},
]

_REAL_MESSAGES_CLEAN: List[Dict[str, Any]] = [
    {"role": "user", "content": "summarise the release notes"},
    {"role": "assistant", "content": "Here is the summary."},
]


# ── AC-2: feedback loop fires with a real (noise) session ───────────────
def test_on_session_end_noisy_session_wires_stale(monkeypatch) -> None:
    """A session where the user pushed back → recall keys are marked stale."""
    orch = _FakeOrchestrator()
    prov = _make_provider(orch)

    # Force the MistakeDetector to report noise for the user texts, so the
    # stale branch is taken deterministically (independent of real classifier).
    from memchorus import mistake_detector as _md
    monkeypatch.setattr(_md.MistakeDetector, "classify_and_flag",
                        lambda self, user_text: (1, 0))

    prov.on_session_end(_REAL_MESSAGES_NOISE)

    assert orch.stale_calls == 1, "stale feedback must be wired exactly once"
    assert orch.useful_calls == 0, "a noisy session must not also mark useful"
    assert orch.calibration_calls == 1, "calibration cycle must fire"


def test_on_session_end_clean_session_wires_useful(monkeypatch) -> None:
    """A clean session (no user pushback) → recall keys are marked useful."""
    orch = _FakeOrchestrator()
    prov = _make_provider(orch)

    from memchorus import mistake_detector as _md
    monkeypatch.setattr(_md.MistakeDetector, "classify_and_flag",
                        lambda self, user_text: (0, 0))

    prov.on_session_end(_REAL_MESSAGES_CLEAN)

    assert orch.useful_calls == 1, "useful feedback must be wired exactly once"
    assert orch.stale_calls == 0, "a clean session must not mark stale"
    assert orch.calibration_calls == 1, "calibration cycle must fire"


def test_on_session_end_empty_messages_no_feedback(monkeypatch) -> None:
    """No user text at all → neither mark_* is called (no buffer to route)."""
    orch = _FakeOrchestrator()
    prov = _make_provider(orch)

    from memchorus import mistake_detector as _md
    monkeypatch.setattr(_md.MistakeDetector, "classify_and_flag",
                        lambda self, user_text: (1, 0))

    prov.on_session_end([])

    assert orch.stale_calls == 0
    assert orch.useful_calls == 0


def test_on_session_end_tolerates_orchestrator_failure() -> None:
    """Feedback bookkeeping must never raise into session teardown."""
    orch = _FakeOrchestrator()
    prov = _make_provider(orch)
    # Even if classify_and_flag throws, on_session_end must swallow it.
    from memchorus import mistake_detector as _md

    def _boom(self, user_text):
        raise RuntimeError("detector exploded")

    import memchorus.mistake_detector as _mod
    _orig = _mod.MistakeDetector.classify_and_flag
    _mod.MistakeDetector.classify_and_flag = _boom
    try:
        prov.on_session_end(_REAL_MESSAGES_CLEAN)  # must not raise
    finally:
        _mod.MistakeDetector.classify_and_flag = _orig
    assert orch.calibration_calls == 1  # later stages still run


def test_on_session_end_ignores_invented_conversation_history_kwarg() -> None:
    """The provider must NOT depend on a ``conversation_history`` kwarg.

    It reads the ``messages`` positional argument (the real Hermes payload).
    Passing the legacy invented kwarg must not change the feedback outcome.
    """
    orch = _FakeOrchestrator()
    prov = _make_provider(orch)
    from memchorus import mistake_detector as _md
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_md.MistakeDetector, "classify_and_flag",
                        lambda self, user_text: (0, 0))

    # messages list is clean → useful is wired from the REAL messages arg.
    prov.on_session_end(_REAL_MESSAGES_CLEAN, conversation_history=["invented"])
    assert orch.useful_calls == 1
    monkeypatch.undo()


# ── AC-3: orientation reaches the model on the first turn ───────────────
def test_system_prompt_block_is_nonempty_and_oriented() -> None:
    """The 'woke up' block the host injects must never be empty."""
    prov = _make_provider(_FakeOrchestrator())
    block = prov.system_prompt_block()
    assert isinstance(block, str)
    assert block.strip(), "system_prompt_block must be non-empty (AC-3)"
    # Canonical orientation header is always present, even in degraded mode.
    assert "MemChorus Orientation" in block
    assert "Active project:" in block
    assert "Active task:" in block


def test_prefetch_returns_orientation_block() -> None:
    """prefetch() (the host's first-turn recall path) must return the block."""
    prov = _make_provider(_FakeOrchestrator())
    block = prov.prefetch("release notes")
    assert isinstance(block, str)
    assert block.strip(), "prefetch must return a non-empty block (AC-3)"
    assert "MemChorus Orientation" in block


def test_orientation_survives_no_orchestrator() -> None:
    """Even with no orchestrator, the orientation block is non-empty and
    carries the canonical session-start header (AC-3: context reaches the model).

    The provider resolves a project from env even with ``orchestrator=None``,
    so we assert on the *structural* contract (header + project/task lines)
    rather than on the presence of the word "degraded" — a live orientation
    source in the test environment can legitimately produce non-degraded output.
    """
    prov = MemChorusMemoryProvider(orchestrator=None)
    prov._session_id = "test"
    block = prov.system_prompt_block()
    assert block.strip(), "orientation block must never be empty (AC-3)"
    assert "MemChorus Orientation" in block, "canonical header must be present"
    assert "Active project:" in block, "project line must be present"
    assert "Active task:" in block, "task line must be present"


# ── Guard: the provider satisfies the real MemoryProvider ABC ───────────
def test_provider_satisfies_memory_provider_abc() -> None:
    """on_session_end must be a callable instance method with the right shape."""
    import inspect
    sig = inspect.signature(MemChorusMemoryProvider.on_session_end)
    params = list(sig.parameters)
    # (self, messages, **kwargs)
    assert params[0] == "self"
    assert params[1] == "messages"
