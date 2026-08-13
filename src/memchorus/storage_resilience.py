"""Storage resilience layer for ChromaDB/MemPalace operations.

Provides retry-with-backoff and per-drawer isolation so transient ChromaDB
errors (compactor crashes, metadata log corruption) no longer abort an entire
batch of memory saves.  A single bad drawer degrades gracefully while the rest
of the session continues saving normally.

Usage:
    from memchorus.storage_resilience import wrap_save_operation

    # Wrap any save that calls into mempalace/ChromaDB underneath
    ok, detail = wrap_save_operation(
        fn=lambda: orchestrator.save(key=k, text=t),
        drawer_id=k,
    )

Configurable via environment:
    MEMCHORUS_STORAGE_RETRIES     (int)  max retries — defaults to 3
    MEMCHORUS_STORAGE_BACKOFF_BASE  (float) base delay in seconds — defaults to 1.5
"""

import logging
import os
import random
import time
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE_SECONDS = 1.5

MAX_RETRIES: int = int(os.environ.get("MEMCHORUS_STORAGE_RETRIES", _DEFAULT_MAX_RETRIES))
BACKOFF_BASE: float = float(
    os.environ.get("MEMCHORUS_STORAGE_BACKOFF_BASE", _DEFAULT_BACKOFF_BASE_SECONDS)
)

# Transient error signatures that justify a retry.  We only retry these —
# validation errors, not-found, or data-integrity failures must propagate.
_TRANSIENT_ERROR_PATTERNS = (
    "chromadb.errors.InternalError",               # compactor crash
    "InternalError:",                               # general internal failure during query plan
    "Failed to apply logs",                        # backfill/compaction log corruption
)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _is_transient_storage_error(error: Exception) -> bool:
    """Return True if the exception looks like a transient ChromaDB/storage error
    worth retrying rather than a permanent data-integrity failure."""
    text = get_full_exception_message(error)
    for pattern in _TRANSIENT_ERROR_PATTERNS:
        if pattern.lower() in text.lower():
            return True
    return False


def get_full_exception_message(exc: BaseException) -> str:
    """Return a single string containing the exception class + message + chained causes."""
    parts: List[str] = []
    cur = exc
    while cur is not None:
        parts.append(f"{type(cur).__module__}.{type(cur).__name__}: {cur}" if str(cur) else f"{type(cur).__module__}.{type(cur).__name__}")
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return " | ".join(parts)


@wraps(time.sleep)
def _backoff_sleep(delay: float, jitter_fraction: float = 0.25) -> None:
    """Sleep with bounded jitter to prevent thundering-herd on the compactor."""
    jitter = delay * jitter_fraction
    actual = delay + random.uniform(-jitter, jitter)
    time.sleep(max(0, actual))


# ---------------------------------------------------------------------------
# Single-operation wrapper (used by hooks and orchestrator)
# ---------------------------------------------------------------------------

def wrap_save_operation(
    fn: Callable[[], Any],
    drawer_id: str = "<unnamed>",
    max_retries: Optional[int] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Execute *fn* with retry+backoff for transient ChromaDB/storage errors.

    Returns ``True`` plus a detail dict when the operation completed successfully.
    On persistent failure after all retries returns ``False`` with an error summary
    so the caller can log and continue processing other drawers without crashing."""

    limit = max_retries if max_retries is not None else MAX_RETRIES
    last_error: Optional[Exception] = None

    for attempt in range(1 + limit):
        try:
            result = fn()
            return True, {"result": result, "drawer_id": drawer_id, "attempts": attempt}
        except Exception as exc:
            err_msg = get_full_exception_message(exc)
            logger.debug("storage attempt %d for '%s': %s", attempt + 1, drawer_id, err_msg)

            if _is_transient_storage_error(exc):
                if attempt < limit:
                    delay = BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "Transient storage error on attempt %d for '%s' — backing off %0.2fs",
                        attempt + 1, drawer_id, delay,
                    )
                    _backoff_sleep(delay)
                    continue

                # Exhausted retries — still count it as transient failure
                logger.error(
                    "Storage FAILED after %d retries for '%s' — content lost. Last error: %s",
                    limit + 1, drawer_id, err_msg,
                )
                return False, {
                    "error": err_msg,
                    "drawer_id": drawer_id,
                    "attempts_made": attempt + 1,
                }

            # Non-transient — raise immediately so upstream sees a real error
            raise
    # Should not be reached, but defensive fallback
    return False, {"error": "Unreachable", "drawer_id": drawer_id}


# ---------------------------------------------------------------------------
# Batch wrapper (iterates multiple saves, stops only on non-transient errors)
# ---------------------------------------------------------------------------

class StorageBatch:
    """Wraps a sequence of save operations with per-drawer isolation.

    The constructor takes a list of ``(fn, drawer_id)`` tuples where each ``fn``
    is zero-arg callable that performs one save or probe.  Call ``run()`` to
    execute the entire batch, returning how many succeeded and a loss summary."""

    def __init__(self, operations: List[Tuple[Callable[[], Any], str]], max_retries: Optional[int] = None) -> None:
        self.operations = operations
        self.max_retries = max_retries
        self._results: Dict[str, Tuple[bool, Dict]] = {}

    def run(self) -> int:
        """Execute all operations and return the number of successful ones.

        Transient failures after retry exhaustion are logged individually but
        do **not** abort remaining saves in the batch."""
        success_count = 0
        for fn, drawer_id in self.operations:
            ok, detail = wrap_save_operation(fn=fn, drawer_id=drawer_id, max_retries=self.max_retries)
            self._results[drawer_id] = (ok, detail)
            if ok:
                success_count += 1
        logger.info(
            "StorageBatch complete — %d/%d drawers saved successfully",
            success_count, len(self.operations),
        )
        return success_count

    @property
    def summary(self) -> Dict[str, Any]:
        """Return structured results: successes, losses and their error summaries."""
        ok_ids = [k for k, (v, _) in self._results.items() if v]
        fail_ids = {k: d for k, (v, d) in self._results.items() if not v}
        return {
            "total": len(self._results),
            "saved": len(ok_ids),
            "lost": len(fail_ids),
            "success_ids": ok_ids,
            "loss_details": fail_ids,
        }
