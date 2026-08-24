"""
Regression tests for category validation (BUG_2).

_validate_category_type_safe must reject unknown enum values that would otherwise
pollute memory payloads across all storage backends.
"""

import pytest
from memchorus.orchestrator import MemoryOrchestrator
from memchorus.auto_storage_engine import ALL_CATEGORIES, SignificanceCategory


class TestValidateCategoryTypeSafe:
    """Acceptance criteria from BUG_2 task."""

    def test_valid_category_returns_value(self):
        for cat in ALL_CATEGORIES:
            result = MemoryOrchestrator._validate_category_type_safe(cat)
            assert result == cat.value, f"{cat} should return its value string"

    def test_valid_category_string_returns_same(self):
        for cat in ALL_CATEGORIES:
            result = MemoryOrchestrator._validate_category_type_safe(cat.value)
            assert result == cat.value

    def test_none_returns_none(self):
        """None is an allowed sentinel — existing callers depend on this."""
        assert MemoryOrchestrator._validate_category_type_safe(None) is None

    def test_empty_string_returns_none(self):
        assert MemoryOrchestrator._validate_category_type_safe("") is None

    def test_whitespace_only_returns_none(self):
        assert MemoryOrchestrator._validate_category_type_safe("   ") is None

    def test_random_unknown_value_raises_valueerror(self):
        with pytest.raises(ValueError, match="unknown"):
            MemoryOrchestrator._validate_category_type_safe("random_cat")

    def test_lowercase_normalized_to_uppercase(self):
        """GH-122: 'learning' (lowercase) is normalised to 'LEARNING' — accepted, not rejected."""
        result = MemoryOrchestrator._validate_category_type_safe("learning")
        assert result == "LEARNING"

    def test_mixed_case_normalized_to_uppercase(self):
        """GH-122: 'Learning' (mixed) is normalised to 'LEARNING'."""
        result = MemoryOrchestrator._validate_category_type_safe("Learning")
        assert result == "LEARNING"

    def test_arbitrary_string_rejected(self):
        with pytest.raises(ValueError):
            MemoryOrchestrator._validate_category_type_safe("MY_CUSTOM_CATEGORY")

    def test_integer_raises_valueerror(self):
        with pytest.raises(ValueError):
            MemoryOrchestrator._validate_category_type_safe(42)


class TestValidateCategoriesInValue:
    """Integration behaviour — save() rejects payloads with bad categories."""

    @pytest.fixture
    def orchestrator(self):
        """Minimal orchestrator with no live sources — sufficient for validation tests."""
        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        return orch

    def test_nested_dict_with_invalid_categories_raises(self, orchestrator):
        payload = {"categories": ["LEARNING", "bad_cat"]}
        with pytest.raises(ValueError, match="unknown"):
            orchestrator._validate_categories_in_value(payload)

    def test_single_invalid_category_field_raises(self, orchestrator):
        payload = {"category": "UNKNOWN"}
        with pytest.raises(ValueError, match="unknown"):
            orchestrator._validate_categories_in_value(payload)

    def test_invalid_significance_field_raises(self, orchestrator):
        payload = {"significance": "GARBAGE"}
        with pytest.raises(ValueError, match="unknown"):
            orchestrator._validate_categories_in_value(payload)

    def test_all_valid_categories_passes(self, orchestrator):
        # No exception means success
        orchestrator._validate_categories_in_value({
            "categories": [c.value for c in ALL_CATEGORIES],
            "significance": "LEARNING",
        })

    def test_no_category_fields_passes(self, orchestrator):
        """A value dict that doesn't carry category keys at all is allowed."""
        orchestrator._validate_categories_in_value({"text": "hello", "count": 5})

    def test_nested_list_of_dicts_with_invalid_raises(self, orchestrator):
        payload = [
            {"categories": ["RESULT"]},
            {"significance": "NOT_A_CATEGORY"},
        ]
        with pytest.raises(ValueError, match="unknown"):
            orchestrator._validate_categories_in_value(payload)

    def test_none_category_ignored(self, orchestrator):
        """A category key whose value is None should be skipped."""
        orchestrator._validate_categories_in_value({"category": None})
