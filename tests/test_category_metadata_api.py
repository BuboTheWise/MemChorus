"""
Tests for GH-122: MemoryOrchestrator.save() now accepts category= and metadata= parameters.
"""
import pytest

from memchorus import MemoryOrchestrator
from memchorus.relevance_engine import RankedResult


class _MockSource:
    """Minimal storage backing that captures saves and supports delete."""

    def __init__(self):
        self.data = {}
        self.available = True

    @property
    def is_available(self) -> bool:
        return self.available

    @property
    def name(self):
        return "mock"

    def save(self, key, value):
        self.data[key] = value
        return True

    def search(self, query="") -> list:
        results = []
        for k, v in self.data.items():
            content_str = str(v) + " " + str(k)
            for term in query.split():
                if term.lower() in content_str.lower():
                    rr = RankedResult(key=k, content=v, source="mock", score=0.8)
                    results.append(rr)
                    break
        return results

    def delete(self, key):
        if self.data:
            self.data.pop(key, None)
        return True


class TestSaveCategoryParam:

    def _make_orch(self):
        orch = MemoryOrchestrator(config={"skip_init_sources": True})
        src = _MockSource()
        orch.register_source(src)  # Takes source object only; reads src.name internally
        return orch, src

    def test_category_string_accepted(self):
        orch, src = self._make_orch()
        orch.save("test/1", {"content": "hello"}, category="decision")
        stored = next(
            (v for k, v in src.data.items() if k == "test/1"), None)
        assert isinstance(stored, dict)
        assert "categories" in stored
        # Category is uppercased by validator
        assert "DECISION" in stored["categories"]

    def test_metadata_category_accepted(self):
        orch, src = self._make_orch()
        orch.save("test/2", {"content": "world"},
                  metadata={"category": "learning", "provenance": "user_note"})
        stored = next(
            (v for k, v in src.data.items() if k == "test/2"), None)
        assert isinstance(stored, dict)
        assert "LEARNING" in stored.get("categories", [])
        assert stored.get("provenance") == "user_note"

    def test_category_param_takes_precedence(self):
        orch, src = self._make_orch()
        orch.save("test/3", {"content": "data"},
                  category="decision", metadata={"category": "learning"})
        stored = next(
            (v for k, v in src.data.items() if k == "test/3"), None)
        assert "DECISION" in stored.get("categories", [])
        assert "LEARNING" not in stored.get("categories", [])

    def test_invalid_category_raises(self):
        orch, _ = self._make_orch()
        with pytest.raises(ValueError, match="unknown"):
            orch.save("test/4", {"content": "x"}, category="NONEXISTENT")

    def test_no_category_preserves_original_value(self):
        orch, src = self._make_orch()
        original = {"simple": "value"}
        orch.save("test/5", original)
        stored = next(
            (v for k, v in src.data.items() if k == "test/5"), None)
        assert stored == original

    def test_metadata_only_enriches(self):
        orch, src = self._make_orch()
        orch.save("test/6", {"content": "test"},
                  metadata={"provenance": "manual"})
        stored = next(
            (v for k, v in src.data.items() if k == "test/6"), None)
        assert stored.get("provenance") == "manual"
        assert stored.get("content") == "test"

    def test_category_wraps_scalar(self):
        orch, src = self._make_orch()
        orch.save("test/7", "just_a_string", category="decision")
        # The scalar should be wrapped in a dict with _content key
        matching = [v for k, v in src.data.items() if k == "test/7"]
        found_wrapped = any(
            isinstance(v, dict) and v.get("_content") == "just_a_string"
            for v in matching)
        assert found_wrapped, f"No wrapped scalar found; data={src.data}"

    def test_category_returns_uppercase(self):
        orch = MemoryOrchestrator()
        cat_key = orch._validate_category_type_safe("decision")
        assert cat_key == "DECISION"

    def test_empty_category_ignored(self):
        orch, src = self._make_orch()
        orch.save("test/8", {"content": "x"}, category="  ")
        stored = next(
            (v for k, v in src.data.items() if k == "test/8"), None)
        assert stored == {"content": "x"}
