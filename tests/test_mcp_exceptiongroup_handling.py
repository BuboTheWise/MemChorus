#!/usr/bin/env python3
"""
test_mcp_exceptiongroup_handling.py - Verify BaseExceptionGroup graceful degradation.

Python 3.11+ introduces ExceptionGroup/BaseExceptionGroup via anyio's internal
TaskGroup implementation (used by mcp.client.stdio). The key distinction:

  ExceptionGroup  -> BaseExceptionGroup -> Exception    (caught by 'except Exception')
  BaseExceptionGroup -> BaseException                   (NOT caught by 'except Exception')

When anyio TaskGroup cancels a task (timeout, process exit), it can raise
BaseExceptionGroup — which ESCAPES 'except Exception' and crashes the session.

Covers:
- _McpClient._call() with BaseExceptionGroup -> returns None without crashing
- _McpClient.connect() with BaseExceptionGroup -> returns False without catching
- MemPalaceMemorySource graceful fallback when BaseExceptionGroup occurs
- _run_async propagation of BaseExceptionGroup up to caller handles it
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.mempalace_memory_source import (
    MemPalaceMemorySource, _McpClient, _run_async, _call_tool_async
)


def _make_fake_python(tmp_path):
    """Helper to create a minimal executable Python stub."""
    fake = tmp_path / "fake_python"
    fake.write_text("#!/usr/bin/env python3\nprint('stub')\n")
    fake.chmod(0o755)
    return fake


def _raise_base_eg():
    """Factory that returns a function raising BaseExceptionGroup (not Exception)."""
    def inner(*args, **kwargs):
        raise BaseExceptionGroup("anyio task group failure", [
            ConnectionError("stdio pipe closed unexpectedly"),
            TimeoutError("subprocess read timed out")
        ])
    return inner


class TestBaseExceptionGroupCrashFix:
    """Verify the fix for BaseExceptionGroup escaping 'except Exception'.

    Before fix: BaseExceptionGroup -> BaseException (NOT Exception) -> crashes.
    After fix: caught by 'except BaseExceptionGroup' handlers at every entry point.
    """

    def test_call_catches_base_exceptiongroup(self, tmp_path):
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        with patch.object(client, '_get_transport', return_value=(str(fake_py), [])):
            with patch('memchorus.mempalace_memory_source._run_async', side_effect=_raise_base_eg()):
                result = client._call("mempalace_search", {"query": "test"})

        assert result is None, f"_call must return None on BaseExceptionGroup, got {result}"
        assert client._connected is False, "_connected must be reset"

    def test_connect_catches_base_exceptiongroup(self, tmp_path):
        """connect() returns False on BaseExceptionGroup — not crash."""
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        with patch.object(client, '_get_transport', return_value=(str(fake_py), [])):
            with patch('memchorus.mempalace_memory_source._run_async', side_effect=_raise_base_eg()):
                result = client.connect()

        assert result is False, f"_connect must return False, got {result}"

    def test_call_tool_short_circuits_when_not_connected(self, tmp_path):
        """call_tool returns None immediately when _connected is False."""
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        # Don't connect at all — _connected starts as False
        result = client.call_tool("mempalace_search", {"query": "test"})
        assert result is None, "call_tool short-circuits when not connected"

    def test_memoerysource_init_survives_base_exceptiongroup_on_connect(self, tmp_path):
        """MemPalaceMemorySource.__init__ must not crash if BaseExceptionGroup occurs."""
        cache_dir = str(tmp_path / "mempalace_cache")
        fake_py = _make_fake_python(tmp_path)

        config = {
            "cache_dir": cache_dir,
            "python_bin": str(fake_py),
            "skip_mcp": False  # try to connect — expect failure
        }

        with patch('memchorus.mempalace_memory_source._run_async', side_effect=_raise_base_eg()):
            src = MemPalaceMemorySource(name="test-fallback", config=config)
            assert src.is_available() is True, "Should still be available via local fallback"

    def test_memoerysource_save_falls_back_to_local_cache(self, tmp_path):
        """save/retrieve fall back to local cache when MCP fails with BaseExceptionGroup."""
        cache_dir = str(tmp_path / "mempalace_cache")
        fake_py = _make_fake_python(tmp_path)

        config = {
            "cache_dir": cache_dir,
            "python_bin": str(fake_py),
            "skip_mcp": False
        }

        with patch('memchorus.mempalace_memory_source._run_async', side_effect=_raise_base_eg()):
            src = MemPalaceMemorySource(name="test-fallback", config=config)

            # Save succeeds via local cache fallback
            assert src.save("test_key", {"data": "value"}) is True, \
                "save() should succeed via local cache fallback"
            retrieved = src.retrieve("test_key")
            assert isinstance(retrieved, dict) and retrieved == {"data": "value"}


class TestExceptionGroupStillHandled:
    """Ensure regular ExceptionGroup (MRO includes Exception) is also caught."""

    def test_call_handles_exceptiongroup(self, tmp_path):
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        with patch.object(client, '_get_transport', return_value=(str(fake_py), [])):
            def raise_eg(*args, **kwargs):
                raise ExceptionGroup("mcp task error", [ValueError("inner")])
            with patch('memchorus.mempalace_memory_source._run_async', side_effect=raise_eg):
                result = client._call("mempalace_search", {"query": "test"})

        assert result is None, "_call must degrade on ExceptionGroup too"

    def test_connect_handles_exceptiongroup(self, tmp_path):
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        with patch.object(client, '_get_transport', return_value=(str(fake_py), [])):
            def raise_eg(*args, **kwargs):
                raise ExceptionGroup("init", [ConnectionError()])
            with patch('memchorus.mempalace_memory_source._run_async', side_effect=raise_eg):
                result = client.connect()

        assert result is False


class TestAllOriginalExceptionPaths:
    """Verify every catch handler in _call() still works correctly after changes."""

    def test_call_brokenpipe(self, tmp_path):
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        with patch.object(client, '_get_transport', return_value=(str(fake_py), [])):
            with patch('memchorus.mempalace_memory_source._run_async', side_effect=BrokenPipeError()):
                assert client._call("x", {}) is None

    def test_call_oserror(self, tmp_path):
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        with patch.object(client, '_get_transport', return_value=(str(fake_py), [])):
            with patch('memchorus.mempalace_memory_source._run_async', side_effect=OSError("enoent")):
                assert client._call("x", {}) is None

    def test_call_timeout(self, tmp_path):
        import asyncio as aio
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        with patch.object(client, '_get_transport', return_value=(str(fake_py), [])):
            with patch('memchorus.mempalace_memory_source._run_async', side_effect=aio.TimeoutError()):
                assert client._call("x", {}) is None

    def test_call_generic_exception(self, tmp_path):
        fake_py = _make_fake_python(tmp_path)
        client = _McpClient(timeout=1.0, config={"python_bin": str(fake_py)})

        with patch.object(client, '_get_transport', return_value=(str(fake_py), [])):
            with patch('memchorus.mempalace_memory_source._run_async', side_effect=RuntimeError()):
                assert client._call("x", {}) is None


class TestCallToolAsyncBaseExceptionGroup:
    """_call_tool_async itself has a try/BaseExceptionGroup handler now."""

    def test_call_tool_async_catches_and_returns_empty_dict(self):
        """Inside the async function, BaseExceptionGroup returns {} not a crash."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def raising_stdio(*args, **kwargs):
            raise BaseExceptionGroup("transport teardown failed", [BrokenPipeError()])
            yield None  # unreachable

        # Patch at the source module — _call_tool_async imports from here
        with patch('mcp.client.stdio.stdio_client', side_effect=raising_stdio):
            result = _run_async(_call_tool_async(
                "/usr/bin/python3", ["-m", "mempalace.mcp_server"],
                5.0, "mempalace_search", {"query": "test"}
            ))

        assert result == {}, f"_call_tool_async should return {{}} on BaseExceptionGroup, got {result}"
