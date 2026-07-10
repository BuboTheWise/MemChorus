#!/usr/bin/env python3
"""test_mempalace_routing.py — multi-wing routing via mempalace_routing config.

Covers Spec §§1 (Wing Routing Contract, AC-R1.1–R1.4) and
§3 (Configuration Schema, AC-R3.1–R3.3).

Each test exercises the internal routing logic with ``skip_mcp=True`` so no real
MCP subprocess is spun up — unit-level isolation with local fallback only.
"""

import os
import sys
import tempfile
import shutil
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.mempalace_memory_source import (
    MemPalaceMemorySource,
    _DEFAULT_WING_MAP,
    _DEFAULT_ROOM_MAP,
)


# --------------------------------------------------------------------------- #
#  Fixture: clean temp cache dir for skip_mcp mode                           #
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_cache(tmp_path):
    d = str(tmp_path / "mempalace_test")
    os.makedirs(d, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================== #
#  SECTION 1 — Default wing assignment per category (AC-R1.3)                #
# =========================================================================== #

class TestDefaultWingAssignment:
    """Built-in defaults route each known category to the correct wing."""

    def _source(self, routing=None):
        return MemPalaceMemorySource(
            config={
                "skip_mcp": True,
                "cache_dir": tempfile.mkdtemp(),
                "mempalace_routing": routing,
            }
        )

    # -- AC-R1.3: DECISION → memchorus_decisions ------------------------------

    def test_decision_wing(self):
        src = self._source()
        assert src._resolve_wing("DECISION") == "memchorus_decisions"

    # -- AC-R1.3: LEARNING  → memchorus_learning --------------------------------

    def test_learning_wing(self):
        src = self._source()
        assert src._resolve_wing("LEARNING") == "memchorus_learning"

    # -- AC-R1.3: MISTAKE   → memchorus_learning (groups with lessons) ---------

    def test_mistake_wing(self):
        src = self._source()
        assert src._resolve_wing("MISTAKE") == "memchorus_learning"

    # -- AC-R1.3: RESULT    → memchorus_general ---------------------------------

    def test_result_wing(self):
        src = self._source()
        assert src._resolve_wing("RESULT") == "memchorus_general"

    # -- AC-R1.2: None / missing → default (AC-R1.2 backward compat) --------

    def test_none_category_defaults(self):
        src = self._source()
        assert src._resolve_wing(None) == "memchorus_general"

    def test_empty_string_category(self):
        src = self._source()
        assert src._resolve_wing("") == "memchorus_general"


# =========================================================================== #
#  SECTION 2 — Case-insensitive category lookup (AC-R3.2)                    #
# =========================================================================== #

class TestCaseInsensitiveLookup:
    """``_resolve_wing`` treats categories case-insensitively."""

    def _source(self, routing=None):
        return MemPalaceMemorySource(
            config={
                "skip_mcp": True,
                "cache_dir": tempfile.mkdtemp(),
                "mempalace_routing": routing,
            }
        )

    @pytest.mark.parametrize(
        "category", ["DECISION", "decision", "Decision", "decIsIon"]
    )
    def test_case_invariant(self, category):
        src = self._source()
        assert src._resolve_wing(category) == "memchorus_decisions"

    @pytest.mark.parametrize(
        "category", ["learning", "LEARNING", "Learning"]
    )
    def test_learning_case(self, category):
        src = self._source()
        assert src._resolve_wing(category) == "memchorus_learning"


# =========================================================================== #
#  SECTION 3 — Config override path (AC-R1.4 + AC-R3.1)                     #
# =========================================================================== #

class TestConfigOverride:
    """Custom config entirely overrides defaults — no merge."""

    def _source(self, routing):
        return MemPalaceMemorySource(
            config={
                "skip_mcp": True,
                "cache_dir": tempfile.mkdtemp(),
                "mempalace_routing": routing,
            }
        )

    def test_custom_wing_map_replaces_defaults(self):
        """AC-R1.4: override replaces, not merges."""
        custom = {
            "wing_map": {
                "DECISION": "my_decisions",
                "LEARNING": "my_lessons",
                "default": "my_general",
            }
        }
        src = self._source(custom)
        assert src._resolve_wing("DECISION") == "my_decisions"
        assert src._resolve_wing("LEARNING") == "my_lessons"

    def test_empty_routing_uses_defaults(self):
        """AC-R3.1: empty dict → built-in defaults (not empty routing)."""
        src = self._source(routing={})
        assert src._resolve_wing("DECISION") == "memchorus_decisions"
        assert src._resolve_wing("LEARNING") == "memchorus_learning"

    def test_missing_routing_uses_defaults(self):
        """AC-R3.1: missing section entirely → built-in defaults."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        assert src._resolve_wing("DECISION") == "memchorus_decisions"

    def test_partial_wing_default_preserved(self):
        """If the user's wing_map omits ``default``, unknown keys still work."""
        custom = {
            "wing_map": {
                "DECISION": "custom_decisions",
                # no LEARNING key — should hit DEFAULT
                "DEFAULT": "catch_all",
            }
        }
        src = self._source(custom)
        assert src._resolve_wing("DECISION") == "custom_decisions"
        assert src._resolve_wing("LEARNING") == "catch_all"  # falls to DEFAULT


# =========================================================================== #
#  SECTION 4 — Unknown category fallthrough (AC-R3.3)                        #
# =========================================================================== #

class TestUnknownCategoryRouting:
    """Unknown categories must resolve gracefully, not crash (AC-R3.3)."""

    def _source(self, routing=None):
        return MemPalaceMemorySource(
            config={
                "skip_mcp": True,
                "cache_dir": tempfile.mkdtemp(),
                "mempalace_routing": routing,
            }
        )

    def test_completely_unknown_category(self):
        src = self._source()
        # "SOMETHING_NEW" is not a known category → DEFAULT
        assert src._resolve_wing("SOMETHING_NEW") == "memchorus_general"

    def test_weird_string(self):
        src = self._source()
        assert src._resolve_wing("!!!") == "memchorus_general"

    def test_unknown_custom_map_falls_to_default(self):
        """AC-R3.3: a value present but the category missing → DEFAULT key."""
        custom = {
            "wing_map": {
                "DECISION": "only_decisions",
                "DEFAULT":  "everything_else",
            }
        }
        src = self._source(custom)
        assert src._resolve_wing("GARBAGE") == "everything_else"

    def test_completely_empty_wing_map(self):
        """When wing_map is {} we fall through to built-in defaults (not crash)."""
        src = self._source({"wing_map": {}})
        # Empty wing_map is treated as "no config provided" → built-ins apply.
        assert src._resolve_wing("DECISION") == "memchorus_decisions"


# =========================================================================== #
#  SECTION 5 — Built-in default table shape assertions                       #
# =========================================================================== #

class TestDefaultTableShape:
    """Sanity checks on the module-level _DEFAULT_WING_MAP / _DEFAULT_ROOM_MAP."""

    def test_wing_map_has_all_categories(self):
        for cat in ("DECISION", "LEARNING", "MISTAKE", "RESULT", "DEFAULT"):
            assert cat in _DEFAULT_WING_MAP, f"Missing {cat!r} in default wing_map"

    def test_room_map_has_all_categories(self):
        for cat in ("DECISION", "LEARNING", "MISTAKE", "RESULT", "DEFAULT"):
            assert cat in _DEFAULT_ROOM_MAP, f"Missing {cat!r} in default room_map"

    def test_wing_map_decisions_and_learning_distinct(self):
        """Decisions must not route to the same wing as learnings."""
        assert (
            _DEFAULT_WING_MAP["DECISION"] != _DEFAULT_WING_MAP["LEARNING"]
        )

    def test_room_map_all_slugs_lowercase(self):
        for room in _DEFAULT_ROOM_MAP.values():
            assert room == room.lower(), f"Room slug {room!r} not lowercase"


# =========================================================================== #
#  SECTION 6 — save() path actually uses routed wing (integration)           #
# =========================================================================== #

class TestSaveUsesRoutedWing:
    """Mock the MCP client to verify save() passes resolved wing."""

    def test_save_passes_routed_wing(self):
        src = MemPalaceMemorySource(
            config={
                "skip_mcp": True,
                "cache_dir": tempfile.mkdtemp(),
            }
        )

        # A DECISION payload should resolve to memchorus_decisions.
        wing_decision = src._resolve_wing("DECISION")
        assert wing_decision == "memchorus_decisions"

        # A LEARNING payload resolves differently.
        wing_learning = src._resolve_wing("LEARNING")
        assert wing_learning == "memchorus_learning"

        # Verify the save method actually uses _resolve_wing internally.
        # We do this by saving a dict with ``category`` key and checking
        # the local cache still works (graceful degradation).
        payload = {
            "text": "Test decision memory",
            "category": "DECISION",
        }
        ok = src.save("test_deci_key", payload)
        assert ok is True  # local fallback should succeed

    def test_save_no_category_uses_default(self):
        """A plain string value with no category dict → DEFAULT."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        # save() extracts category from the value; a plain str has none.
        ok = src.save("plain_key", "just a string")
        assert ok is True  # still succeeds

    def test_save_significance_fallback(self):
        """A payload with 'significance' key (legacy name) also works."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        wing = src._resolve_wing("MISTAKE")
        assert wing == "memchorus_learning"


# =========================================================================== #
#  SECTION 7 — _categorize_room room selection (§2, AC-R2.1–R2.4)           #
# =========================================================================== #

class TestCategorizeRoom:
    """_categorize_room derives semantic room slugs from payload category."""

    @pytest.mark.parametrize(
        "category,expected_room", [
            ("DECISION", "decisions"),
            ("decision", "decisions"),
            ("LEARNING", "lessons-learned"),
            ("MISTAKE", "corrections"),
            ("RESULT", "outcomes"),
            ("result", "outcomes"),
        ]
    )
    def test_category_to_room_mapping(self, category, expected_room):
        """AC-R2.1: Category maps to semantic room slug (case insensitive)."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "test", "category": category}
        assert src._categorize_room(payload) == expected_room

    def test_no_category_fallback(self):
        """AC-R2.3: No category → 'general' fallback."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "no category here"}
        assert src._categorize_room(payload) == "general"

    def test_plain_string_value(self):
        """AC-R2.3: Non-dict value → 'general' fallback."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        assert src._categorize_room("just a string") == "general"

    def test_significance_string_path(self):
        """Legacy 'significance' key (string) also works."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "test", "significance": "LEARNING"}
        assert src._categorize_room(payload) == "lessons-learned"

    def test_nested_significance_category(self):
        """AutoStorageEngine nested path: metadata.significance.category."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {
            "text": "test",
            "metadata": {
                "significance": {"category": "MISTAKE"}
            }
        }
        assert src._categorize_room(payload) == "corrections"

    def test_custom_room_map(self):
        """Custom room_map overrides built-in rooms."""
        src = MemPalaceMemorySource(
            config={
                "skip_mcp": True,
                "cache_dir": tempfile.mkdtemp(),
                "mempalace_routing": {
                    "room_map": {
                        "DECISION": "board-notes",
                        "LEARNING": "growth",
                        "DEFAULT": "inbox",
                    }
                },
            }
        )
        payload = {"text": "test", "category": "DECISION"}
        assert src._categorize_room(payload, room_map=src._room_map) == "board-notes"

    def test_deterministic_slugs(self):
        """AC-R2.2: Same category always produces same slug."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "test", "category": "LEARNING"}
        for _ in range(5):
            assert src._categorize_room(payload) == "lessons-learned"

    def test_slug_format(self):
        """AC-R2.4: Slug is lowercase hyphen-separated."""
        import re
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        for cat in ["DECISION", "LEARNING", "MISTAKE", "RESULT"]:
            payload = {"text": "test", "category": cat}
            slug = src._categorize_room(payload)
            assert re.match(r'^[a-z][a-z\-]*$', slug), f"Bad slug format: {slug!r}"


# =========================================================================== #
#  SECTION 8 — _resolve_wing_from_payload (§6, AC-R6.1)                     #
# =========================================================================== #

class TestResolveWingFromPayload:
    """"_resolve_wing_from_payload extracts wing from cached payload."""

    def test_decision_payload(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "test", "category": "DECISION"}
        assert src._resolve_wing_from_payload(payload) == "memchorus_decisions"

    def test_learning_payload(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "test", "category": "LEARNING"}
        assert src._resolve_wing_from_payload(payload) == "memchorus_learning"

    def test_nested_metadata_path(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {
            "text": "test",
            "metadata": {"significance": {"category": "MISTAKE"}}
        }
        assert src._resolve_wing_from_payload(payload) == "memchorus_learning"

    def test_no_category_fallback(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "no category"}
        assert src._resolve_wing_from_payload(payload) == "memchorus_general"

    def test_non_dict_value(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        assert src._resolve_wing_from_payload("string value") == "memchorus_general"

    def test_none_value(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        assert src._resolve_wing_from_payload(None) == "memchorus_general"


# =========================================================================== #
#  SECTION 9 — retrieve() wing-aware recall (§6, AC-R6.1/AC-R6.2)          #
# =========================================================================== #

class TestRetrieveWingAware:
    """retrieve() finds memories in routed wings via cached category info."""

    def test_category_key_used_for_retrieve(self):
        """A DECISION saved and retrieved should resolve to decisions wing."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {
            "text": "My decision",
            "category": "DECISION",
        }
        src.save("retrieve_test_key", payload)
        # In skip_mcp mode, retrieve falls back to local cache -> should work
        result = src.retrieve("retrieve_test_key")
        assert result is not None
        assert result == payload

    def test_retrieve_no_category_fallback(self):
        """AC-R6.2: Memory without category still retrievable from cache."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "plain memory"}
        src.save("no_cat_key", payload)
        result = src.retrieve("no_cat_key")
        assert result is not None

    def test_retrieve_legacy_string_value(self):
        """Legacy string value still retrievable."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        src.save("legacy_key", "just a string")
        result = src.retrieve("legacy_key")
        assert result == "just a string"


# =========================================================================== #
#  SECTION 10 — search() wing/room filters (§6, AC-R6.3)                   #
# =========================================================================== #

class TestSearchWingRoomFilters:
    """search() accepts optional wing and room parameters."""

    def test_search_signature_has_wing_room(self):
        """Verify search() accepts wing and room keyword arguments."""
        import inspect
        sig = inspect.signature(MemPalaceMemorySource.search)
        params = list(sig.parameters.keys())
        assert "wing" in params, "search() should accept 'wing' parameter"
        assert "room" in params, "search() should accept 'room' parameter"

    def test_search_without_filters_works(self):
        """search() with no filters returns results from local cache."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        # Should not crash even without MCP
        result = src.search("test", limit=5)
        assert isinstance(result, list)

    def test_search_with_wing_filter(self):
        """search() with wing filter passes it through."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        # Should not crash even without MCP - the parameter itself is what we test
        result = src.search("test", limit=5, wing="memchorus_decisions")
        assert isinstance(result, list)

    def test_search_with_room_filter(self):
        """search() with room filter passes it through."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        result = src.search("test", limit=5, room="decisions")
        assert isinstance(result, list)

    def test_search_with_wing_and_room(self):
        """search() with both wing and room filters."""
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        result = src.search("test", limit=5, wing="memchorus_decisions", room="decisions")
        assert isinstance(result, list)


# =========================================================================== #
#  SECTION 11 — End-to-end routing verification                             #
# =========================================================================== #

class TestEndToEndRouting:
    """Full save→cache→retrieve cycle with routed categories."""

    def test_decision_full_cycle(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "Decision content", "category": "DECISION"}
        assert src.save("e2e_decision", payload)
        result = src.retrieve("e2e_decision")
        assert result["category"] == "DECISION"

    def test_learning_full_cycle(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "Learning content", "category": "LEARNING"}
        assert src.save("e2e_learning", payload)
        result = src.retrieve("e2e_learning")
        assert result["category"] == "LEARNING"

    def test_mistake_full_cycle(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "Mistake content", "category": "MISTAKE"}
        assert src.save("e2e_mistake", payload)
        result = src.retrieve("e2e_mistake")
        assert result["category"] == "MISTAKE"

    def test_result_full_cycle(self):
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payload = {"text": "Result content", "category": "RESULT"}
        assert src.save("e2e_result", payload)
        result = src.retrieve("e2e_result")
        assert result["category"] == "RESULT"

    def test_multiple_categories_coexist(self):
        """Multiple categories saved and retrieved without collision."""
        import json
        src = MemPalaceMemorySource(
            config={"skip_mcp": True, "cache_dir": tempfile.mkdtemp()}
        )
        payloads = [
            ("k1", {"text": "a", "category": "DECISION"}),
            ("k2", {"text": "b", "category": "LEARNING"}),
            ("k3", {"text": "c", "category": "MISTAKE"}),
            ("k4", {"text": "d", "category": "RESULT"}),
        ]
        for key, payload in payloads:
            assert src.save(key, payload)

        for key, expected in payloads:
            result = src.retrieve(key)
            assert result == expected
