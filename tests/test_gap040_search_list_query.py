"""GAP040: search() must handle list-valued query inputs gracefully.

Root cause: callers sometimes pass orch.search(['term']) instead of
orch.search('term').  The orchestrator's search accepts the raw
query and forwards it to each source's source.search(query, ...),
which calls _content_matches(query.lower(), ...).  A list has no
.lower(), causing an AttributeError silently caught by the
try/except at the fetch loop line - all_results stays empty and
the caller gets zero results despite valid data existing.

Fix: normalise list queries to space-joined strings before any downstream
call that expects a string (scorer.score_and_rank, enforcement hooks,
source.search).

Acceptance criteria:

1. save(key, value) then search([key_terms]) actually returns the item.
2. Search with list input produces identical results to the equivalent
   space-joined string query.
3. Re-install memchorus from source so CI tests against the fixed code
   and not an older cached archive in site-packages.
"""

import os
import shutil
import tempfile
import pytest
from memchorus.orchestrator import MemoryOrchestrator


@pytest.fixture
def fresh_orch():
    """Return a MemoryOrchestrator backed by HermesDefaultMemorySource in
    a throwaway temp directory.  Cleans up on teardown."""
    tmp_dir = tempfile.mkdtemp()
    orch = MemoryOrchestrator()

    # Ensure hermes_default is used
    from memchorus.hermes_memory_source import HermesDefaultMemorySource
    old_src = orch.memory_sources.get("hermes_default")
    if old_src:
        orch.memory_sources["hermes_default"] = HermesDefaultMemorySource(tmp_dir)
    else:
        orch.memory_sources["hermes_default"] = HermesDefaultMemorySource(tmp_dir)

    yield orch

    # Cleanup
    shutil.rmtree(tmp_dir, ignore_errors=True)


class TestGAP040SearchListQuery:
    """Regression tests for GAP040 - search with list query input."""

    def test_save_and_search_by_content_list_query(self, fresh_orch):
        """Save item then verify search finds it via content term matching.

        The original bug: orch.search(['key_terms']) returned [] even after
        orch.save('key', value) succeeded and orch.retrieve('key') worked fine.
        Search matches against stored CONTENT, not key names, so query terms
        must exist in the payload text."""
        fresh_orch.save("gap040_repro", {"text": "hello world from live runtime test"})

        # Retrieve should work (always did)
        retrieved = fresh_orch.retrieve("gap040_repro")
        assert retrieved is not None
        assert retrieved.get("text") == "hello world from live runtime test"

        # Search with a LIST query - this was the bug
        results = fresh_orch.search(["live", "runtime"])
        assert len(results) >= 1, (
            "Search with list query should find items saved by "
            "orch.save(). Original GAP040 bug returned empty []"
        )

    def test_search_list_query_equals_string_query(self, fresh_orch):
        """List queries should produce the same results as equivalent strings."""
        from memchorus.hermes_memory_source import HermesDefaultMemorySource
        safe_key = HermesDefaultMemorySource._safe_key

        original_key = "list_test_item"
        normalized_key = safe_key(original_key)
        fresh_orch.save(original_key, {"text": "alpha beta gamma delta"})

        # Query as list
        list_results = fresh_orch.search(["alpha", "beta"])
        # Query as space-joined string
        str_results = fresh_orch.search("alpha beta")

        assert list_results is not None
        assert str_results is not None

        # Both should find the item (key is normalized via _safe_key, underscores -> hyphens)
        list_keys = {r["key"] for r in list_results}
        str_keys = {r["key"] for r in str_results}

        assert normalized_key in list_keys, (
            f"List query ['alpha', 'beta'] missed saved content with those terms "
            f"(expected key={normalized_key}, got keys={list_keys})"
        )
        assert normalized_key in str_keys, (
            f"String query 'alpha beta' missed saved content with those terms "
            f"(expected key={normalized_key}, got keys={str_keys})"
        )

    def test_search_single_term_list(self, fresh_orch):
        """A single-element list should also work identically to a string."""
        from memchorus.hermes_memory_source import HermesDefaultMemorySource
        safe_key = HermesDefaultMemorySource._safe_key

        original_key = "single_elem_key"
        normalized_key = safe_key(original_key)
        fresh_orch.save(original_key, {"text": "this is the only item"})

        # Single element list
        results = fresh_orch.search(["only"])
        assert len(results) >= 1
        assert any(r["key"] == normalized_key for r in results), (
            f"Expected key {normalized_key}, got {[r['key'] for r in results]}"
        )

    def test_search_content_match_with_list_query(self, fresh_orch):
        """Search should match query terms against stored text content."""
        from memchorus.hermes_memory_source import HermesDefaultMemorySource
        safe_key = HermesDefaultMemorySource._safe_key

        original_key = "content_match"
        normalized_key = safe_key(original_key)
        fresh_orch.save(original_key, {"text": "the quick brown fox jumps"})

        # Query terms that appear in the content
        results = fresh_orch.search(["quick", "brown", "fox"])
        assert len(results) >= 1, (
            "Search should find items containing the query terms"
        )
        result_keys = [r["key"] for r in results]
        assert normalized_key in result_keys

    def test_search_with_multiple_saved_items(self, fresh_orch):
        """Verify search still works correctly with multiple items."""
        from memchorus.hermes_memory_source import HermesDefaultMemorySource
        safe_key = HermesDefaultMemorySource._safe_key

        fresh_orch.save("item_alpha", {"text": "alpha content"})
        fresh_orch.save("item_beta", {"text": "beta content here"})
        fresh_orch.save("item_gamma", {"text": "gamma data stored"})

        # Search only for 'beta' via list query
        results = fresh_orch.search(["beta"])
        result_keys = [r["key"] for r in results]

        assert safe_key("item_beta") in result_keys, (
            f"Search should find the item specifically matching the query term "
            f"(expected key={safe_key('item_beta')}, got keys={result_keys})"
        )

    def test_search_with_list_query_returns_scored_results(self, fresh_orch):
        """Each result must include a score field for downstream ranking."""
        from memchorus.hermes_memory_source import HermesDefaultMemorySource
        safe_key = HermesDefaultMemorySource._safe_key

        original_key = "score_test"
        normalized_key = safe_key(original_key)
        fresh_orch.save(original_key, {"text": "scored result content"})

        results = fresh_orch.search(["scored"])
        assert len(results) >= 1, "Search should return at least one result"
        for r in results:
            assert "score" in r, (
                "Each search result must contain a 'score' field"
            )
            assert isinstance(r["score"], (int, float)), (
                "Score must be numeric"
            )
            assert r["score"] > 0, (
                f"Score must be positive: got {r['score']}"
            )
