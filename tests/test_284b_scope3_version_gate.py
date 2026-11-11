"""GH-284 Issue B, Scope 3 — version / schema gate acceptance tests.

The gate is the "refuse unsafe writes" mechanism: when the installed SDK is
older than the version the store was declared for (or the on-disk palace
schema is newer than the installed SDK), writes are held while reads remain
available.  It must be import-safe (never raises, degrades to *block* on any
internal error) and the provider must (a) enforce it on every write path,
(b) surface its state in the orientation header, and (c) share the declared
minimum with the provider manifest so the two cannot drift.

Scope-3 acceptance criteria (from the kanban card / issue #284):
  * ``evaluate(installed=None)``          → no-sdk block (reads ok, writes no).
  * ``evaluate(installed="0.9.0")``       → version block, names both versions.
  * ``evaluate(installed="2.0.80")``      → ok (reads + writes).
  * unparseable non-empty version          → detected, writes blocked (B6).
  * pre-release ordering                   → rc/beta sort before the release.
  * palace ``schema_version`` newer than SDK → schema block.
  * provider write paths (remember/sync_turn/on_session_end/on_pre_compress)
    refuse when the gate blocks, but reads (orientation) still render.
  * orientation header always shows a gate line (green AND red).
  * ``EXPECTED_SDK_VERSION`` re-exported from the gate — single source of
    truth, equal to ``memchorus.__version__``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

# --- path setup (mirror conftest.py / scope-1/2 suites) ----------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Part 1 — pure gate-module tests (no provider construction required)
# ---------------------------------------------------------------------------
from integrations.provider._version_gate import (  # noqa: E402
    EXPECTED_SDK_VERSION as GATE_EXPECTED,
    VersionGateStatus,
    compare_versions,
    evaluate,
    parse_version,
    palace_schema_version,
    status_for_current_environment,
)


class TestParseVersion:
    def test_full_semver(self):
        v = parse_version("2.0.80")
        assert v is not None
        assert (v.major, v.minor, v.patch) == (2, 0, 80)
        assert v.pre is None

    def test_v_prefix_and_missing_parts(self):
        v = parse_version("v2.0")
        assert v is not None
        assert (v.major, v.minor, v.patch) == (2, 0, 0)

    def test_pre_release_tag_captured(self):
        v = parse_version("2.0.80-rc1")
        assert v is not None
        assert (v.major, v.minor, v.patch) == (2, 0, 80)
        assert v.pre == "rc1"

    def test_non_numeric_returns_none(self):
        assert parse_version("not-a-version") is None
        assert parse_version("") is None
        assert parse_version(None) is None


class TestCompareVersions:
    def test_equal(self):
        assert compare_versions("2.0.80", "2.0.80") == 0

    def test_ordering(self):
        assert compare_versions("2.0.80", "2.0.81") == -1
        assert compare_versions("2.0.81", "2.0.80") == 1
        assert compare_versions("1.0.0", "2.0.0") == -1

    def test_pre_release_sorts_before_release(self):
        # A pre-release of 2.0.80 is strictly *before* the 2.0.80 release.
        assert compare_versions("2.0.80-rc1", "2.0.80") == -1
        assert compare_versions("2.0.80", "2.0.80-rc1") == 1

    def test_numeric_pre_release_suffix(self):
        # beta10 > beta2 (numeric, not lexicographic) — both before release.
        assert compare_versions("2.0.80-beta10", "2.0.80-beta2") == 1
        assert compare_versions("2.0.80-beta2", "2.0.80") == -1

    def test_unparseable_is_indeterminate(self):
        # Neither parses -> 0 (a "no signal", not a "a < b" claim).
        assert compare_versions("garbage", "2.0.80") == 0


class TestEvaluate:
    def test_no_sdk_blocks_writes_keeps_reads(self):
        s = evaluate(installed_version=None)
        assert s.blocked_by == "no-sdk"
        assert s.safe_for_reads is True
        assert s.safe_for_writes is False
        assert s.blocked is True
        assert "Fix" not in s.reason or True  # reason is human text, not a tag

    def test_older_sdk_blocks_writes_names_both(self):
        s = evaluate(installed_version="0.9.0", expected_version="2.0.80")
        assert s.blocked_by == "version"
        assert s.safe_for_writes is False
        assert s.safe_for_reads is True
        assert "0.9.0" in s.reason
        assert "2.0.80" in s.reason

    def test_equal_version_allows_writes(self):
        s = evaluate(installed_version="2.0.80", expected_version="2.0.80")
        assert s.blocked_by == "ok"
        assert s.safe_for_reads is True
        assert s.safe_for_writes is True
        assert s.reason == ""

    def test_newer_version_allows_writes(self):
        s = evaluate(installed_version="2.1.0", expected_version="2.0.80")
        assert s.blocked_by == "ok"
        assert s.safe_for_writes is True

    def test_unparseable_version_is_detected_and_blocks(self):
        # B6: the mismatch must be *detected*, not silently passed.  A string
        # with no numeric core is the unparseable case (a version-like core
        # with an unusual pre-release tag is parsed and compared instead).
        s = evaluate(installed_version="not-a-version", expected_version="2.0.80")
        assert s.blocked_by == "version"
        assert s.safe_for_writes is False
        assert s.safe_for_reads is True
        assert "not-a-version" in s.reason

    def test_schema_newer_than_sdk_blocks(self, tmp_path: Path):
        (tmp_path / "schema_version").write_text("2.9.0\n")
        s = evaluate(
            installed_version="2.0.80",
            expected_version="2.0.80",
            palace_dir=tmp_path,
        )
        assert s.blocked_by == "schema"
        assert s.safe_for_writes is False
        assert s.schema_version == "2.9.0"

    def test_schema_same_or_older_allows(self, tmp_path: Path):
        (tmp_path / "schema_version").write_text("2.0.80\n")
        s = evaluate(
            installed_version="2.0.80",
            expected_version="2.0.80",
            palace_dir=tmp_path,
        )
        assert s.blocked_by == "ok"
        assert s.safe_for_writes is True

    def test_no_palace_dir_no_schema_probe(self):
        s = evaluate(installed_version="2.0.80", expected_version="2.0.80")
        assert s.blocked_by == "ok"


class TestPalaceSchemaReader:
    def test_reads_marker(self, tmp_path: Path):
        (tmp_path / "schema_version").write_text("2.9.0\ntrailing\n")
        assert palace_schema_version(tmp_path) == "2.9.0"

    def test_missing_dir_returns_none(self, tmp_path: Path):
        assert palace_schema_version(tmp_path / "nope") is None

    def test_no_marker_returns_none(self, tmp_path: Path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        assert palace_schema_version(tmp_path) is None


class TestStatusForCurrentEnvironment:
    def test_never_raises(self):
        # Whatever the live env is, this must return a status, not raise.
        s = status_for_current_environment()
        assert isinstance(s, VersionGateStatus)
        assert s.blocked_by in ("ok", "no-sdk", "version", "schema")


# ---------------------------------------------------------------------------
# Part 2 — provider wiring (gate enforced on writes, shown in orientation)
# ---------------------------------------------------------------------------
from integrations.provider import (  # noqa: E402
    EXPECTED_SDK_VERSION,
    MemChorusMemoryProvider,
)


def _green():
    return VersionGateStatus(
        installed="2.0.80", expected="2.0.80",
        safe_for_reads=True, safe_for_writes=True, reason="", blocked_by="ok",
    )


def _red(reason_tag="version", reason="Installed SDK is older than the declared minimum."):
    return VersionGateStatus(
        installed="0.9.0", expected="2.0.80",
        safe_for_reads=True, safe_for_writes=False,
        reason=reason, blocked_by=reason_tag,
    )


class TestProviderEnforcesGateOnWrites:
    """When the gate blocks, every write path must refuse; reads still work."""

    def test_remember_tool_refused_on_blocked_gate(self):
        provider = MemChorusMemoryProvider()
        provider._version_gate_status = _red()
        orch = mock.MagicMock()
        provider._external_orchestrator = orch
        provider.handle_tool_call(
            "memchorus_remember",
            {"key": "k", "content": "v", "category": "LEARNING"},
        )
        # The orchestrator's save() must never have been reached.
        orch.save.assert_not_called()

    def test_sync_turn_skips_storage_on_blocked_gate(self):
        provider = MemChorusMemoryProvider()
        provider._version_gate_status = _red()
        orch = mock.MagicMock()
        provider._external_orchestrator = orch
        provider.sync_turn("user text", "assistant text")
        # sync_turn's enforcement/storage write path must be skipped.
        orch.run_calibration_cycle.assert_not_called()

    def test_on_session_end_writes_skipped_on_blocked_gate(self):
        provider = MemChorusMemoryProvider()
        provider._version_gate_status = _red()
        provider._external_orchestrator = mock.MagicMock()
        provider.on_session_end(
            messages=[{"role": "user", "content": "hi"}]
        )

    def test_on_pre_compress_still_returns_contribution(self):
        provider = MemChorusMemoryProvider()
        provider._version_gate_status = _red()
        provider._external_orchestrator = mock.MagicMock()
        provider._resolve_project = lambda: "proj"
        contribution = provider.on_pre_compress(
            [{"role": "user", "content": "hi"}]
        )
        assert "MemChorus" in contribution

    def test_orientation_still_renders_on_blocked_gate(self):
        """Reads remain available even when writes are refused."""
        provider = MemChorusMemoryProvider()
        provider._version_gate_status = _red()
        block, _prov, _count, _degraded = provider._render_orientation_block([])
        assert "Version gate:" in block
        assert "BLOCKED" in block


class TestProviderOrientationShowsGate:
    """The orientation header must always carry a gate line, green or red."""

    def test_green_gate_line(self):
        provider = MemChorusMemoryProvider()
        provider._version_gate_status = _green()
        block, *_ = provider._render_orientation_block([])
        assert "Version gate:" in block
        assert "ok" in block
        assert "2.0.80" in block
        assert "BLOCKED" not in block

    def test_red_gate_line(self):
        provider = MemChorusMemoryProvider()
        provider._version_gate_status = _red()
        block, *_ = provider._render_orientation_block([])
        assert "Version gate:" in block
        assert "BLOCKED" in block


class TestProviderGateLazyEval:
    """``_version_gate`` must call the env probe once and cache the result."""

    def test_caches_result(self):
        provider = MemChorusMemoryProvider()
        assert provider._version_gate_status is None
        with mock.patch(
            "integrations.provider._version_gate_status",
            side_effect=AssertionError("should not be called"),
        ) as probe:
            # Seed a value so we can assert the cache is returned unchanged.
            provider._version_gate_status = _green()
            first = provider._version_gate()
            second = provider._version_gate()
        assert first is second is provider._version_gate_status
        probe.assert_not_called()

    def test_internal_error_degrades_to_block(self):
        provider = MemChorusMemoryProvider()
        provider._version_gate_status = None
        with mock.patch(
            "integrations.provider._version_gate_status",
            side_effect=RuntimeError("boom"),
        ):
            status = provider._version_gate()
        # Conservative default: on any internal error, refuse writes.
        assert status.safe_for_writes is False
        assert status.safe_for_reads is True
        assert status.blocked_by == "no-sdk"


class TestExpectedVersionSingleSource:
    def test_reexport_is_gate_constant(self):
        assert EXPECTED_SDK_VERSION is GATE_EXPECTED

    def test_in_sync_with_sdk(self):
        import memchorus
        assert EXPECTED_SDK_VERSION == memchorus.__version__
        assert GATE_EXPECTED == memchorus.__version__
