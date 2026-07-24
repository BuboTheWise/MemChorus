"""
tool_capture_buffer -- batched write buffer with flush timer for tool capture events.

Sits between AutoStorageEngine's filtering pipeline and the orchestrator save call,
accumulating payloads in memory and flushing them in batches when either:
- The item count threshold is reached (default 10), or
- The time interval expires (default 5 seconds).

This prevents hammering storage backends during rapid tool-call bursts while
ensuring no data is lost on shutdown via the context manager __exit__ path.

Public API:
    ToolCaptureBuffer(max_items=10, flush_interval=5.0, callback=<callable>)
        callback(payloads) receives a list of capture payloads for batch write.

    with ToolCaptureBuffer(callback=write_fn) as buf:
        buf.add({"text": "...", ...})
    # __exit__ flushes remaining items automatically.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)


@dataclass
class BufferStats:
    """Read-only snapshot of buffer state for introspection / testing."""

    total_items_added: int = 0
    total_flushes: int = 0
    total_items_flushed: int = 0
    last_flush_size: int = 0


BufferCallback = Callable[[List[Dict[str, Any]]], None]


class ToolCaptureBuffer:
    """Thread-safe batch buffer with countdown timer for tool capture events.

    Flushes when EITHER threshold is hit -- max_items OR flush_interval (seconds).

    Thread-safety: all public methods are safe from concurrent access via a Lock.
    Timer runs in a daemon thread so it won't block process exit, but we also
    drain on __exit__ for graceful shutdown.

    Args:
        max_items: Flush immediately after this many items accumulate (default 10).
        flush_interval: Seconds before time-based flush fires (default 5.0).
        callback: Receives List[Dict] payloads on each flush. Must not raise;
            exceptions are caught and logged. If None, payloads are dropped
            (useful for testing discard behavior or as a no-op sink).

    Usage:
        with ToolCaptureBuffer(max_items=50, flush_interval=10.0, callback=my_writer) as buf:
            for payload in events:
                buf.add(payload)
        # Remaining items flushed on exit.
    """

    def __init__(
        self,
        max_items: int = 10,
        flush_interval: float = 5.0,
        callback: Optional[BufferCallback] = None,
    ) -> None:
        if max_items < 1:
            raise ValueError(f"max_items must be >= 1, got {max_items}")
        if flush_interval <= 0:
            raise ValueError(f"flush_interval must be > 0, got {flush_interval}")

        self.max_items = max_items
        self.flush_interval = flush_interval
        self.callback = callback

        # Protected by _lock.
        self._queue: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._closed = False
        self._stats = BufferStats()

    @property
    def pending(self) -> int:
        """Number of items currently in the buffer (read-safe)."""
        with self._lock:
            return len(self._queue)

    @property
    def stats(self) -> BufferStats:
        """Snapshot of cumulative statistics."""
        snap = BufferStats(
            total_items_added=self._stats.total_items_added,
            total_flushes=self._stats.total_flushes,
            total_items_flushed=self._stats.total_items_flushed,
            last_flush_size=self._stats.last_flush_size,
        )
        return snap

    def add(self, payload: Dict[str, Any]) -> None:
        """Enqueue a capture payload. Triggers immediate flush if max_items reached.

        Args:
            payload: A dict representing one captured tool outcome (the result of
                AutoStorageEngine's filtering pipeline).

        Returns:
            None

        Raises:
            RuntimeError: if called after close().
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("Cannot add to closed buffer; call flush() before close()")
            self._queue.append(payload)
            self._stats.total_items_added += 1

            # Arm the flush timer on first item.
            if len(self._queue) == 1 and self._timer is None:
                self._arm_timer()

            # Immediate flush if count threshold hit.
            if len(self._queue) >= self.max_items:
                self._flush_locked()

    def flush(self) -> int:
        """Drain the buffer and deliver to callback. Thread-safe.

        Returns:
            Number of items flushed (0 when empty).
        """
        with self._lock:
            return self._flush_locked()

    def clear(self) -> None:
        """Discard all pending items without flushing. Use for reset/testing."""
        with self._lock:
            discarded = len(self._queue)
            self._queue.clear()
            if discarded:
                logger.debug("ToolCaptureBuffer: cleared %d pending items", discarded)

    def close(self) -> None:
        """Flush remaining items, cancel timer, mark buffer closed. No-op on subsequent calls."""
        with self._lock:
            if self._closed:
                return
            self._flush_locked()
            self._cancel_timer()
            self._closed = True

    def __enter__(self) -> "ToolCaptureBuffer":
        return self

    def __exit__(self, *_exc: Any) -> None:  # type: ignore[override]
        """Flush remaining items on context manager exit."""
        self.close()

    # --- Internal methods (caller MUST hold _lock) -----------------------------

    def _flush_locked(self) -> int:
        """Drain and deliver. Caller holds self._lock."""
        if not self._queue:
            return 0

        batch = self._queue[:]
        self._queue.clear()
        count = len(batch)

        # Cancel the pending timer since we're flushing now.
        self._cancel_timer()

        if self.callback is None:
            logger.warning(
                "ToolCaptureBuffer: no callback set, discarding %d items", count
            )
            self._stats.total_flushes += 1
            self._stats.last_flush_size = count
            return count

        try:
            self.callback(batch)
            self._stats.total_flushes += 1
            self._stats.total_items_flushed += count
            self._stats.last_flush_size = count
            logger.debug(
                "ToolCaptureBuffer: flush #%d — %d items",
                self._stats.total_flushes,
                count,
            )
        except Exception as exc:
            logger.error("ToolCaptureBuffer: callback failed — %s", exc)
            # Re-queue the batch so it's not silently lost; next add or flush will retry.
            self._queue = batch + self._queue

        return count

    def _arm_timer(self) -> None:
        """Arm a timer for time-based flush. Called with _lock held."""
        # Cancel existing timer first (safety net).
        self._cancel_timer()
        self._timer = threading.Timer(
            self.flush_interval,
            self._on_timer_expire,
        )
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self) -> None:
        """Cancel the pending timer. Caller may or may not hold _lock."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timer_expire(self) -> None:
        """Timer callback — drains buffer regardless of count."""
        logger.debug("ToolCaptureBuffer: timer expired, flushing")
        self.flush()
