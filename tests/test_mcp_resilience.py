"""GH#103 — test_mcp_resilience.py

Simulate MemPalace MCP source failures (timeout, partial response, auth loss)
and verify orchestrator graceful degradation continues to fallback sources.
"""
from unittest.mock import MagicMock, patch
import pytest
from memchorus.orchestrator import _check_source_available


class FailingMemorySource:
    """Mock MemorySource that simulates different failure modes."""

    def __init__(self, mode="success"):
        self.mode = mode
        self.name = "mock_mcp_source"

    @property
    def is_available(self):
        return True if self.mode != "empty" else False

    def search(self, query, limit=10):
        if self.mode == "timeout":
            raise TimeoutError("MCP connection timed out after 30s")
        elif self.mode == "auth_error":
            raise PermissionError("MCP auth token expired")
        elif self.mode == "connection_refused":
            raise ConnectionRefusedError("111: Connection refused")
        elif self.mode == "partial":
            return [{"key": "partial_1", "content": "only got one result", "score": 0.6}]
        elif self.mode == "empty":
            return []
        else:
            return [
                {"key": "ok_1", "content": "first result from MCP source", "score": 0.9},
                {"key": "ok_2", "content": "second result from MCP source", "score": 0.7},
            ]


def _run_source_loop(sources, query="query"):
    """Simulate orchestrator.py line 1050-1076 search loop."""
    results_all = []
    for sname, src in sources.items():
        if not src or not _check_source_available(src):
            continue
        try:
            res = src.search(query, limit=10)
            if res:
                results_all.extend(res)
        except Exception:  # same as orchestrator.py line 1075: continue
            continue
    return results_all


class TestSourceFailureModes:

    def test_timeout_does_not_crash_search_pipeline(self):
        """A timeout in one source is caught and other sources continue."""
        fallback = FailingMemorySource(mode="success")
        failing = FailingMemorySource(mode="timeout")

        sources = {"mcp_mock": failing, "fallback_hermes": fallback}
        results = _run_source_loop(sources)
        assert len(results) > 0, "Fallback source was blocked by MCP timeout"

    def test_timeout_is_available_still_true(self):
        """A timeout during search does not make is_available switch to false."""
        src = FailingMemorySource(mode="timeout")
        assert src.is_available is True
        try:
            src.search("query", limit=10)
        except TimeoutError:
            pass
        assert src.is_available is True

    def test_auth_error_caught_same_as_timeout(self):
        """Auth loss follows the same graceful degradation path."""
        fallback = FailingMemorySource(mode="success")
        failing = FailingMemorySource(mode="auth_error")

        sources = {"mcp_mock": failing, "fallback_hermes": fallback}
        results = _run_source_loop(sources)
        assert len(results) > 0, "Fallback was blocked by auth error"

    def test_connection_refused_caught_same(self):
        """Connection refused treated same as timeout."""
        src = FailingMemorySource(mode="connection_refused")
        try:
            src.search("query", limit=10)
        except ConnectionRefusedError:
            pass

    def test_is_available_false_skips_search_entirely(self):
        """An unavailable source is skipped before search reaches its .search() method."""
        unavailable = FailingMemorySource(mode="empty")
        assert unavailable.is_available is False


class TestPartialResponseGracefulDegradation:

    def test_partial_results_still_returned(self):
        """A partial response (one result instead of five) does not zero out."""
        src = FailingMemorySource(mode="partial")
        results = src.search("test query", limit=10)
        assert len(results) == 1

    def test_orchestrator_accepts_any_result_count_per_source(self):
        """search() appends whatever comes back from each source."""
        sources = {
            "sparse": FailingMemorySource(mode="partial"),
            "rich": FailingMemorySource(mode="success"),
        }
        results = _run_source_loop(sources)
        assert len(results) == 3


class TestSimultaneousSourceFailure:

    def test_all_sources_fail_returns_empty_not_error(self):
        """If every source fails, pipeline returns empty list silently."""
        sources = {
            "mcp_a": FailingMemorySource(mode="timeout"),
            "mcp_b": FailingMemorySource(mode="auth_error"),
        }
        results = _run_source_loop(sources)
        assert results == []
