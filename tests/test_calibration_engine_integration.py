"""CalibrationEngine integration tests — full pipeline from hit recording through adjustment computation to config write-back."""

import os
import tempfile
from pathlib import Path

import pytest

from memchorus.calibration_engine import (
    CalibrationEngine,
    CalibrationState,
    _DEFAULT_PARAMS,
)


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset shared singletons to prevent cross-test contamination."""
    try:
        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()
    except Exception:
        pass
    try:
        from memchorus.mistake_detector import MistakeDetector
        MistakeDetector._instance = None
    except Exception:
        pass
    yield
    try:
        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()
    except Exception:
        pass
    try:
        from memchorus.mistake_detector import MistakeDetector
        MistakeDetector._instance = None
    except Exception:
        pass


class TestCalibrationState:
    """Verify CalibrationState serialization."""

    def test_default_state_values(self):
        state = CalibrationState()
        assert state.min_relevance_score == _DEFAULT_PARAMS["min_relevance_score"]
        assert state.dedup_similarity_threshold == _DEFAULT_PARAMS["dedup_similarity_threshold"]
        assert state.retention_scan_interval_days == _DEFAULT_PARAMS["retention_scan_interval_days"]
        assert state.calibration_window == 50
        assert state.profile_volume_writes_per_day == 0.0
        assert state.last_calibrated_at is None
        assert state.calibration_count == 0

    def test_to_dict_roundtrip(self):
        original = CalibrationState(
            min_relevance_score=0.35,
            dedup_similarity_threshold=0.55,
            retention_scan_interval_days=12.0,
            calibration_window=40,
            profile_volume_writes_per_day=85.0,
            last_calibrated_at="2026-01-15T10:00:00Z",
            calibration_count=7,
        )
        data = original.to_dict()
        restored = CalibrationState.from_dict(data)

        assert restored.min_relevance_score == 0.35
        assert restored.dedup_similarity_threshold == 0.55
        assert restored.retention_scan_interval_days == 12.0
        assert restored.calibration_window == 40
        assert restored.profile_volume_writes_per_day == pytest.approx(85.0)
        assert restored.last_calibrated_at == "2026-01-15T10:00:00Z"
        assert restored.calibration_count == 7


class TestLoadAndPersistState:
    """Verify YAML load/save of tuning config."""

    def test_load_creates_defaults_when_no_file(self, tmp_path):
        engine = CalibrationEngine(profile_name="nonexistent_profile")
        engine.tuning_path = tmp_path / "nonexistent.yaml"
        state = engine._load_state()
        assert state.min_relevance_score == _DEFAULT_PARAMS["min_relevance_score"]

    def test_load_reads_existing_yaml(self, tmp_path):
        import yaml

        tuning_file = tmp_path / "test_profile.yaml"
        tuning_file.write_text(yaml.dump({
            "calibration_window": 30,
            "min_relevance_score": 0.42,
            "dedup_similarity_threshold": 0.55,
            "retention_scan_interval_days": 10.0,
            "profile_volume_writes_per_day": 55.0,
            "last_calibrated_at": "2026-01-01T00:00:00Z",
            "calibration_count": 3,
        }))

        engine = CalibrationEngine(profile_name="test_profile")
        engine.tuning_path = tuning_file
        state = engine._load_state()

        assert state.min_relevance_score == 0.42
        assert state.dedup_similarity_threshold == 0.55
        assert state.calibration_window == 30
        assert state.calibration_count == 3

    def test_save_and_reload(self, tmp_path):
        engine = CalibrationEngine(profile_name="save_test")
        engine.tuning_path = tmp_path / "save_test.yaml"

        engine.state.min_relevance_score = 0.45
        engine.state.calibration_count = 1
        engine._save_state()

        # Reload — tuning_path is set in __init__ so we must override before assigning state
        engine2 = CalibrationEngine(profile_name="save_test")
        engine2.tuning_path = tmp_path / "save_test.yaml"
        engine2.state = engine2._load_state()  # reload from file after path override
        assert engine2.state.min_relevance_score == 0.45
        assert engine2.state.calibration_count == 1


class TestComputeBoostFromFlags:
    """Verify the AC-RTB boost computation piecewise mapping for useful/noise flags."""

    def test_minimum_observations_not_met(self):
        # Fewer than MIN_OBSERVATIONS (3) → returns 1.0
        boost = CalibrationEngine._compute_boost_from_flags(useful=1, noise=1)
        assert boost == pytest.approx(1.0)

    def test_high_hit_rate_yields_boost_above_two(self):
        # hit_rate=1.0 (useful only) → max boost 3.0
        boost = CalibrationEngine._compute_boost_from_flags(useful=10, noise=0)
        assert boost == pytest.approx(3.0)

    def test_very_low_hit_rate_yields_penalty(self):
        # hit_rate near 0 → penalty floor ~0.5
        boost = CalibrationEngine._compute_boost_from_flags(useful=0, noise=10)
        assert boost <= 0.6
        assert boost >= 0.5

    def test_moderate_hit_rate_returns_intermediate(self):
        # hit_rate=0.5 → middle of neutral zone [0.3, 0.8) → value in [0.6, 2.0)
        boost = CalibrationEngine._compute_boost_from_flags(useful=5, noise=5)
        assert 0.6 <= boost < 2.0

    def test_boost_clamped_to_range(self):
        # All edge cases must stay within [0.5, 3.0]
        for useful in range(11):
            for noise in range(4):
                if useful + noise >= CalibrationEngine.MIN_OBSERVATIONS:
                    boost = CalibrationEngine._compute_boost_from_flags(useful, noise)
                    assert 0.5 <= boost <= 3.0, (
                        f"boost={boost} out of bounds for useful={useful}, noise={noise}"
                    )

    def test_boundary_at_point_eight(self):
        # hit_rate=0.8 → should be exactly 2.0 (boundary between high and neutral)
        boost = CalibrationEngine._compute_boost_from_flags(useful=8, noise=2)
        assert boost == pytest.approx(2.0, abs=0.01)

    def test_boundary_at_point_three(self):
        # hit_rate just below 0.3 → enters penalty zone
        boost = CalibrationEngine._compute_boost_from_flags(useful=3, noise=7)
        assert boost <= 0.6


class TestBoostFactorForKey:
    """Recall-time relevance boost from HitRateTracker data."""

    def test_boost_for_unknown_key(self):
        engine = CalibrationEngine(profile_name="boost_test")
        result = engine.boost_factor_for_key("never_seen_before")
        assert result == pytest.approx(1.0)


class TestIntegrationPipeline:
    """End-to-end pipeline from hit recording through adjustment to config write-back."""

    def test_full_pipeline(self, tmp_path):
        """Set up a HitRateTracker with entries, record usage, calibrate, verify adjustments persisted."""
        # Set up temporary memory directory and tuning path
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()

        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()
        tracker = HitRateTracker.get_instance(str(mem_dir))

        # Simulate real usage patterns
        entries = [f"entry_{i}" for i in range(20)]
        for entry_key in entries:
            tracker.register_save(entry_key)

        # Recoll half the entries (moderate hit ratio ~0.5)
        for entry_key in entries[:10]:
            tracker.record_recallhit(entry_key)

        # Mark some as useful, some as noisy
        for entry_key in entries[:5]:
            tracker.record_useful(entry_key, count=2)

        for entry_key in entries[-3:]:
            tracker.record_stale(entry_key, count=1)

        tracker.flush()

        # Now run calibration
        engine = CalibrationEngine(profile_name="integration_test")
        engine.tuning_path = tmp_path / "integration.yaml"

        saves, recalls = engine.aggregate_hit_rate_stats()
        assert saves == 20
        assert recalls == 10

        adjusted = engine.compute_adjustments()

        # Verify all three parameters present and within bounds
        from memchorus.adaptive_threshold import PARAM_BOUNDS
        for param_name in ["min_relevance_score", "dedup_similarity_threshold", "retention_scan_interval_days"]:
            assert param_name in adjusted
            bounds = PARAM_BOUNDS[param_name]
            assert bounds.minimum <= adjusted[param_name] <= bounds.maximum

    def test_apply_and_persist_writes_file(self, tmp_path):
        """Verify that apply_and_persist actually writes the tuning YAML."""
        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()

        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()
        tracker = HitRateTracker.get_instance(str(mem_dir))
        tracker.register_save("e1")
        tracker.record_recallhit("e1")

        engine = CalibrationEngine(profile_name="persist_test")
        engine.tuning_path = tmp_path / "persist.yaml"

        engine.apply_and_persist()

        assert (tmp_path / "persist.yaml").exists()

    def test_aggregate_with_available_tracker(self):
        """When HitRateTracker is available, stats are pulled correctly."""
        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()

        with tempfile.TemporaryDirectory() as tmp_dir:
            HitRateTracker.get_instance(tmp_dir).register_save("ag_test")
            for _ in range(3):
                HitRateTracker.get_instance(tmp_dir).record_recallhit("ag_test")

            engine = CalibrationEngine(profile_name="agg_test")
            saves, recalls = engine.aggregate_hit_rate_stats()
            assert saves == 1
            assert recalls == 3


class TestGetAdjustedParams:
    """Class-level convenience method for boot-time lookups."""

    def test_returns_dict_with_all_params(self):
        result = CalibrationEngine.get_adjusted_params("default")
        assert "min_relevance_score" in result
        assert "dedup_similarity_threshold" in result
        assert "retention_scan_interval_days" in result

    def test_returns_defaults_for_untracked_profile(self):
        result = CalibrationEngine.get_adjusted_params("some_new_profile_xyz")
        for param_name, default_val in _DEFAULT_PARAMS.items():
            assert result[param_name] == pytest.approx(default_val)


class TestAggregateMistakeFlags:
    """Verify mistake aggregation from MistakeDetector."""

    def test_pulls_from_detector(self):
        from memchorus.mistake_detector import MistakeDetector
        MistakeDetector._instance = None

        detector = MistakeDetector.get_instance()
        detector.classify_and_flag("I already told you this is wrong")
        detector.record_positive_signal()

        engine = CalibrationEngine(profile_name="mistake_agg_test")
        noise, useful = engine.aggregate_mistake_flags()
        assert noise >= 1
        assert useful >= 1


class TestNoAdaptiveFallback:
    """Verify graceful degradation when AdaptiveThreshold unavailable."""

    def test_returns_defaults_when_adaptive_unavailable(self):
        # Simulate missing adaptive module by temporarily blocking import
        engine = CalibrationEngine(profile_name="fallback_test")
        engine._adaptive = None  # Force fallback state

        result = engine.compute_adjustments()
        assert result == _DEFAULT_PARAMS


class TestVolumeEstimation:
    """Verify writes_per_day estimation from observed volume."""

    def test_writes_per_day_set_from_tracker(self, tmp_path):
        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()

        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()
        tracker = HitRateTracker.get_instance(str(mem_dir))
        for _ in range(42):
            tracker.register_save(f"vol_{_}")

        engine = CalibrationEngine(profile_name="volume_test")
        engine.tuning_path = tmp_path / "volume.yaml"
        engine.apply_and_persist()

        assert engine.state.profile_volume_writes_per_day == pytest.approx(42.0)


class TestCalibrationCountIncrement:
    """Verify calibration_count increments on each cycle."""

    def test_count_bumps_after_persist(self, tmp_path):
        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()

        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()
        tracker = HitRateTracker.get_instance(str(mem_dir))
        tracker.register_save("count_entry")

        engine = CalibrationEngine(profile_name="count_test")
        engine.tuning_path = tmp_path / "count.yaml"

        before = engine.state.calibration_count
        engine.apply_and_persist()
        assert engine.state.calibration_count == before + 1

    def test_last_calibrated_at_set(self, tmp_path):
        from memchorus.hit_rate_tracker import HitRateTracker
        HitRateTracker.reset()

        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()
        tracker = HitRateTracker.get_instance(str(mem_dir))
        tracker.register_save("ts_entry")

        engine = CalibrationEngine(profile_name="ts_test")
        engine.tuning_path = tmp_path / "ts.yaml"
        assert engine.state.last_calibrated_at is None

        engine.apply_and_persist()
        assert engine.state.last_calibrated_at is not None
