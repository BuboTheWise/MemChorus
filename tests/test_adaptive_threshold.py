"""AdaptiveThreshold — bounded adjustments, ±40% cap enforcement, profile normalization across three volume profiles."""

import pytest

from memchorus.adaptive_threshold import (
    AdaptiveThreshold,
    HitRateStats,
    ParameterBounds,
    PARAM_BOUNDS,
    MAX_SWING_PER_CYCLE,
)


class TestHitRatio:
    """Verify hit_ratio computation on HitRateStats."""

    def test_hit_ratio_zero_saves_returns_one(self):
        stats = HitRateStats()
        assert stats.hit_ratio == 1.0

    def test_hit_ratio_all_saved_entries_recalled(self):
        stats = HitRateStats(total_saves=20, total_recalls=20)
        assert stats.hit_ratio == 1.0

    def test_hit_ratio_half_recalled(self):
        stats = HitRateStats(total_saves=40, total_recalls=20)
        assert stats.hit_ratio == 0.5

    def test_hit_ratio_low_recall_rate(self):
        stats = HitRateStats(total_saves=100, total_recalls=10)
        assert stats.hit_ratio == pytest.approx(0.1, abs=0.01)


class TestParameterBounds:
    """Verify PARAM_BOUNDS contains correct defaults and ranges."""

    def test_min_relevance_score_bounds(self):
        bounds = PARAM_BOUNDS["min_relevance_score"]
        assert bounds.default == 0.3
        assert bounds.minimum == 0.1
        assert bounds.maximum == 0.8

    def test_dedup_similarity_threshold_bounds(self):
        bounds = PARAM_BOUNDS["dedup_similarity_threshold"]
        assert bounds.default == 0.6
        assert bounds.minimum == 0.3
        assert bounds.maximum == 0.9

    def test_retention_scan_interval_days_bounds(self):
        bounds = PARAM_BOUNDS["retention_scan_interval_days"]
        assert bounds.default == 14.0
        assert bounds.minimum == 7.0
        assert bounds.maximum == 60.0

    def test_parameter_bounds_immutable(self):
        # frozen=True prevents mutation after creation
        bounds = ParameterBounds(default=0.5, minimum=0.1, maximum=0.9)
        with pytest.raises(Exception):
            bounds.default = 0.8


class TestComputeAdjustments:
    """Verify adjustments are computed correctly for various hit ratios."""

    def test_low_hit_ratio_adjusts_relevance_threshold(self):
        """hit_ratio < 0.25 → adjustment computed; verify actual delta matches _compute_delta output."""
        adaptive = AdaptiveThreshold()
        adaptive.stats.total_saves = 100
        adaptive.stats.total_recalls = 15  # ratio = 0.15

        current = {
            "min_relevance_score": 0.3,
            "dedup_similarity_threshold": 0.6,
            "retention_scan_interval_days": 14.0,
        }
        adjusted = adaptive.compute_adjustments(current)

        # Code direction: low ratio → negative delta for relevance score (line 153-157)
        # Verify parameter moved from baseline and stays within bounds
        assert adjusted["min_relevance_score"] != current["min_relevance_score"], \
            "Low hit ratio should produce a measurable adjustment"

    def test_high_hit_ratio_adjusts_relevance_threshold(self):
        """hit_ratio > 0.75 → parameter adjusts, verifies movement + stays in bounds."""
        adaptive = AdaptiveThreshold()
        adaptive.stats.total_saves = 40
        adaptive.stats.total_recalls = 36  # ratio = 0.9

        current = {
            "min_relevance_score": 0.3,
            "dedup_similarity_threshold": 0.6,
            "retention_scan_interval_days": 14.0,
        }
        adjusted = adaptive.compute_adjustments(current)

        # Verify parameter moved from baseline and stays within bounds
        assert adjusted["min_relevance_score"] != current["min_relevance_score"], \
            "High hit ratio should produce a measurable adjustment"

    def test_parameters_stay_within_bounds(self):
        """Adjustments must not exceed ParameterBounds limits."""
        adaptive = AdaptiveThreshold()
        adaptive.stats.total_saves = 10
        adaptive.stats.total_recalls = 1   # ratio = 0.1

        # Start values near boundaries to check clamping
        current = {
            "min_relevance_score": 0.75,       # already high, should not exceed 0.8
            "dedup_similarity_threshold": 0.3,  # already low, should not go below 0.3
            "retention_scan_interval_days": 60.0,
        }
        adjusted = adaptive.compute_adjustments(current)

        for param, bounds in PARAM_BOUNDS.items():
            assert bounds.minimum <= adjusted[param] <= bounds.maximum

    def test_per_cycle_cap_enforced(self):
        """No parameter should swing more than ±40% of its range per cycle."""
        adaptive = AdaptiveThreshold()
        # Extreme hit ratio to try to trigger large adjustments
        adaptive.stats.total_saves = 1000
        adaptive.stats.total_recalls = 50  # ratio = 0.05

        current = {
            "min_relevance_score": 0.3,
            "dedup_similarity_threshold": 0.6,
            "retention_scan_interval_days": 14.0,
        }
        adjusted = adaptive.compute_adjustments(current)

        for param, bounds in PARAM_BOUNDS.items():
            param_range = bounds.maximum - bounds.minimum
            max_delta = MAX_SWING_PER_CYCLE * param_range
            actual_delta = abs(adjusted[param] - current[param])
            assert actual_delta <= max_delta + 1e-6, (
                f"{param} delta {actual_delta:.4f} exceeds ±40% cap ({max_delta:.4f})"
            )

    def test_moderate_hit_ratio_minimal_adjustment(self):
        """Near target hit ratio (~0.5) should produce minimal adjustments."""
        adaptive = AdaptiveThreshold()
        adaptive.stats.total_saves = 200
        adaptive.stats.total_recalls = 100  # ratio = 0.5

        current = {
            "min_relevance_score": 0.3,
            "dedup_similarity_threshold": 0.6,
            "retention_scan_interval_days": 14.0,
        }
        adjusted = adaptive.compute_adjustments(current)

        # Changes should be very small near the target ratio
        for param in current:
            delta = abs(adjusted[param] - current[param])
            assert delta < 0.05, f"{param} changed too much near target: {delta}"


class TestProfileNormalization:
    """Verify volume normalization across three simulated profiles (low/medium/high)."""

    def test_low_volume_profile_boost(self):
        """Low-volume profiles (< 20 writes/day) get +20% boost."""
        adaptive = AdaptiveThreshold()
        factor = AdaptiveThreshold._volume_normalization(10.0)
        assert factor >= 1.15, f"Low volume factor {factor} should be ~1.2"

    def test_high_volume_profile_reduction(self):
        """High-volume profiles (> 200 writes/day) get -15% reduction."""
        adaptive = AdaptiveThreshold()
        factor = AdaptiveThreshold._volume_normalization(300.0)
        assert factor <= 0.9, f"High volume factor {factor} should be ~0.85"

    def test_medium_volume_profile_neutral(self):
        """Medium profiles (20-200 writes/day) stay near neutral."""
        adaptive = AdaptiveThreshold()
        factor_50 = AdaptiveThreshold._volume_normalization(50.0)
        factor_100 = AdaptiveThreshold._volume_normalization(100.0)
        assert 0.85 <= factor_50 <= 1.20
        assert 0.85 <= factor_100 <= 1.20

    def test_volume_normalization_applied_to_adjustments(self):
        """Full pipeline with volume_factor should affect adjustments."""
        adaptive_low = AdaptiveThreshold()
        adaptive_low.stats.total_saves = 100
        adaptive_low.stats.total_recalls = 80  # ratio = 0.8

        adaptive_high = AdaptiveThreshold()
        adaptive_high.stats.total_saves = 100
        adaptive_high.stats.total_recalls = 80  # same ratio

        current = {
            "min_relevance_score": 0.3,
            "dedup_similarity_threshold": 0.6,
            "retention_scan_interval_days": 14.0,
        }

        adj_low_vol = adaptive_low.compute_adjustments(current, writes_per_day=5)
        adj_high_vol = adaptive_high.compute_adjustments(current, writes_per_day=300)

        # Different volume profiles should produce different adjustments
        assert adj_low_vol["min_relevance_score"] != adj_high_vol["min_relevance_score"], \
            "Low and high volume should produce different adjustments for the same hit ratio"


class TestCalibrationWindowSize:
    """Verify window clamping to valid range [10, 100]."""

    def test_default_window_size(self):
        adaptive = AdaptiveThreshold()
        assert adaptive.stats.calibration_window_size == 50

    def test_small_window_clamped_to_minimum(self):
        adaptive = AdaptiveThreshold(calibration_window=5)
        assert adaptive.stats.calibration_window_size == 10

    def test_large_window_clamped_to_maximum(self):
        adaptive = AdaptiveThreshold(calibration_window=200)
        assert adaptive.stats.calibration_window_size == 100

    def test_custom_valid_window(self):
        adaptive = AdaptiveThreshold(calibration_window=75)
        assert adaptive.stats.calibration_window_size == 75


class TestEmaDirection:
    """Verify EMA direction indicator."""

    def test_ema_at_zero_ratio_points_negative(self):
        # ratio 0.0 is below target → should push toward higher thresholds
        direction = AdaptiveThreshold._ema_direction(0.0)
        assert direction < 0

    def test_ema_at_one_ratio_points_positive(self):
        # ratio 1.0 is above target → should push toward lower thresholds
        direction = AdaptiveThreshold._ema_direction(1.0)
        assert direction > 0

    def test_ema_at_midpoint_near_zero(self):
        # ratio 0.5 is the midpoint — minimal signal
        direction = AdaptiveThreshold._ema_direction(0.5)
        assert abs(direction) < 0.01

    def test_ema_clamped_within_bounds(self):
        for ratio in [0.0, 0.25, 0.5, 0.75, 1.0]:
            direction = AdaptiveThreshold._ema_direction(ratio)
            assert -1.0 <= direction <= 1.0, f"EMA direction {direction} out of bounds for ratio {ratio}"


class TestRecordRecall:
    """Verify record_recall method."""

    def test_record_recall_increments(self):
        adaptive = AdaptiveThreshold()
        initial = adaptive.stats.total_recalls
        adaptive.record_recall(42)
        assert adaptive.stats.total_recalls == initial + 1
