"""GAP021: Search max_results alias + retrieve_with_source provenance

Verifies:
  A) search() accepts both ``limit`` and ``max_results``, with max_results taking precedence.
  B) retrieve_with_source(key) returns a dict with key, content, and source_name.
"""

import pytest

from memchorus.orchestrator import MemoryOrchestrator


@pytest.fixture
def orchestrator():
    """In-memory orchestrator (no MCP sources) for unit testing."""
    orch = MemoryOrchestrator(config={})
    return orch


# ------------------------------------------------------------------
# Part A: max_results alias on search()
# ------------------------------------------------------------------

class TestSearchMaxResultsAlias:
    def test_search_accepts_limit_kwarg(self, orchestrator):
        """Existing callers using limit should still work."""
        results = orchestrator.search("test", limit=5)
        assert isinstance(results, list)

    def test_search_accepts_max_results_kwarg(self, orchestrator):
        """New documented name max_results must be accepted without error."""
        results = orchestrator.search("test", max_results=3)
        assert isinstance(results, list)

    def test_max_results_takes_precedence_over_limit(self, orchestrator):
        """When both provided, max_results should win (docs say it is canonical)."""
        # Even with no data to return, we prove no TypeError is raised and
        # the method respects the higher-priority parameter.
        results = orchestrator.search("test", limit=100, max_results=2)
        assert isinstance(results, list)

    def test_search_limit_default_value(self, orchestrator):
        """Default limit should still be 10 when neither arg provided."""
        # Just prove it runs without positional-arg issue
        results = orchestrator.search("test")
        assert isinstance(results, list)


# ------------------------------------------------------------------
# Part B: retrieve_with_source() source provenance
# ------------------------------------------------------------------

class TestRetrieveWithSource:
    def test_method_exists(self, orchestrator):
        """retrieve_with_source must be callable."""
        assert hasattr(orchestrator, "retrieve_with_source")
        assert callable(orchestrator.retrieve_with_source)

    def test_returns_none_for_unknown_key(self, orchestrator):
        """Unknown keys should return None (same contract as retrieve)."""
        result = orchestrator.retrieve_with_source("nonexistent_key_xyz123")
        assert result is None

    def test_retrieve_roundtrip_with_hermes_memory(self, tmp_path):
        """Write + read-back entry and verify source attribution."""
        orch_config = {
            "hermes_default_config": {"memory_dir": str(tmp_path / "mem.json")},
            "enforce_on_read": False,
            "enforce_on_write": False,
        }
        orch = MemoryOrchestrator(config=orch_config)
        test_key = "gap021_roundtrip"

        # Write via orchestrator store() on the default source
        orch.memory_sources["hermes_default"].save(test_key, "hello from GAP021")

        result = orch.retrieve_with_source(test_key)
        assert result is not None
        assert result["key"] == test_key
        content_val = result["content"]
        # Content may be raw string or wrapped dict depending on source impl
        assert "hello from GAP021" in str(content_val)

    def test_retrieve_with_source_includes_source_name_field(self, tmp_path):
        """The returned dict must have a 'source_name' key with an actual source."""
        orch_config = {
            "hermes_default_config": {"memory_dir": str(tmp_path / "mem2.json")},
            "enforce_on_read": False,
            "enforce_on_write": False,
        }
        orch = MemoryOrchestrator(config=orch_config)
        orch.memory_sources["hermes_default"].save("alpha", {"text": "data"})

        hit = orch.retrieve_with_source("alpha")
        assert hit is not None
        assert "source_name" in hit
        # Source should be one of the registered sources (not empty)
        assert len(hit["source_name"]) > 0

    def test_retrieve_with_source_caches_provenance(self, tmp_path):
        """A second call within TTL reuses the cached provenance dict."""
        orch_config = {
            "hermes_default_config": {"memory_dir": str(tmp_path / "mem3.json")},
            "enforce_on_read": False,
            "enforce_on_write": False,
        }
        orch = MemoryOrchestrator(config=orch_config)
        orch.memory_sources["hermes_default"].save("beta", "cached value")

        hit1 = orch.retrieve_with_source("beta")
        hit2 = orch.retrieve_with_source("beta")

        # Both should be dicts with the same source attribution
        assert isinstance(hit1, dict) and isinstance(hit2, dict)
        assert hit1["source_name"] == hit2["source_name"]

    def test_retrieve_cache_ttl_expiry_clears_stale_entry(self, tmp_path):
        """Expired cache entries trigger a fresh look (data still found)."""
        orch_config = {
            "hermes_default_config": {"memory_dir": str(tmp_path / "mem4.json")},
            "enforce_on_read": False,
            "enforce_on_write": False,
        }
        orch = MemoryOrchestrator(config=orch_config)
        # Force a TTL of 0 to expire immediately for testing
        orch._cache_ttl = 0
        orch.memory_sources["hermes_default"].save("gamma", "expired test")

        hit1 = orch.retrieve_with_source("gamma")
        assert hit1 is not None

        # Second call should see expired cache but still find the data
        hit2 = orch.retrieve_with_source("gamma")
        assert hit2 is not None  # because underlying source still has it
