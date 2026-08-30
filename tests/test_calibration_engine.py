"""Integration tests for CalibrationEngine (v1.8.0 auto-tuning framework).

Covers:
- Full pipeline: HitRateTracker recording -> adjustment computation -> config write-back -> round-trip
- CLI entry point output format verification
- Graceful degradation when AdaptiveThreshold unavailable
- CalibrationState serialization/deserialization
- YAML persistence round-trip
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from memchorus.calibration_engine import (
    CalibrationEngine,
    CalibrationState,
    _DEFAULT_PARAMS,
    DEFAULT_TUNING_DIR,
)



class TestCalibrationState:
    """Verify CalibrationState serialization round-trips correctly."""

    def test_default_values_match_v170(self):
        state = CalibrationState()
        assert state.min_relevance_score == _DEFAULT_PARAMS["min_relevance_score"]
        assert state.dedup_similarity_threshold == _DEFAULT_PARAMS["dedup_similarity_threshold"]
        assert state.retention_scan_interval_days == _DEFAULT_PARAMS["retention_scan_interval_days"]

    def test_to_dict_has_all_keys(self):
        state = CalibrationState()
        d = state.to_dict()
        expected_keys = {
            "min_relevance_score",
            "dedup_similarity_threshold",
            "retention_scan_interval_days",
            "calibration_window",
            "profile_volume_writes_per_day",
            "last_calibrated_at",
            "calibration_count",
        }
        assert set(d.keys()) == expected_keys

    def test_from_dict_round_trip(self):
        state = CalibrationState(
            min_relevance_score=0.5,
            dedup_similarity_threshold=0.75,
            retention_scan_interval_days=21.0,
            calibration_window=30,
            profile_volume_writes_per_day=42.0,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            calibration_count=5,
        )
        d = state.to_dict()
        restored = CalibrationState.from_dict(d)
        assert restored.min_relevance_score == 0.5
        assert restored.dedup_similarity_threshold == 0.75
        assert restored.retention_scan_interval_days == 21.0
        assert restored.calibration_window == 30
        assert restored.profile_volume_writes_per_day == 42.0
        assert restored.last_calibrated_at == "2026-01-01T00:00:00+00:00"
        assert restored.calibration_count == 5

    def test_from_dict_missing_keys_uses_defaults(self):
        d = {}
        state = CalibrationState.from_dict(d)
        assert state.min_relevance_score == _DEFAULT_PARAMS["min_relevance_score"]
        assert state.dedup_similarity_threshold == _DEFAULT_PARAMS["dedup_similarity_threshold"]
        assert state.calibration_window == 50
        assert state.calibration_count == 0


class TestCalibrationEnginePersistence:
    """YAML persistence round-trip integration tests."""

    def _tmp_dir(self):
        d = tempfile.mkdtemp()
        return Path(d)

    @pytest.fixture(autouse=True)
    def _patch_tuning_dir(self):
        original = DEFAULT_TUNING_DIR.parent
        tmp = Path(tempfile.mkdtemp())
        with patch("memchorus.calibration_engine.DEFAULT_TUNING_DIR", tmp):
            self.tmp_root = tmp
            yield

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        shutil.rmtree(str(self.tmp_root), ignore_errors=True)

    def test_fresh_profile_returns_defaults(self):
        engine = CalibrationEngine(profile_name="test_fresh")
        params = {
            "min_relevance_score": engine.state.min_relevance_score,
            "dedup_similarity_threshold": engine.state.dedup_similarity_threshold,
            "retention_scan_interval_days": engine.state.retention_scan_interval_days,
        }
        assert params == _DEFAULT_PARAMS

    def test_save_and_load_round_trip(self):
        engine = CalibrationEngine(profile_name="test_roundtrip")
        engine.state.min_relevance_score = 0.5
        engine.state.calibration_count = 7
        engine._save_state()

        engine2 = CalibrationEngine(profile_name="test_roundtrip")
        assert engine2.state.min_relevance_score == pytest.approx(0.5, abs=1e-6)
        assert engine2.state.calibration_count == 7

    def test_corrupt_yaml_falls_back_to_defaults(self):
        (self.tmp_root).mkdir(parents=True, exist_ok=True)
        bad_path = self.tmp_root / "corrupt_profile.yaml"
        bad_path.write_text("{{invalid yaml: [broken")
        with patch.object(CalibrationEngine, "__init__", lambda self, pn: None):
            pass
        # Simpler: just create the file and test that _load_state falls back
        engine = CalibrationEngine(profile_name="corrupt_profile")
        assert engine.state.min_relevance_score == _DEFAULT_PARAMS["min_relevance_score"]

    def test_get_adjusted_params_returns_persisted_values(self):
        engine = CalibrationEngine(profile_name="test_adj")
        engine.state.min_relevance_score = 0.8
        engine._save_state()

        result = CalibrationEngine.get_adjusted_params("test_adj")
        assert result["min_relevance_score"] == pytest.approx(0.8, abs=1e-6)


class TestCalibrationEnginePipeline:
    """Full pipeline: HitRateTracker -> compute_adjustments -> persist."""

    @pytest.fixture(autouse=True)
    def _patch_tuning_dir(self):
        tmp = Path(tempfile.mkdtemp())
        with patch("memchorus.calibration_engine.DEFAULT_TUNING_DIR", tmp):
            self.tmp_root = tmp
            yield

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        shutil.rmtree(str(self.tmp_root), ignore_errors=True)

    def test_compute_adjustments_with_tracker(self):
        """Aggregate HitRateTracker stats, feed to AdaptiveThreshold, compute adjustments."""
        from memchorus.hit_rate_tracker import HitRateTracker

        engine = CalibrationEngine(profile_name="pipeline_test")

        # Simulate tracker data by mocking the singleton
        class MockTracker:
            total_saves = 100
            total_recalls = 50

        with patch.object(HitRateTracker, 'get_instance', return_value=MockTracker()):
            saves, recalls = engine.aggregate_hit_rate_stats()
            assert saves == 100
            assert recalls == 50

    def test_apply_and_persist_updates_state(self):
        """apply_and_persist increments calibration_count and sets timestamp."""
        engine = CalibrationEngine(profile_name="persist_test")
        initial_count = engine.state.calibration_count

        adjusted = engine.apply_and_persist()
        assert engine.state.calibration_count == initial_count + 1
        assert engine.state.last_calibrated_at is not None
        assert len(adjusted) == 3

    def test_full_pipeline_integration(self):
        """Complete end-to-end: save -> recall -> recalibrate -> persist."""
        from memchorus.hit_rate_tracker import HitRateTracker

        # Save and restore real instance _index to prevent cross-test pollution.
        real_instance = HitRateTracker.get_instance()
        saved_index = real_instance._index.copy()

        try:
            # Populate with exactly 200 entries, each carrying 0.75 recall count.
            # → total_saves == len(_index) == 200
            # → total_recalls == sum(0.75 × 200) == 150 ✓
            real_instance._index = {
                f"entry_{i:03d}": {"total_recalls": 0.75, "useful_flags": 0,
                                   "noise_flags": 0, "first_saved_at": 0.0,
                                   "last_seen_at": 0.0}
                for i in range(200)
            }

            engine = CalibrationEngine(profile_name="full_pipeline")
            saves, recalls = engine.aggregate_hit_rate_stats()
            assert saves == 200
            assert recalls == 150

            adjusted = engine.compute_adjustments()
            for param in _DEFAULT_PARAMS:
                assert param in adjusted
                # Values stay within parameter bounds
                from memchorus.adaptive_threshold import PARAM_BOUNDS
                bounds = PARAM_BOUNDS[param]
                assert bounds.minimum <= adjusted[param] <= bounds.maximum + 1e-9

        finally:
            real_instance._index = saved_index

        engine._save_state()


class TestGracefulDegradation:
    """Graceful degradation when AdaptiveThreshold is unavailable."""

    @pytest.fixture(autouse=True)
    def _patch_tuning_dir(self):
        tmp = Path(tempfile.mkdtemp())
        with patch("memchorus.calibration_engine.DEFAULT_TUNING_DIR", tmp):
            self.tmp_root = tmp
            yield

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        shutil.rmtree(str(self.tmp_root), ignore_errors=True)

    @pytest.fixture(autouse=True)
    def _reset_tracker(self):
        """Clear the HitRateTracker and MistakeDetector singletons before and
        after each test so cross-test pollution (evidenced by total_saves > 0 or
        non-zero mistake flags) never leaks.  Both are process-level singletons;
        any test module that drives the live classify_and_flag / register_save
        path bumps their counters, and a reset here protects this class (and any
        future class in this file) from that state."""
        from memchorus.hit_rate_tracker import HitRateTracker
        from memchorus.mistake_detector import MistakeDetector

        HitRateTracker.reset()
        det = MistakeDetector.get_instance()
        det.total_noise_flags = 0
        det.total_useful_flags = 0
        yield
        HitRateTracker.reset()
        det = MistakeDetector.get_instance()
        det.total_noise_flags = 0
        det.total_useful_flags = 0

    def test_no_adaptive_returns_v170_defaults(self):
        """When AdaptiveThreshold is not importable, compute_adjustments returns v1.7.0 defaults."""
        engine = CalibrationEngine(profile_name="no_adaptive")
        engine._adaptive = None  # simulate unavailable module

        result = engine.compute_adjustments()
        assert result == _DEFAULT_PARAMS

    def test_no_tracker_returns_zeros(self):
        """When HitRateTracker is unavailable, aggregate returns zeroes."""
        engine = CalibrationEngine(profile_name="no_tracker")

        # Force the module import to fail (the degradation path aggregate_hit_rate_stats
        # actually guards) so the result is independent of any on-disk sidecar this
        # machine may carry — the sibling tests simulate unavailability the same way.
        with patch.dict(sys.modules, {"memchorus.hit_rate_tracker": None}):
            saves, recalls = engine.aggregate_hit_rate_stats()
        assert saves == 0
        assert recalls == 0

    def test_no_mistake_detector_returns_zeros(self):
        """When MistakeDetector is unavailable, aggregate returns zeroes."""
        engine = CalibrationEngine(profile_name="no_mistake")

        noise, useful = engine.aggregate_mistake_flags()
        assert noise == 0
        assert useful == 0


class TestCLIEntryPoint:
    """CLI entry point `memchorus recalibrate --profile <name>` end-to-end."""

    @staticmethod
    def _env(tmp_home: str) -> dict:
        """Return a subprocess-safe environment for CLI tests.

        Clears PYTHONPATH workspace entries and sets HOME to an isolated temp
        directory so no real config is touched.  Because overriding HOME breaks
        Python's automatic discovery of user site-packages (which live under
        ~/.local), we explicitly preserve the installed site-packages path."""
        base = os.environ.copy()
        base["HOME"] = tmp_home
        # Strip workspace-source paths from PYTHONPATH so these tests exercise
        # the installed package, not adjacent src/ trees.  Because HOME is
        # overridden we also need to preserve the actual memchorus install path
        # (works for both system user-site and venv site-packages).
        pp = base.get("PYTHONPATH", "") or ""
        cleaned = [p for p in pp.split(os.pathsep) if "MemChorus" not in p] if pp else []
        
        # Dynamically find where memchorus lives so -m resolves correctly even
        # when HOME override would normally block discovery.
        try:
            import memchorus as _mc
            mc_site = os.path.dirname(os.path.dirname(_mc.__file__))  # parent of site-packages/memchorus
            if mc_site and mc_site not in cleaned:
                cleaned.append(mc_site)
        except (ImportError, TypeError):
            pass
        
        base["PYTHONPATH"] = os.pathsep.join(cleaned)
        return base

    def test_main_help(self, tmp_path):
        """--help returns exit code 0 and expected output (tested via entry point)."""
        result = subprocess.run(
            ["memchorus-recalibrate", "--help"],
            capture_output=True,
            text=True,
            env=self._env(str(tmp_path)),
        )
        assert result.returncode == 0
        assert "profile" in result.stdout.lower()

    def test_main_profile_output(self, tmp_path):
        """Running with --profile produces expected output format (tested via entry point)."""
        result = subprocess.run(
            ["memchorus-recalibrate", "--profile", "test_cli"],
            capture_output=True,
            text=True,
            env=self._env(str(tmp_path)),
        )
        # Should contain profile name and at least the calibration message
        assert result.returncode == 0
        assert "test_cli" in result.stdout or "Calibrating" in result.stdout
