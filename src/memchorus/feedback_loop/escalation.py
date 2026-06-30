"""Escalation tracking for feedback-loop conditions.

Provides ``EscalationTracker`` -- a lightweight per-loop state manager that
handles cooldown windows and progressive severity levels (1-3).  This module
is designed to be thin, deterministic, and testable without any external state
beyond what it holds in its own data structures.

Usage::

    tracker = EscalationTracker()
    if tracker.check_cooldown("spiral_guard", 120):
        level = tracker.record_trigger("spiral_guard")
    print(tracker.get_escalation_level("spiral_guard"))  # -> level
"""


from __future__ import annotations

import time
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_COOLDOWN_SECONDS = 60
_MAX_COOLDOWN_SECONDS = 3_600
_DEFAULT_LEVEL_THRESHOLD = 3  # triggers per level advancement

_SEVERITY_DESC: Dict[int, str] = {
    1: "L1: Warning -- log context spiral risk",
    2: "L2: Prompt -- inject corrective feedback",
    3: "L3: Override -- force summarization or context truncation",
}


# ---------------------------------------------------------------------------
# EscalationTracker
# ---------------------------------------------------------------------------

class EscalationTracker:
    """Per-loop escalation with cooldown windows and L1-L3 stepped severity.

    Internal state per loop is keyed in ``self._loop_state[name]`` -- a mutable
    dict containing (at minimum) the keys ``trigger_count``, ``level``,
    ``last_fired_at`` and ``threshold_per_level`` so that unit tests can inject
    fake state without instantiating the real tracker.
    """

    def __init__(
        self, default_cooldown: float = _DEFAULT_COOLDOWN_SECONDS, level_threshold: int = _DEFAULT_LEVEL_THRESHOLD
    ) -> None:
        self._loop_state: Dict[str, Dict] = {}
        self._state: Dict[str, Any] = dict(self._loop_state)  # compat shim
        self._default_cooldown = max(0.0, min(float(default_cooldown), float(_MAX_COOLDOWN_SECONDS)))
        self._level_threshold: int = level_threshold

    def _ensure_loop(self, loop_name: str) -> Dict:
        """Return or create the mutable state dict for *loop_name*."""
        if loop_name not in self._loop_state:
            self._loop_state[loop_name] = {
                "trigger_count": 0,
                "last_fired_at": 0.0,
                "level": 1,
                "threshold_per_level": self._level_threshold,
                "cooldown_seconds": _DEFAULT_COOLDOWN_SECONDS,
            }
        return self._loop_state[loop_name]

    # -- public API ---------------------------------------------------------

    def init_loop(self, loop_name: str, cooldown_seconds: Any = None) -> None:
        """Create or reset state for *loop_name*."""
        _cd = max(0.0, min(float(cooldown_seconds), float(_MAX_COOLDOWN_SECONDS))) if cooldown_seconds is not None else self._default_cooldown
        entry = dict(
            trigger_count=0, last_fired_at=0.0, level=1,
            threshold_per_level=self._level_threshold,
            cooldown_seconds=_cd,
        )
        self._loop_state[loop_name] = entry

    def check_cooldown(self, loop_name: str, interval: float) -> bool:
        """Return ``True`` if cooldown has expired (loop *can* fire)."""
        state = self._loop_state.get(loop_name)
        if state is None:
            return True  # never fired => always allowed
        last = state.get("last_fired_at", 0.0)
        if last == 0.0:
            return True
        elapsed = time.monotonic() - last
        return elapsed >= interval

    def record_trigger(self, loop_name: str) -> int:
        """Mark this loop as fired *now*.  Advances escalation step if threshold crossed."""
        state = self._ensure_loop(loop_name)
        state["trigger_count"] += 1
        state["last_fired_at"] = time.monotonic()

        count = state["trigger_count"]
        threshold = max(1, state.get("threshold_per_level", self._level_threshold))
        new_level = min(3, max(1, (count - 1) // threshold + 1))
        state["level"] = new_level
        return new_level

    def get_escalation_level(self, loop_name: str) -> int:
        """Return current escalation level for *loop_name* (defaulting to 1)."""
        state = self._ensure_loop(loop_name)
        return state["level"]

    def reset_loop(self, loop_name: str) -> None:
        """Remove tracked state for *loop_name*, effectively resetting it."""
        self._loop_state.pop(loop_name, None)
        self._state.pop(loop_name, None)  # compat shim

    # -- helpers (used by engine code) --------------------------------------

    def get_cooldown_remaining_seconds(self, loop_name: str) -> float:
        state = self._loop_state.get(loop_name)
        if state is None or state.get("last_fired_at", 0.0) == 0.0:
            return 0.0
        elapsed = time.monotonic() - state["last_fired_at"]
        interval = state.get("cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS)
        return max(0.0, interval - elapsed)

    @property
    def trigger_count(self) -> int:
        """Compat shim for older EscalationManager signature.

        Returns the total number of triggers across all tracked loops.
        """
        return sum(state.get("trigger_count", 0) for state in self._loop_state.values())

    @property
    def get_cooldown_remaining(self) -> Any:
        """Compat shim for older EscalationManager signature."""
        return False  # compat shim -- legacy interface, not used by callers
