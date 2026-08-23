#!/usr/bin/env python3
"""
test_mcp_resilience.py - MCP failure simulation for MemoryOrchestrator.

Mock MemPalace MCP responses simulating timeout / empty / partial response.
Verify graceful degradation + fallback source takeover without error
propagation to the hook caller.

Uses pytest fixtures for mock sources -- no real MCP connection required.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from typing import List, Dict, Any, Optional

from memchorus.orchestrator import MemoryOrchestrator


# --------------------------------------------------------------------------- #
#  Fixtures: mock MCP source that can be configured to fail                      #
# --------------------------------------------------------------------------- #


class MockMcpMemorySource:
    """A configurable mock memory source simulating MemPalace MCP behaviour.

    Configurable via ``fail_mode``:
      - None             -- normal operation (happy path)
      - "timeout"        -- raises TimeoutError on search
      - "empty"          -- returns empty results on search
      - "partial"        -- only returns first result, drops the rest
      - "exception"      -- raises a generic Exception on search
    """

    def __init__(self, name="mempalace", fail_mode=None):
        self.name = name
        self.fail_mode = fail_mode
        self._store = {}
        self._available = True

    @property
    def is_available(self):
        return self._available

    def save(self, key: str, value: Any) -> bool:
        if self.fail_mode == "timeout":
            raise TimeoutError("MCP call timed out")
        if self.fail_mode == "exception":
            raise Exception("Simulated MCP failure")
        self._store[key] = value
        return True

    def retrieve(self, key: str) -> Optional[Any]:
        # retrieve generally still works even when search fails
        return self._store.get(key)

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        if self.fail_mode == "timeout":
            raise TimeoutError("MCP call timed out")
        if self.fail_mode == "exception":
            raise Exception("Simulated MCP failure")
        if self.fail_mode == "empty":
            return []

        # Gather matching results from _store
        results = []
        q_low = query.lower()
        for key, value in self._store.items():
            match = q_low in key.lower() or q_low in str(value).lower()
            if match:
                entry = {
                    "key": key,
                    "content": value,
                    "source": self.name,
                    "score": 0.7,
                }
                results.append(entry)

        if self.fail_mode == "partial" and len(results) > 1:
            results = results[:1]

        return results[:limit]

    def get_source_info(self) -> Dict[str, Any]:
        return {"name": self.name, "type": "mock_mcp"}


@pytest.fixture
def hermes_mock():
    """Simple in-memory dict-backed MemorySource stand-in for Hermes."""
    store = {}

    class HermesMock:
        name = "hermes_default"

        @property
        def is_available(self):
            return True

        def save(self, key, value):
            store[key] = value
            return True

        def retrieve(self, key):
            return store.get(key)

        def search(self, query, limit=10):
            q_low = query.lower()
            hits = []
            for k, v in store.items():
                if q_low in k.lower() or q_low in str(v).lower():
                    hits.append({
                        "key": k,
                        "content": v,
                        "source": self.name,
                        "score": 0.8,
                    })
            return hits[:limit]

        def get_source_info(self):
            return {"name": self.name}

    return HermesMock()


@pytest.fixture
def mock_mcp():
    """Default MCP source (no failure mode), pre-populated."""
    src = MockMcpMemorySource(name="mempalace")
    src._store["alpha_doc"] = {"text": "Alpha document content"}
    src._store["bravo_report"] = {"text": "Bravo quarterly report"}
    src._store["charlie_log"] = {"text": "Charlie system event log"}
    return src


@pytest.fixture
def orch(hermes_mock, mock_mcp):
    """Build orchestrator with two mock sources."""
    orch_instance = MemoryOrchestrator({
        "default_source": "hermes_default",
        "hermes_default_config": {},
        "mempalace_config": {"skip_mcp": True},
    })
    orch_instance.memory_sources["hermes_default"] = hermes_mock
    orch_instance.memory_sources["mempalace"] = mock_mcp
    return orch_instance


# --------------------------------------------------------------------------- #
#  Tests: MCP resilience - graceful degradation                                 #
# --------------------------------------------------------------------------- #

class TestMcpResilience:
    """Graceful degradation when MCP fails."""

    @pytest.mark.parametrize(
        "fail_mode,description",
        [
            ("timeout", "TimeoutError from MCP"),
            ("exception", "Generic Exception from MCP"),
            ("empty", "Empty results from MCP"),
            ("partial", "Partial results from MCP"),
            (None, "No failure -- happy path"),
        ],
    )
    def test_search_does_not_propagate_errors(self, hermes_mock, fail_mode, description):
        """orchestrator.search() never propagates MCP exceptions to caller."""
        mcp = MockMcpMemorySource(name="mempalace", fail_mode=fail_mode)
        mcp._store["test_key"] = {"val": "test_data"}

        hermes_mock.save("h_test_key", {"val": "hermes_data"})

        orch_instance = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["hermes_default"] = hermes_mock
        orch_instance.memory_sources["mempalace"] = mcp

        # Should not raise regardless of fail_mode
        try:
            results = orch_instance.search("test")
        except Exception as exc:
            pytest.fail(
                f"Error propagated through orchestrator for {fail_mode} ({description}): {exc}"
            )

        assert isinstance(results, list)
        if fail_mode in ("timeout", "exception"):
            # MCP source drops its results; hermes_default should fill the gap
            has_hermes = any(r["source"] == "hermes_default" for r in results)
            assert has_hermes, f"No hermes_default fallback when MCP {fail_mode}"

    def test_save_succeeds_via_fallback_on_mcp_timeout(self, hermes_mock):
        """save() persists via hermes_default when MCP times out."""
        mcp = MockMcpMemorySource(name="mempalace", fail_mode="timeout")

        orch_instance = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["hermes_default"] = hermes_mock
        orch_instance.memory_sources["mempalace"] = mcp

        result = orch_instance.save("save_fallback_key", {"info": "should persist"})
        assert result is True

        retrieved = orch_instance.retrieve("save_fallback_key")
        assert retrieved is not None
        assert retrieved["info"] == "should persist"

    @pytest.mark.parametrize(
        "fail_mode", ["timeout", "exception", "empty", "partial", None]
    )
    def test_retrieve_returns_data_regardless_of_mcp_mode(self, hermes_mock, fail_mode):
        """retrieve() finds data from available sources no matter MCP state."""
        mcp = MockMcpMemorySource(name="mempalace", fail_mode=fail_mode)
        key = "resilient_retrieve"
        hermes_mock.save(key, {"from_hermes": True})

        orch_instance = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["hermes_default"] = hermes_mock
        orch_instance.memory_sources["mempalace"] = mcp

        try:
            result = orch_instance.retrieve(key)
        except Exception as exc:
            pytest.fail(f"retrieve() exploded on fail_mode={fail_mode}: {exc}")

        assert result is not None
        assert result["from_hermes"] is True

    @pytest.mark.parametrize(
        "fail_mode", ["timeout", "exception"]
    )
    def test_orchestrator_still_available_when_mcp_broken(self, fail_mode):
        """is_available() returns True if at least one source works."""
        mcp = MockMcpMemorySource(name="mempalace", fail_mode=fail_mode)

        class SimpleHermes:
            name = "hermes_default"
            _store = {}

            @property
            def is_available(self):
                return True

            def save(self, k, v):
                self._store[k] = v
                return True

            def retrieve(self, k):
                return self._store.get(k)

            def search(self, q, limit=10):
                hits = []
                ql = q.lower()
                for kk, vv in self._store.items():
                    if ql in kk.lower():
                        hits.append({
                            "key": kk, "content": vv,
                            "source": "hermes_default", "score": 0.8,
                        })
                return hits[:limit]

            def get_source_info(self):
                return {"name": "hermes_default"}

        orch_instance = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["hermes_default"] = SimpleHermes()
        orch_instance.memory_sources["mempalace"] = mcp

        assert orch_instance.is_available() is True

    def test_search_empty_mcp_returns_hermes_data(self, hermes_mock):
        """When MCP returns empty results, hermes_default fills in the gap."""
        mcp = MockMcpMemorySource(name="mempalace", fail_mode="empty")
        key = "hermes_filler"
        hermes_mock.save(key, {"content": "filler data from hermes"})

        orch_instance = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["hermes_default"] = hermes_mock
        orch_instance.memory_sources["mempalace"] = mcp

        results = orch_instance.search("filler")
        assert len(results) > 0, "Expected hermes_default to fill gaps when MCP is empty"
        assert any(r.get("source") == "hermes_default" for r in results)


class TestMcpPartialResilience:
    """Specific tests around partial MCP data loss."""

    def test_partial_results_combined_with_hermes(self, hermes_mock):
        """Partial MCP + full hermes = combined unique results without crash."""
        mcp = MockMcpMemorySource(name="mempalace", fail_mode="partial")
        mcp._store["alpha"] = {"data": "A"}
        mcp._store["bravo"] = {"data": "B"}
        hermes_mock.save("alpha", {"data": "H-A"})

        orch_instance = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["hermes_default"] = hermes_mock
        orch_instance.memory_sources["mempalace"] = mcp

        # No crash; results may be partial but that is the graceful path
        results = orch_instance.search("a")
        assert isinstance(results, list)

    @pytest.mark.parametrize(
        "fail_modes", [
            ["timeout", None],
            [None, "exception"],
            ["empty", "empty"],
        ],
    )
    def test_sequential_searches_survive(self, hermes_mock, fail_modes):
        """Multiple sequential searches succeed even as failure mode changes."""
        mcp = MockMcpMemorySource(name="mempalace")
        mcp._store["persistent_key"] = {"data": "always here"}

        orch_instance = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["hermes_default"] = hermes_mock
        orch_instance.memory_sources["mempalace"] = mcp

        hermes_mock.save("persist_key_h", {"data": "in hermes too"})

        for mode in fail_modes:
            mcp.fail_mode = mode
            try:
                orch_instance.search("persist")
            except Exception as exc:
                pytest.fail(f"Search crashed at mode={mode}: {exc}")


# --------------------------------------------------------------------------- #
#  Hook-caller isolation tests                                                  #
# --------------------------------------------------------------------------- #

class TestHookCallerIsolation:
    """MCP errors never leak through to the hook caller layer."""

    def test_hook_search_no_propagation(self, hermes_mock):
        """When hooks call orchestrator.search(), MCP failures are absorbed."""
        mcp = MockMcpMemorySource(name="mempalace", fail_mode="timeout")
        hermes_mock.save("hook_context", {"context": "relevant memory"})

        orch_instance = MemoryOrchestrator({
            "default_source": "hermes_default",
            "hermes_default_config": {},
            "mempalace_config": {"skip_mcp": True},
        })
        orch_instance.memory_sources["hermes_default"] = hermes_mock
        orch_instance.memory_sources["mempalace"] = mcp

        # Simulate hook calling search - should succeed from hermes_default
        results = []
        try:
            results = orch_instance.search("context")
        except Exception as exc:
            pytest.fail(f"Hook caller received error: {exc}")

        assert isinstance(results, list)
        # At minimum the hook caller got a result list instead of an exception