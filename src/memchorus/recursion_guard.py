"""
RecursionGuard — thread-safe, module-level recursion depth counter.

Provides a shared context manager that tracks nesting depth across recursive
call chains (e.g., hook → search → enforce → recall → hook) and raises
RecursionError when the configured maximum depth is exceeded. Uses an RLock
so concurrent Hermes hook invocations from different threads don't race on
the counter.

Usage:

    guard = RecursionGuard(max_depth=5)
    ...
    with guard as depth:
        # depth == 1 on first entry, increments on each nested call
        pass

Designed to replace fragile boolean sentinels (_REC_GUARD, _in_enforcement_*) 
and module-level depth counters scattered across auto_recall_engine.py,
orchestrator.py, and hooks.py.
"""

import threading
from contextlib import contextmanager


class RecursionGuard:
    """Thread-safe recursion depth guard with configurable maximum nesting.

    Thread Safety: An internal :class:`threading.RLock` serializes access to
    the ``_depth`` counter so that concurrent hook invocations from different
    threads each see correct nesting state without data races.

    Args:
        max_depth: Maximum allowed nesting level (default 5). Set to any
            positive integer. A value of 1 means "no recursion at all."
    """

    def __init__(self, max_depth: int = 5) -> None:
        if max_depth < 1:
            raise ValueError(f"max_depth must be >= 1, got {max_depth}")
        self.max_depth = max_depth
        self._depth: int = 0
        self._lock: threading.RLock = threading.RLock()

    # ------------------------------------------------------------------
    # Context manager (preferred interface)
    # ------------------------------------------------------------------

    def __enter__(self) -> int:
        with self._lock:
            if self._depth >= self.max_depth:
                raise RecursionError(
                    f"RecursionGuard max depth exceeded ({self._depth}/{self.max_depth})"
                )
            self._depth += 1
            return self._depth

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        with self._lock:
            self._depth -= 1
        # Do not swallow exceptions — return False propagates them
        return False

    # ------------------------------------------------------------------
    # Query helpers (optional diagnostics / testing)
    # ------------------------------------------------------------------

    @property
    def current_depth(self) -> int:
        """Return the nesting depth without holding the lock long."""
        with self._lock:
            return self._depth

    def is_at_limit(self) -> bool:
        """Check whether entering would exceed the configured max_depth."""
        with self._lock:
            return self._depth >= self.max_depth

    @contextmanager
    def enter(self):
        """Generator-based context manager form (for ``with guard.enter(): ...``)."""
        depth = self.__enter__()
        try:
            yield depth
        finally:
            self.__exit__(None, None, None)

    # -- Sentinel / boolean check alias -----------------------------------

    def block_reentry(self) -> bool:
        """Return True if the guard is currently held (depth > 0)."""
        with self._lock:
            return self._depth > 0
