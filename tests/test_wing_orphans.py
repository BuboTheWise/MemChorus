"""Tests for memchorus.wing_orphans (issue #305 core).

Covers the pure classification/planning layer: timestamp detection,
canonical target resolution, per-value classification, the row-weighted
orphan report, the reassignment plan, and the config-flag helpers.
All tests are hermetic — explicit ``valid_wings`` sets are passed in so no
test depends on the local profile's memchorus.yaml.
"""
import pytest

from memchorus import wing_orphans

#: A small in-namespace wing set standing in for a profile's namespace.
VALID = frozenset(
    {"cthugha_LEARNING", "cthugha_DECISION", "memchorus_general", "general"}
)


# ---------------------------------------------------------------------------
# is_timestamp_wing
# ---------------------------------------------------------------------------
class TestIsTimestampWing:
    @pytest.mark.parametrize(
        "ts",
        [
            "2026-11-20T10:17:03",
            "2026-11-20 10:17:03",
            "2026-11-20T10:17",
            "2026-11-20T10",
            "2026-11-20",
            "20261120",
            "2026-11-20T10:17:03.123",
            "2026-11-20T10:17:03Z",
        ],
    )
    def test_timestamps_detected(self, ts):
        assert wing_orphans.is_timestamp_wing(ts) is True

    @pytest.mark.parametrize(
        "bad",
        [
            "v3",
            "cthugha",
            "cthugha_LEARNING",
            "mcp",
            "session_meta",
            "2026",            # too short
            "abcd-11-20",      # does not start with a year
            "learning-659c71ed2703042e",
            "",
            None,
            "2026-99-20",      # month out of range
        ],
    )
    def test_non_timestamps_rejected(self, bad):
        assert wing_orphans.is_timestamp_wing(bad) is False


# ---------------------------------------------------------------------------
# canonical_target
# ---------------------------------------------------------------------------
class TestCanonicalTarget:
    def test_exact_valid_returned_as_is(self):
        assert wing_orphans.canonical_target("cthugha_LEARNING", VALID) == "cthugha_LEARNING"
        assert wing_orphans.canonical_target("memchorus_general", VALID) == "memchorus_general"

    def test_case_variant_normalised(self):
        assert wing_orphans.canonical_target("cthugha_learning", VALID) == "cthugha_LEARNING"
        assert wing_orphans.canonical_target("MEMCHORUS_GENERAL", VALID) == "memchorus_general"

    @pytest.mark.parametrize("null", [None, "", "None", "<none>", "NULL"])
    def test_null_tokens_fall_back(self, null):
        assert wing_orphans.canonical_target(null, VALID) == "memchorus_general"

    def test_unrecognized_falls_back_to_shared_wing(self):
        assert wing_orphans.canonical_target("v3", VALID) == "memchorus_general"
        assert wing_orphans.canonical_target("mcp", VALID) == "memchorus_general"

    def test_empty_valid_set_returns_empty_string(self):
        assert wing_orphans.canonical_target("v3", frozenset()) == ""
        assert wing_orphans.canonical_target(None, frozenset()) == ""

    def test_fallback_missing_picks_first_sorted(self):
        small = frozenset({"general", "cthugha_LEARNING"})
        assert "memchorus_general" not in small
        assert wing_orphans.canonical_target("v3", small) == "cthugha_LEARNING"


# ---------------------------------------------------------------------------
# classify_wing_value
# ---------------------------------------------------------------------------
class TestClassifyWingValue:
    def test_valid_wing_is_not_an_orphan(self):
        assert wing_orphans.classify_wing_value("cthugha_LEARNING", VALID) is None
        assert wing_orphans.classify_wing_value("general", VALID) is None

    def test_empty_is_category_empty(self):
        info = wing_orphans.classify_wing_value(None, VALID)
        assert info is not None
        assert info.category == wing_orphans.ORPHAN_EMPTY
        assert info.wing == ""
        assert info.suggested_target == "memchorus_general"
        assert wing_orphans.classify_wing_value("", VALID).category == wing_orphans.ORPHAN_EMPTY

    def test_none_string_is_category_empty(self):
        info = wing_orphans.classify_wing_value("<None>", VALID)
        assert info is not None and info.category == wing_orphans.ORPHAN_EMPTY

    def test_timestamp_category(self):
        info = wing_orphans.classify_wing_value("2026-11-20T10:17:03", VALID)
        assert info is not None
        assert info.category == wing_orphans.ORPHAN_TIMESTAMP
        assert info.suggested_target == "memchorus_general"

    def test_case_variant_category(self):
        info = wing_orphans.classify_wing_value("cthugha_learning", VALID)
        assert info is not None
        assert info.category == wing_orphans.ORPHAN_CASE_VARIANT
        assert info.suggested_target == "cthugha_LEARNING"

    def test_unrecognized_category(self):
        info = wing_orphans.classify_wing_value("v3", VALID)
        assert info is not None
        assert info.category == wing_orphans.ORPHAN_UNRECOGNIZED
        assert info.suggested_target == "memchorus_general"


# ---------------------------------------------------------------------------
# orphan_report + plan_reassignment
# ---------------------------------------------------------------------------
class TestOrphanReport:
    def census(self):
        # Mirrors the shape of #305's live cthugha data (row-weighted).
        return {
            "cthugha_LEARNING": 7,
            "cthugha_DECISION": 3,
            "memchorus_general": 1,
            "v3": 14,
            None: 17,
            "": 11,
            "2026-11-20T10:17:03": 3,
            "cthugha_learning": 2,
        }

    def test_totals_and_split(self):
        report = wing_orphans.orphan_report("cthugha", self.census(), VALID)
        assert report.total_rows == 58
        assert report.mapped_rows == 11
        assert report.orphan_rows == 47
        assert report.is_clean is False
        assert round(report.orphan_pct, 1) == 81.0  # matches issue #305 headline

    def test_orphans_sorted_by_row_count_desc(self):
        report = wing_orphans.orphan_report("cthugha", self.census(), VALID)
        counts = [o.row_count for o in report.orphans]
        assert counts == sorted(counts, reverse=True)

    def test_none_and_empty_normalised_to_empty_key(self):
        report = wing_orphans.orphan_report("cthugha", self.census(), VALID)
        wings = [o.wing for o in report.orphans]
        assert "" in wings          # both None and "" collapse to ""
        assert None not in wings
        # the empty-key orphan must carry the combined row count (17 + 11)
        empty = next(o for o in report.orphans if o.wing == "")
        assert empty.row_count == 28
        assert empty.category == wing_orphans.ORPHAN_EMPTY

    def test_per_wing_categories_and_targets(self):
        report = wing_orphans.orphan_report("cthugha", self.census(), VALID)
        by_wing = {o.wing: o for o in report.orphans}
        assert by_wing["v3"].category == wing_orphans.ORPHAN_UNRECOGNIZED
        assert by_wing["v3"].suggested_target == "memchorus_general"
        assert by_wing["2026-11-20T10:17:03"].category == wing_orphans.ORPHAN_TIMESTAMP
        assert by_wing["cthugha_learning"].category == wing_orphans.ORPHAN_CASE_VARIANT
        assert by_wing["cthugha_learning"].suggested_target == "cthugha_LEARNING"

    def test_clean_store_report(self):
        report = wing_orphans.orphan_report("cthugha", {"cthugha_LEARNING": 5}, VALID)
        assert report.is_clean is True
        assert report.orphan_rows == 0
        assert report.orphans == ()
        assert "0 of 5" in report.summary()

    def test_empty_census(self):
        report = wing_orphans.orphan_report("cthugha", {}, VALID)
        assert report.total_rows == 0
        assert report.orphan_pct == 0.0

    def test_to_dict_shape(self):
        d = wing_orphans.orphan_report("cthugha", self.census(), VALID).to_dict()
        for key in (
            "profile", "total_rows", "mapped_rows", "orphan_rows",
            "orphan_pct", "is_clean", "orphans", "summary",
        ):
            assert key in d
        for o in d["orphans"]:
            for key in ("wing", "row_count", "category", "suggested_target"):
                assert key in o

    def test_plan_reassignment_entries(self):
        report = wing_orphans.orphan_report("cthugha", self.census(), VALID)
        plan = wing_orphans.plan_reassignment(report)
        assert len(plan) == 4  # "", v3, ts, cthugha_learning
        by_src = {p["source"]: p for p in plan}
        assert by_src[""]["target"] == "memchorus_general"
        assert by_src[""]["rows"] == 28
        assert by_src["v3"]["target"] == "memchorus_general"
        assert by_src["cthugha_learning"]["target"] == "cthugha_LEARNING"
        for p in plan:
            assert p["rows"] > 0
            assert p["category"] in wing_orphans.ORPHAN_CATEGORIES


# ---------------------------------------------------------------------------
# Config-flag helpers + valid set
# ---------------------------------------------------------------------------
class TestConfigAndValidSet:
    def test_flag_defaults_enabled(self):
        # No wing_isolation section configured in the test env -> defaults.
        assert wing_orphans.warn_on_orphaned_enabled("cthugha") is True
        assert wing_orphans.enforce_on_write_enabled("cthugha") is True

    def test_valid_wings_for_profile_contains_code_namespace(self):
        valid = wing_orphans.valid_wings_for_profile("cthugha")
        assert "cthugha_LEARNING" in valid
        assert "memchorus_general" in valid

    def test_valid_wings_for_profile_default_profile(self):
        valid = wing_orphans.valid_wings_for_profile("default")
        # The default profile owns the shared writer family and legacy
        # category keys — but NOT ``general``, which is only declared in
        # nyarlathotep's profile YAML wing_map (machine-specific, asserted
        # separately so this test stays hermetic).
        assert "memchorus_general" in valid
        assert "LEARNING" in valid
        assert "general" not in valid

    def test_valid_set_is_frozenset(self):
        assert isinstance(wing_orphans.valid_wings_for_profile("cthugha"), frozenset)
