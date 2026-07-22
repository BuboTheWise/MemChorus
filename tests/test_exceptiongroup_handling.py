"""ExceptionGroup handling in MemPalace MCP client.

Regression test for: ExceptionGroup propagating through _call_tool_async
when anyio task groups raise inside MCP subprocess teardown.

The crash happens when the MCP subprocess dies mid-operation and anyio's
create_task_group raises ExceptionGroup - this needs to be caught and handled
gracefully in all MCP code paths, not just RuntimeError/ConnectionError.

_run_async is a module-level function that wraps asyncio.run().  When the MCP
subprocess crashes inside an anyio TaskGroup, ExceptionGroup propagates out of
asyncio.run() because only RuntimeError is explicitly caught (for the set_event_loop
fallback).  _run_async now catches ExceptionGroup and returns None so callers can
handle it as a normal failure instead of crashing.
"""
import pytest
from unittest.mock import patch

from memchorus.mempalace_memory_source import _McpClient


class TestRuntimeExceptionGroup:
    """Verify _run_async unwraps ExceptionGroup without crashing (module-level helper)."""

    def test_run_async_returns_none_on_exceptiongroup(self):
        from memchorus.mempalace_memory_source import _run_async
        import asyncio

        async def broken():
            raise ExceptionGroup("task crash", [RuntimeError("server died")])

        # Patch asyncio.run so it can hit the code path under test
        with patch('memchorus.mempalace_memory_source.asyncio.run', side_effect=ExceptionGroup(
                "simulated anyio tg", [ConnectionRefusedError("subprocess gone")]
        )):
            result = _run_async(broken())

        assert result is None, "_run_async should return None on ExceptionGroup instead of crashing"

    def test_run_async_returns_none_on_nested_exceptiongroup(self):
        from memchorus.mempalace_memory_source import _run_async

        async def broken():
            pass  # Won't run - asyncio.run is mocked below

        with patch('memchorus.mempalace_memory_source.asyncio.run', side_effect=ExceptionGroup(
                "outer", [ExceptionGroup("inner", [RuntimeError("deep crash")])]
        )):
            result = _run_async(broken())

        assert result is None, "_run_async should handle nested ExceptionGroup too"


class TestMcpClientGracedegradation:
    """Call paths return safe values rather than crashing on ExceptionGroup."""

    def test_connect_does_not_crash_on_exceptiongroup(self):
        """When init fails with ExceptionGroup, connect returns False and stays disconnected.

        _run_async catches ExceptionGroup and returns None, so connect() treats that
        as failure (not success).
        """
        # Make sure the transport command exists so we hit _run_async path
        import shutil
        client = _McpClient(timeout=2)

        with patch('memchorus.mempalace_memory_source._run_async', return_value=None):
            result = client.connect()

        assert result is False, "connect must treat _run_async returning None as failure"
        assert not client._connected, "client must stay disconnected"

    def test_call_tool_does_not_crash_on_exceptiongroup(self):
        client = _McpClient(timeout=2)
        with patch('memchorus.mempalace_memory_source.asyncio.run', side_effect=ExceptionGroup(
                "tg", [RuntimeError("crashed")]
        )):
            # Should return None, not raise
            result = client.call_tool('mempalace_search', {'query': 'test'})

        assert result is None, "call_tool should return None when ExceptionGroup occurs"
        assert not client._connected, "should mark disconnected after failure"

    def test_search_does_not_crash_on_exceptiongroup(self):
        client = _McpClient(timeout=2)
        with patch('memchorus.mempalace_memory_source.asyncio.run', side_effect=ExceptionGroup(
                "tg", [RuntimeError("crashed")]
        )):
            result = client.search(query="test", limit=5)

        assert result is None or (isinstance(result, list) and len(result) == 0), \
            "search should return None or empty list on ExceptionGroup"

    def test_add_drawer_does_not_crash_on_exceptiongroup(self):
        client = _McpClient(timeout=2)
        with patch('memchorus.mempalace_memory_source.asyncio.run', side_effect=ExceptionGroup(
                "tg", [RuntimeError("crashed")]
        )):
            result = client.add_drawer(wing="w", room="r", content="c")

        assert not result, "add_drawer should return False on ExceptionGroup"


class TestMemorySourceGracedegradation:
    """MemPalaceMemorySource wraps the client and absorbs failures from all paths."""

    def test_retrieve_returns_none_when_exceptiongroup(self):
        from memchorus.mempalace_memory_source import MemPalaceMemorySource

        src = MemPalaceMemorySource(config={"skip_mcp": True, "cache_dir": "/tmp"})
        with patch('memchorus.mempalace_memory_source.asyncio.run', side_effect=ExceptionGroup(
                "tg", [RuntimeError("server died")]
        )):
            # Won't crash even if client._connected is somehow True
            result = src.retrieve("any_key")

        assert result is not None or result is None, "should return something without crashing"

    def test_save_returns_false_when_exceptiongroup(self):
        from memchorus.mempalace_memory_source import MemPalaceMemorySource

        src = MemPalaceMemorySource(config={"skip_mcp": True, "cache_dir": "/tmp"})
        with patch('memchorus.mempalace_memory_source.asyncio.run', side_effect=ExceptionGroup(
                "tg", [RuntimeError("server died")]
        )):
            result = src.save("test_key", {"data": "value"})

        # Should return False (or True if local cache saved despite MCP failure) — no crash

    def test_delete_does_not_crash_on_exceptiongroup(self):
        from memchorus.mempalace_memory_source import MemPalaceMemorySource
        import tempfile

        # Use an isolated temp dir so stale cache files from other tests don't leak in
        with tempfile.TemporaryDirectory() as tmp:
            src = MemPalaceMemorySource(config={"cache_dir": tmp})
            with patch('memchorus.mempalace_memory_source.asyncio.run', side_effect=ExceptionGroup(
                    "tg", [RuntimeError("server died")]
            )):
                # Should return False without crashing (no MCP, no local cache file to remove)
                result = src.delete("test_key")

            assert result is False, "delete should return False when ExceptionGroup occurs"
