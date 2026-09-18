"""IMPL #204 — LIVE MCP run-loop coverage for PersistentMcpSession.

The 8 tests in ``test_mempalace_persistent_session.py`` pass, but every one of
them runs against the **local-JSON-cache / state-machine** path (or the
``PYTEST_CURRENT_TEST`` fast-escape) and never asserts a real
``stdio_client`` / ``ClientSession`` / ``call_tool`` round-trip.  That is the
gap MemChorus #204 calls out and the reason the 2026-09-11 live crash
surfaced despite green tests.

These tests close the gap by driving the *real* worker ``run()`` loop
(``mempalace_persistent_session.py`` — the async dispatch loop that runs after
``session.initialize()``) against faked MCP transports:

- ``pps._skip_live`` is patched to ``False`` so the worker does **not** take
  the ``PYTEST_CURRENT_TEST`` escape and launches the live path (the seam
  added in IMPL #204).
- ``mcp.client.stdio.StdioServerParameters`` / ``mcp.client.stdio.stdio_client``
  and ``mcp.client.session.ClientSession`` are patched to fakes.  Because the
  worker resolves these with a LAZY ``from mcp.client... import ...`` inside
  ``_worker()`` (which runs after the patch is applied), the worker picks up
  our doubles — i.e. we are exercising the real live code, not the fallback.
- The session then runs in its real background thread and tool calls are
  dispatched through the real ``call_tool`` → ``work_event`` → ``run()`` loop,
  asserting the live behaviour end-to-end.

These tests are written to **fail on the pre-fix code** (with no seam the
worker never reaches the live loop and every call_tool returns None via the
local-cache fallback) and **pass once** the seam + the wait-then-clear
dispatch fix land, proving the gap was real, not incidental.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest import mock

import memchorus.mempalace_persistent_session as pps
from memchorus.mempalace_persistent_session import PersistentMcpSession

# The worker does `from mcp.client.stdio import ...` and
# `from mcp.client.session import ...` inside _worker(), so we patch these real
# modules (imported once here so the patch targets are stable objects).
from mcp.client import stdio as _stdio_mod  # type: ignore
from mcp.client import session as _session_mod  # type: ignore


# --------------------------------------------------------------------------- fakes
class _Stream:
    """Opaque stream token; the fake session only records which ones it got."""


class _ServerParams:
    """Fake for mcp.client.stdio.StdioServerParameters."""

    def __init__(self, command, args):
        self.command = command
        self.args = list(args)

    def __repr__(self) -> str:
        return f"StdioServerParameters(command={self.command!r}, args={self.args!r})"


class _TextBlock:
    """One MCP text content block (type='text', .text=...)."""

    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _ToolResult:
    """Fake for the object ClientSession.call_tool returns."""

    def __init__(self, text: str):
        self.content = [_TextBlock(text)]


class _FakeClientSession:
    """Stand-in for mcp.client.session.ClientSession (async context manager).

    Behaviour is selected per-instance by ``mode`` (passed as the first
    positional arg by the patched factory):
      - default: returns a JSON payload echoing the call (parseable → dict)
      - 'raw':    returns non-JSON text (→ {'raw': ...} fallback branch)
      - 'raise':  raises RuntimeError (→ result=None, session stays alive)
    """

    _last: "_FakeClientSession | None" = None

    def __init__(self, mode: str = "ok", **kw):
        self.mode = mode
        self.read_stream = kw.get("read_stream")
        self.write_stream = kw.get("write_stream")
        self.read_timeout = kw.get("read_timeout_seconds")
        self.initialized = False
        self.calls: list = []
        # 'raise-once' flips to 'ok' after its first call (proves the dispatch
        # loop continues after a tool failure).
        self._pending_failures = 1 if mode == "raise-once" else 0

    async def __aenter__(self) -> "_FakeClientSession":
        _FakeClientSession._last = self
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def initialize(self) -> None:
        # Yield once so the readiness handshake actually interleaves with the
        # caller thread (mirrors a real async initialize round-trip).
        await asyncio.sleep(0)
        _FakeClientSession._last = self
        self.initialized = True

    async def call_tool(self, name: str, arguments=None, **kw):
        await asyncio.sleep(0)
        self.calls.append((name, arguments))
        if self._pending_failures > 0:
            self._pending_failures -= 1
            raise RuntimeError("boom: tool failed")
        if self.mode == "raise":
            raise RuntimeError("boom: tool failed")
        if self.mode == "raw":
            return _ToolResult("this is raw, not json")
        if self.mode == "raw-unicode":
            return _ToolResult("héllo — non-ascii, still not json")
        # default: JSON payload echoing the call (parsed to a dict by the worker)
        return _ToolResult(json.dumps({"ok": True, "tool": name, "args": arguments}))


class _FakeStdioClient:
    """Stand-in for mcp.client.stdio.stdio_client (async CM → two streams)."""

    _last_params: object = None
    _entered: int = 0

    def __init__(self, server_params: _ServerParams):
        self.params = server_params
        type(self)._last_params = server_params

    async def __aenter__(self):
        type(self)._entered += 1
        return (_Stream(), _Stream())

    async def __aexit__(self, *exc) -> bool:
        return False


# ------------------------------------------------------------------ test harness
@contextmanager
def _live_patch(mode: str = "ok"):
    """Enable the live path via the IMPL #204 seam and swap the lazy-imported
    MCP symbols for our fakes.  Shared fake state is reset on entry so tests are
    order-independent (safe under pytest-xdist, where several tests in one file
    run sequentially in the same worker process)."""
    _FakeStdioClient._entered = 0
    _FakeStdioClient._last_params = None
    _FakeClientSession._last = None
    p1 = mock.patch.object(pps, "_skip_live", return_value=False)
    p2 = mock.patch.object(_stdio_mod, "StdioServerParameters", _ServerParams)
    p3 = mock.patch.object(_stdio_mod, "stdio_client", _FakeStdioClient)
    p4 = mock.patch.object(_session_mod, "ClientSession",
                          lambda **kw: _FakeClientSession(mode, **kw))
    for p in (p1, p2, p3, p4):
        p.start()
    try:
        yield
    finally:
        for p in (p1, p2, p3, p4):
            p.stop()


def _run_live(mode: str = "ok", calls: int = 1, timeout: float = 3.0) -> dict:
    """Run a real live session: patch the seams, start in a background thread,
    dispatch ``calls`` tool calls through the real call_tool API, capture
    behaviour, then stop.  Returns an observation dict (does not assert)."""
    with _live_patch(mode):
        session = PersistentMcpSession(command="fake-mcp-server", args=["--test"],
                                       timeout=timeout)
        started = session.start()
        try:
            results = [session.call_tool("mempalace_search", {"query": f"q{i}"})
                       for i in range(calls)]
            # Liveness *mid-flight*: captured before stop().  stop() flips
            # alive to False by design, so a post-stop "alive" read would be
            # vacuously False and mask the real "session survived the call"
            # property.  This is the signal the error-recovery test needs.
            alive_mid = session.alive
        finally:
            session.stop()
        last = _FakeClientSession._last
        return {
            "started": started,
            "alive": session.alive,            # post-stop (expected False)
            "alive_mid": alive_mid,            # mid-flight (the real assertion)
            "results": results,
            "fake_initialized": bool(last and last.initialized),
            "fake_calls": list(last.calls) if last else [],
            "fake_stdio_entered": _FakeStdioClient._entered,
            "fake_server_params": repr(_FakeStdioClient._last_params),
            "fake_session": last,
        }


# --------------------------------------------------------------------------- tests
def test_live_handshake_and_readiness():
    """(a) handshake/readiness: worker enters the stdio client, builds
    StdioServerParameters with our command/args, initializes the session, and
    start() returns True with the session alive."""
    obs = _run_live("ok", calls=0)
    assert obs["started"] is True, "live handshake should succeed"
    assert obs["alive_mid"] is True, "session must be alive after readiness"
    assert obs["fake_initialized"] is True, "ClientSession.initialize() must run"
    assert obs["fake_stdio_entered"] == 1, "stdio_client context must be entered once"
    assert "fake-mcp-server" in obs["fake_server_params"], \
        "our command must reach StdioServerParameters"


def test_live_dispatch_roundtrip_via_call_tool():
    """(b) work_event dispatch round-trip: a call dispatched via call_tool
    reaches the live ClientSession.call_tool with the same name/args and the
    parsed result is returned to the caller."""
    obs = _run_live("ok", calls=1)
    assert obs["started"] is True
    assert obs["fake_initialized"] is True
    assert len(obs["fake_calls"]) == 1, "exactly one live call_tool must be observed"
    name, args = obs["fake_calls"][0]
    assert name == "mempalace_search"
    assert args == {"query": "q0"}
    assert obs["results"][0] == \
        {"ok": True, "tool": "mempalace_search", "args": {"query": "q0"}}


def test_live_text_block_json_parsing():
    """(c) MCP text-block → JSON parsing: content whose single text block is
    valid JSON must be parsed to a dict (tool-name preserved, not a raw str)."""
    obs = _run_live("ok", calls=1)
    assert isinstance(obs["results"][0], dict), "JSON text block must parse to a dict"
    assert obs["results"][0]["tool"] == "mempalace_search"


def test_live_text_block_raw_fallback():
    """(c) raw-fallback branch: non-JSON text must come back as {'raw': ...}
    (the worker's JSONDecodeError fallback path)."""
    obs = _run_live("raw", calls=1)
    assert obs["results"][0] == {"raw": "this is raw, not json"}


def test_live_raw_fallback_nonascii():
    """(c) raw-fallback handles non-ASCII text without an encoding error."""
    obs = _run_live("raw-unicode", calls=1)
    assert obs["results"][0] == {"raw": "héllo — non-ascii, still not json"}


def test_live_call_tool_none_on_error_stays_alive():
    """A live call_tool that raises must surface as a None result to the
    caller while the session stays alive (a single tool failure must not kill
    the persistent session)."""
    obs = _run_live("raise", calls=1)
    assert obs["started"] is True
    assert obs["results"][0] is None, "tool failure must surface as a None result"
    assert obs["alive_mid"] is True, "session must survive a single tool failure"


def test_live_dispatch_continues_after_tool_failure():
    """Regression: once a tool call fails, the next call must STILL be
    dispatched through the live loop and succeed (proves the worker's
    per-call exception handler re-enters the while loop rather than exiting
    it).  First call fails → None; second call succeeds → parsed dict."""
    obs = _run_live("raise-once", calls=2)
    assert obs["started"] is True
    assert len(obs["fake_calls"]) == 2, "both calls must reach the live session"
    assert obs["results"][0] is None, "first (failing) call must surface as None"
    assert obs["results"][1] == \
        {"ok": True, "tool": "mempalace_search", "args": {"query": "q1"}}, \
        "second call must succeed after the failure"
    assert obs["alive_mid"] is True, "session must still be alive after recovery"


def test_live_multiple_calls_no_lost_wakeup():
    """Regression: several sequential calls must each reach the live session
    and return a result — proving the wait-then-clear dispatch loop does not
    drop a wakeup (the pre-fix clear-before-wait could wipe the next call's
    set() on interleaving, timing the caller out into a spurious None)."""
    n = 12
    obs = _run_live("ok", calls=n)
    assert obs["started"] is True
    assert len(obs["fake_calls"]) == n, \
        f"expected {n} live calls, saw {len(obs['fake_calls'])}"
    assert all(r is not None for r in obs["results"]), \
        "no call may time out to a lost wakeup"
    assert obs["results"] == [
        {"ok": True, "tool": "mempalace_search", "args": {"query": f"q{i}"}}
        for i in range(n)
    ]


def test_live_stop_is_clean_and_idempotent():
    """Stop after a live call must join the worker and be safe to call twice."""
    with _live_patch("ok"):
        session = PersistentMcpSession(command="fake-mcp-server", args=[], timeout=3.0)
        assert session.start() is True
        assert session.call_tool("t", {}) is not None
        session.stop()
        session.stop()  # idempotent, must not raise
        assert session.alive is False


def test_live_seam_symbols_exist():
    """Sanity: the seam we patch is real and the MCP symbols the worker imports
    are present on those modules (guards against silently regressing onto the
    local-cache path because the fake wasn't actually swapped in)."""
    assert hasattr(_stdio_mod, "StdioServerParameters")
    assert hasattr(_stdio_mod, "stdio_client")
    assert hasattr(_session_mod, "ClientSession")
    assert callable(pps._skip_live)
