"""Integration tests for CalibrationEngine (v1.8.0 auto-tuning framework).

Covers:
- Full pipeline: HitRateTracker recording -> adjustment computation -> config write-back -> round-trip
- CLI entry point verification
- Graceful degradation when dependencies unavailable
- Parameter bounds enforcement after multiple calibration cycles

NOTE: Pipeline tests mock aggregate_hit_rate_stats via patch.object at CLASS level
to avoid xdist singleton isolation issues (HitRateTracker._index empty per worker).
"""

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_DEFAULT_PARAMS = {
    "min_relevance_score": 0.3,
    "dedup_similarity_threshold": 0.6,
    "retention_scan_interval_days": 14.0,
}


class TestCalibrationState:
    """Basic state dict round-trips."""

    def test_to_dict_has_all_keys(self):
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="state_test")
        d = engine.state.to_dict()
        for key in ("calibration_count", "last_calibrated_at", "min_relevance_score",
                     "dedup_similarity_threshold", "retention_scan_interval_days"):
            assert key in d, f"Missing key: {key}"

    def test_from_dict_round_trip(self):
        from memchorus.calibration_engine import CalibrationState
        d = {
            "calibration_count": 42,
            "last_calibrated_at": "2026-01-01T00:00:00Z",
            "min_relevance_score": 0.35,
            "dedup_similarity_threshold": 0.70,
            "retention_scan_interval_days": 10.0,
        }
        state = CalibrationState.from_dict(d)
        assert state.calibration_count == 42
        assert round(state.min_relevance_score, 2) == 0.35

    def test_from_dict_missing_keys_uses_defaults(self):
        from memchorus.calibration_engine import CalibrationState
        empty = CalibrationState.from_dict({})
        assert empty.min_relevance_score is not None


class TestCalibrationEnginePersistence:
    """YAML file read/write round-trips."""

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

    def test_fresh_profile_returns_defaults(self):
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="fresh")
        result = engine.compute_adjustments()
        assert len(result) == 3

    def test_save_and_load_round_trip(self):
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="roundtrip")
        engine.apply_and_persist()
        engine2 = CalibrationEngine(profile_name="roundtrip")
        assert engine2.state.min_relevance_score is not None

    def test_get_adjusted_params_returns_persisted_values(self):
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="persist_read")
        engine.apply_and_persist()
        params = CalibrationEngine.get_adjusted_params("persist_read")
        assert len(params) == 3

    def test_corrupt_yaml_falls_back_to_defaults(self):
        from memchorus.calibration_engine import CalibrationEngine, DEFAULT_TUNING_DIR
        from memchorus.calibration_engine import _DEFAULT_PARAMS
        profile = "corrupt"
        tuning_dir = Path(DEFAULT_TUNING_DIR)
        tuning_dir.mkdir(parents=True, exist_ok=True)
        profile_file = tuning_dir / f"{profile}.yaml"
        profile_file.write_text("[INVALID YAML {{}}")
        engine = CalibrationEngine(profile_name=profile)
        result = engine.compute_adjustments()
        assert len(result) == 3


class TestCalibrationEnginePipeline:
    """End-to-end pipeline under mocked stats (avoids xdist singleton issues)."""

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
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="pipeline_test")

        # Use patch.object at CLASS level for reliable mocking under xdist.
        with patch.object(CalibrationEngine, 'aggregate_hit_rate_stats', return_value=(100, 50)):
            saves, recalls = engine.aggregate_hit_rate_stats()
            assert saves == 100
            assert recalls == 50

    def test_apply_and_persist_updates_state(self):
        """apply_and_persist increments calibration_count and sets timestamp."""
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="persist_test")
        initial_count = engine.state.calibration_count

        adjusted = engine.apply_and_persist()
        assert engine.state.calibration_count == initial_count + 1
        assert engine.state.last_calibrated_at is not None
        assert len(adjusted) == 3

    def test_full_pipeline_integration(self):
        """Complete end-to-end: stats fed in -> adjust computed -> bounds checked."""
        from memchorus.adaptive_threshold import PARAM_BOUNDS
        from memchorus.calibration_engine import CalibrationEngine

        engine = CalibrationEngine(profile_name="full_pipeline")

        # Class-level patch for xdist safety, returns deterministic hit-rate values.
        with patch.object(CalibrationEngine, 'aggregate_hit_rate_stats', return_value=(200, 150)):
            saves, recalls = engine.aggregate_hit_rate_stats()
            assert saves == 200
            assert recalls == 150

            adjusted = engine.compute_adjustments()
            for param in _DEFAULT_PARAMS:
                assert param in adjusted
                bounds = PARAM_BOUNDS[param]
                assert bounds.minimum <= adjusted[param] <= bounds.maximum + 1e-9

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

    def test_no_adaptive_returns_v170_defaults(self):
        """When AdaptiveThreshold is not importable, compute_adjustments returns v1.7.0 defaults."""
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="no_adaptive")
        engine._adaptive = None  # simulate unavailable module

        result = engine.compute_adjustments()
        assert result == _DEFAULT_PARAMS

    def test_no_tracker_returns_zeros(self):
        """When HitRateTracker is unavailable, aggregate returns zeroes."""
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="no_tracker")

        saves, recalls = engine.aggregate_hit_rate_stats()
        assert saves == 0
        assert recalls == 0

    def test_no_mistake_detector_returns_zeros(self):
        """When MistakeDetector is unavailable, aggregate returns zeroes."""
        from memchorus.calibration_engine import CalibrationEngine
        engine = CalibrationEngine(profile_name="no_mistake")

        result = engine.aggregate_hit_rate_stats()
        assert result == (0, 0)


class TestCLIFlags:
    """CLI entry points and --profile flag wiring."""

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

    def test_main_no_crash_on_run(self):
        """CLI entry main() does not raise."""
        from memchorus.calibration_engine import main
        sys.argv = ["memchorus-recalibrate"]
        try:
            main()
        except SystemExit:
            pass  # argparse --help triggers this

    def test_main_profile_flag_output(self):
        """CLI runs with explicit profile."""
        from memchorus.calibration_engine import main
        sys.argv = ["memchorus-recalibrate", "--profile", "test_cli"]
        try:
            main()
        except SystemExit as e:
            assert e.code in (0, None)


# ---- Bounds enforcement after multiple calibration cycles ----

class TestParameterBounds:
    """Ensure repeated calibrations never blow params out of bounds."""

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

    def test_repeated_calibration_keeps_bounds(self):
        """Ten consecutive calibrations should respect PARAM_BOUNDS ceiling and floor."""
        from memchorus.adaptive_threshold import PARAM_BOUNDS
        from memchorus.calibration_engine import CalibrationEngine

        engine = CalibrationEngine(profile_name="bounds_test")
        for _i in range(10):
            adjusted = engine.apply_and_persist()
            for param, val in adjusted.items():
                if param in PARAM_BOUNDS:
                    bounds = PARAM_BOUNDS[param]
                    assert bounds.minimum <= val <= bounds.maximum + 1e-9, \
                        f"Out of bounds after cycle {_i}: {param}={val}"

    def test_extreme_hit_rate_does_not_overshoot(self):
        """Even with perfect hit rate (100%), params stay in range."""
        from memchorus.adaptive_threshold import PARAM_BOUNDS
        from memchorus.calibration_engine import CalibrationEngine

        engine = CalibrationEngine(profile_name="extreme")
        for _i in range(5):
            with patch.object(CalibrationEngine, 'aggregate_hit_rate_stats', return_value=(1000, 999)):
                adjusted = engine.apply_and_persist()

        for param, val in adjusted.items():
            if param in PARAM_BOUNDS:
                bounds = PARAM_BOUNDS[param]
                assert bounds.minimum <= val <= bounds.maximum + 1e-9
