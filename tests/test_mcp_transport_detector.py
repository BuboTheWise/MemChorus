"""Tests for _McpTransportDetector dual-shape config handling.

Covers both Shape A (legacy: single command string) and Shape B (Hermes native:
command path + args list) that the Hermes agent 3.x config.yaml format uses.
"""
import os
import sys
import tempfile
from pathlib import Path

# Ensure src on path so we test local changes
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import memchorus.mempalace_memory_source as mms


def _write_and_detect(content: str):
    """Write *content* to a temp YAML file and run detector on it.

    Returns the dict result (or None) after clearing cache.
    """
    f = tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False)
    f.write(content)
    f.close()
    try:
        mms._McpTransportDetector.clear_cache()
        result = mms._McpTransportDetector.detect(config_path=Path(f.name))
        return result
    finally:
        mms._McpTransportDetector.clear_cache()
        try:
            os.unlink(f.name)
        except OSError:
            pass


class TestShapeAHermesConfigSingleString:
    """Shape A: command is a single shell string that gets shlex.split()-ed."""

    def test_command_only_string_is_split(self):
        content = (
            "mcp_servers:\n"
            "  mempalace:\n"
            "    command: '/usr/bin/env python -m mempalace.test'\n"
        )
        result = _write_and_detect(content) or {}
        assert result.get("command") == "/usr/bin/env"
        assert "-m" in result.get("args", [])

    def test_missing_args_key_uses_shlex(self):
        """Absent 'args' key means Shape A — shlex split takes over."""
        content = (
            "mcp_servers:\n"
            "  mempalace:\n"
            f"    command: '{sys.executable} -m foo'\n"
        )
        result = _write_and_detect(content) or {}
        args = result.get("args", [])
        assert len(args) >= 2  # at least ['-m', 'foo']

    def test_dead_command_path_falls_back(self):
        """A non-existent command binary triggers fallback (returns None)."""
        content = (
            "mcp_servers:\n"
            "  mempalace:\n"
            "    command: '/nonexistent/binary -X'\n"
        )
        result = _write_and_detect(content)
        assert result is None


class TestShapeBHermesNativeSplitKeys:
    """Shape B: command path + args list (Hermes 3.x config format)."""

    def test_split_command_and_args(self):
        content = (
            "mcp_servers:\n"
            "  mempalace:\n"
            f"    command: '{sys.executable}'\n"
            "    args: ['-m', 'mempalace.mcp_server']\n"
        )
        result = _write_and_detect(content) or {}
        assert result.get("command") == sys.executable
        assert result.get("args") == ["-m", "mempalace.mcp_server"]

    def test_args_must_be_list_not_string(self):
        """If 'args' exists but is a string (wrong type), ignore it."""
        content = (
            "mcp_servers:\n"
            "  mempalace:\n"
            f"    command: '{sys.executable}'\n"
            "    args: '-m foo'\n"
        )
        result = _write_and_detect(content) or {}
        # Invalid args type → ignored entirely, so only command path remains
        assert result.get("command") == sys.executable
        assert result.get("args") == []

    def test_args_empty_list_still_works(self):
        content = (
            "mcp_servers:\n"
            "  mempalace:\n"
            f"    command: '{sys.executable}'\n"
            "    args: []\n"
        )
        result = _write_and_detect(content) or {}
        assert result.get("command") == sys.executable
        assert result.get("args") == []


class TestCacheBehavior:
    """The detector caches by config path to avoid redundant YAML parses."""

    def test_cache_returns_same_result(self):
        f = tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False)
        f.write(
            "mcp_servers:\n"
            "  mempalace:\n"
            f"    command: '{sys.executable}'\n"
            "    args: ['-m', 'x']\n"
        )
        f.close()
        try:
            mms._McpTransportDetector.clear_cache()
            path = Path(f.name)
            r1 = mms._McpTransportDetector.detect(config_path=path)
            r2 = mms._McpTransportDetector.detect(config_path=path)  # from cache
            assert r1 is not None and r1 == r2
        finally:
            mms._McpTransportDetector.clear_cache()
            try:
                os.unlink(f.name)
            except OSError:
                pass
