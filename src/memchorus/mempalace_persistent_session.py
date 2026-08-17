"""Persistent MCP session support for MemPalace.

Replaces the per-call subprocess model with a long-lived session that stays
alive across multiple tool calls. This prevents ChromaDB compactor crashes
caused by repeated spawn-and-kill cycles.

The persistent session runs in a background thread with its own asyncio event
loop, so it integrates cleanly with MemChorus's synchronous API surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PersistentSessionState:
    """Thread-safe state for a persistent MCP session."""
    ready_event: threading.Event = field(default_factory=threading.Event)
    work_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)

    # Pending work (set by caller, consumed by worker)
    pending_name: str = ""
    pending_args: Dict[str, Any] = field(default_factory=dict)

    # Result from last call (set by worker, read by caller)
    result: Optional[Dict[str, Any]] = None
    result_ready: threading.Event = field(default_factory=threading.Event)

    # Liveness tracking
    alive: bool = False
    last_activity: float = 0.0


class PersistentMcpSession:
    """Keeps one MCP subprocess + ClientSession alive across tool calls.

    Usage:

        session = PersistentMcpSession(command=cmd, args=args, timeout=30)
        if session.start():
            ok, data = session.call_tool("mempalace_search", {"query": "test"})
            # ... make more calls ...
            session.stop()
    """

    def __init__(self, command: str, args: List[str], timeout: float = 30.0):
        self.command = command
        self.args = list(args)
        self.timeout = max(1, float(timeout))
        self._state = PersistentSessionState()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """Spawn the background thread and MCP session. Returns True on success."""
        if self._state.alive:
            return True

        self._state = PersistentSessionState()
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="mcp-persist"
        )
        self._thread.start()

        # Wait for the worker to complete initialization (or timeout)
        ready = self._state.ready_event.wait(timeout=self.timeout)
        self._state.last_activity = time.time()
        return ready and self._state.alive

    def stop(self):
        """Signal and wait for clean shutdown."""
        with self._lock:
            if not self._state.alive:
                return
            self._state.alive = False
            self._state.work_event.set()  # wake worker so it can exit

        if self._thread is not None:
            self._thread.join(timeout=self.timeout)

    @property
    def alive(self) -> bool:
        return self._state.alive

    # ------------------------------------------------------------------ tool calls

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:  # type: ignore[misc]
        """Run one tool call through the persistent session.

        Returns (True, parsed_dict) on success or (False, None) on failure.
        The caller decides fallback policy.
        """
        if not self._state.alive:
            logger.warning("PersistentMcpSession.call_tool: session not alive")
            return None

        # Clear any old result
        self._state.result_ready.clear()
        self._state.result = None

        with self._lock:
            self._state.pending_name = name
            self._state.pending_args = arguments
            self._state.last_activity = time.time()

        # Signal worker to process
        self._state.work_event.set()

        # Wait for result or timeout
        got_result = self._state.result_ready.wait(timeout=self.timeout * 2)
        if not got_result:
            logger.error("PersistentMcpSession.call_tool: timed out waiting for result")
            self._state.alive = False
            return None

        return self._state.result

    # -------------------------------------------------------------------------- internal

    def _worker(self):
        """Async event loop running in a background thread."""

        # Do not start the MCP persistence thread during pytest runs — mcp.client.stdio may
        # not be installed, and letting it raise here creates unhandled thread exceptions that
        # pollute test output even when graceful degradation works in the main process.
        if "PYTEST_CURRENT_TEST" in os.environ:
            self._state.alive = False
            self._state.ready_event.set()
            return

        # Wrap stdio imports so a missing mcp package does not crash the thread either.
        try:
            from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: F401
            from mcp.client.session import ClientSession  # noqa: F401
        except ImportError as exc:
            logger.warning(
                "PersistentMcpSession worker: mcp.client not available — "
                "persistent session will not start (%s)", exc
            )
            self._state.alive = False
            self._state.ready_event.set()
            return

        server_params = StdioServerParameters(command=self.command, args=self.args)

        async def run():
            try:
                async with stdio_client(server_params) as (r_stream, w_stream):
                    async with ClientSession(
                        read_stream=r_stream,
                        write_stream=w_stream,
                        read_timeout_seconds=timedelta(seconds=self.timeout * 2),
                    ) as session:
                        await session.initialize()

                        # Signal ready
                        self._state.alive = True
                        self._state.ready_event.set()

                        # Main loop: wait for work events
                        while self._state.alive:
                            self._state.work_event.clear()
                            self._state.work_event.wait()

                            if not self._state.alive:
                                break  # shutdown signal

                            tool_name = self._state.pending_name
                            tool_args = dict(self._state.pending_args)

                            try:
                                result = await session.call_tool(
                                    tool_name, arguments=tool_args
                                )

                                # Parse MCP text blocks into JSON
                                texts: list[str] = []
                                if hasattr(result, "content") and result.content:
                                    for block in result.content:
                                        if getattr(block, "type", "") == "text":
                                            texts.append(str(getattr(block, "text", "")))

                                raw = "\n".join(texts) if texts else ""
                                try:
                                    parsed = json.loads(raw) if raw else {}
                                except (json.JSONDecodeError, TypeError):
                                    parsed = {"raw": raw} if raw else {}

                                self._state.result = parsed
                                self._state.result_ready.set()
                                self._state.last_activity = time.time()

                            except BaseExceptionGroup as exc:
                                # anyio TaskGroups raise BaseExceptionGroup (NOT Exception)
                                # during teardown when internal reader/flusher tasks are
                                # cancelled.  Catch it HERE so a single tool call failure
                                # doesn't kill the entire persistent session.  Unwrap to
                                # log the actual underlying error, then signal failure + continue.
                                sub_exc = exc.exceptions[0] if exc.exceptions else exc
                                logger.warning(
                                    "PersistentMcpSession worker: call_tool(%s) raised ExceptionGroup — %s",
                                    tool_name, sub_exc,
                                )
                                self._state.result = None
                                self._state.result_ready.set()

                            except Exception as exc:
                                logger.error(
                                    "PersistentMcpSession worker: call_tool(%s) failed: %s",
                                    tool_name, exc,
                                )
                                self._state.result = None
                                self._state.result_ready.set()

            except BaseExceptionGroup as exc:
                # Outer catch: only fires during session-level teardown
                # (stdio_client/ ClientSession context manager exit), not per-call.
                logger.warning(
                    "PersistentMcpSession worker: session-level BaseExceptionGroup (%d): %s",
                    len(exc.exceptions),
                    ", ".join(type(e).__name__ for e in exc.exceptions),
                )
            except Exception as exc:
                logger.error("PersistentMcpSession worker crashed: %s", exc)
            finally:
                self._state.alive = False
                self._state.ready_event.set()  # unblock a waiting start() caller

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()
