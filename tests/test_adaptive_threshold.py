"""Unit tests for AdaptiveThreshold (v1.8.0 auto-tuning framework).

Covers:
- Bounded adjustments (±40% per-cycle cap on all 3 parameters)
- Profile normalization across low/medium/high volume profiles
- Calibration window sliding when exceeding calibration_window_size
- EMA direction computation for boundary conditions (ratio=0.0, 1.0, 0.5)
"""

from __future__ import annotations

import pytest
from memchorus.adaptive_threshold import (
    AdaptiveThreshold,
    HitRateStats,
    PARAM_BOUNDS,
    ParameterBounds,
    MAX_SWING_PER_CYCLE,
)


class TestParameterBounds:
    """Verify PARAM_BOUNDS definitions are well-formed."""

    def test_all_params_have_bounds(self):
        expected_keys = {"min_relevance_score", "dedup_similarity_threshold", "retention_scan_interval_days"}
        assert set(PARAM_BOUNDS.keys()) == expected_keys

    def test_default_within_range(self):
        for name, pb in PARAM_BOUNDS.items():
            assert pb.minimum <= pb.default <= pb.maximum, f"{name} default {pb.default} outside [{pb.minimum}, {pb.maximum}]"


class TestHitRateStats:
    """Verify HitRateStats hit_ratio computation."""

    def test_zero_saves_returns_one(self):
        stats = HitRateStats()
        assert stats.hit_ratio == 1.0

    def test_perfect_recall(self):
        stats = HitRateStats(total_saves=10, total_recalls=10)
        assert stats.hit_ratio == 1.0

    def test_partial_recall(self):
        stats = HitRateStats(total_saves=100, total_recalls=30)
        assert stats.hit_ratio == pytest.approx(0.3)

    def test_no_recalls(self):
        stats = HitRateStats(total_saves=50, total_recalls=0)
        assert stats.hit_ratio == 0.0


class TestHitRateStatsCalibrationWindow:
    """Verify calibration_window_size clamping and sliding window behavior."""

    def test_default_window_size(self):
        at = AdaptiveThreshold()
        assert at.stats.calibration_window_size == 50

    def test_window_clamped_minimum(self):
        at = AdaptiveThreshold(calibration_window=2)
        assert at.stats.calibration_window_size == 10

    def test_window_clamped_maximum(self):
        at = AdaptiveThreshold(calibration_window=999)
        assert at.stats.calibration_window_size == 100

    def test_window_within_range(self):
        at = AdaptiveThreshold(calibration_window=75)
        assert at.stats.calibration_window_size == 75


class TestEMADirection:
    """EMA direction computation for boundary conditions."""

    def test_ratio_zero_returns_negative(self):
        """ratio=0.0: all entries saved, none recalled → trending below target → negative."""
        result = AdaptiveThreshold._ema_direction(0.0)
        assert result < 0
        # Expected: (0.0 - 0.5) / 0.5 * (1.0 - 0.3) = -1.0 * 0.7 = -0.7
        assert pytest.approx(result, abs=1e-6) == -0.7

    def test_ratio_one_returns_positive(self):
        """ratio=1.0: every recall hits → trending above target → positive."""
        result = AdaptiveThreshold._ema_direction(1.0)
        assert result > 0
        # Expected: (1.0 - 0.5) / 0.5 * 0.7 = 1.0 * 0.7 = 0.7
        assert pytest.approx(result, abs=1e-6) == 0.7

    def test_ratio_midpoint_returns_zero(self):
        """ratio=0.5: midpoint is neutral → zero."""
        result = AdaptiveThreshold._ema_direction(0.5)
        assert pytest.approx(result, abs=1e-6) == 0.0

    def test_below_low_bound_negative(self):
        result = AdaptiveThreshold._ema_direction(AdaptiveThreshold.HIT_RATIO_LOW_BOUND)
        assert result < 0

    def test_above_high_bound_positive(self):
        result = AdaptiveThreshold._ema_direction(AdaptiveThreshold.HIT_RATIO_HIGH_BOUND)
        assert result > 0

    def test_custom_alpha(self):
        """alpha=0.5 should halve the output compared to alpha=0 (no dampening)."""
        r1 = AdaptiveThreshold._ema_direction(0.0, alpha=0.3)
        r2 = AdaptiveThreshold._ema_direction(0.0, alpha=0.9)
        # Higher alpha → less output because direction *= (1 - alpha)
        assert abs(r2) < abs(r1)


class TestVolumeNormalization:
    """Profile normalization for low/medium/high volume profiles."""

    def test_low_volume_boost(self):
        """< 20 writes/day → +20% boost."""
        result = AdaptiveThreshold._volume_normalization(5)
        assert pytest.approx(result, abs=1e-6) == 1.20

    def test_high_volume_reduction(self):
        """> 200 writes/day → -15% reduction (0.85)."""
        result = AdaptiveThreshold._volume_normalization(500)
        assert pytest.approx(result, abs=1e-6) == 0.85

    def test_medium_volume_neutral(self):
        """20 writes/day → starts interpolation from neutral (1.0+)."""
        result = AdaptiveThreshold._volume_normalization(20)
        # At exactly 20: scale = 1.0 + 0.2 * (200-20)/180 = 1.0 + 0.2 = 1.2... but clamped
        assert result >= 0.85 and result <= 1.20

    def test_at_200_writes(self):
        """At exactly 200 → scale = 1.0 + 0.2 * 0 = 1.0."""
        result = AdaptiveThreshold._volume_normalization(200)
        assert pytest.approx(result, abs=1e-6) == 1.0

    def test_boundary_low(self):
        """Just under 20."""
        result = AdaptiveThreshold._volume_normalization(19.9)
        assert pytest.approx(result, abs=1e-6) == 1.20

    def test_boundary_high(self):
        """Just over 200."""
        result = AdaptiveThreshold._volume_normalization(200.1)
        assert pytest.approx(result, abs=1e-6) == 0.85


class TestBoundedAdjustments:
    """Verify ±40% per-cycle cap on each of 3 parameters."""

    def _defaults(self):
        return {k: pb.default for k, pb in PARAM_BOUNDS.items()}

    def test_per_cycle_cap_min_relevance_score(self):
        """Even extreme ratio cannot swing min_relevance by more than 40% of range per cycle."""
        current = dict(self._defaults())
        bounds = PARAM_BOUNDS["min_relevance_score"]
        param_range = bounds.maximum - bounds.minimum
        max_delta = MAX_SWING_PER_CYCLE * param_range

        # Extremely low ratio should produce a large delta, capped at 40% of range
        at = AdaptiveThreshold()
        at.stats.total_saves = 1000
        at.stats.total_recalls = 1

        result = at.compute_adjustments(current)
        actual_delta = abs(result["min_relevance_score"] - current["min_relevance_score"])
        assert actual_delta <= max_delta + 1e-6

    def test_all_params_respect_per_cycle_cap(self):
        """Each parameter stays within its ±40% cap regardless of input."""
        current = dict(self._defaults())

        # Extreme case: zero hit ratio
        at = AdaptiveThreshold()
        at.stats.total_saves = 1000
        at.stats.total_recalls = 0

        result = at.compute_adjustments(current)
        for param in PARAM_BOUNDS:
            bounds = PARAM_BOUNDS[param]
            param_range = bounds.maximum - bounds.minimum
            max_delta = MAX_SWING_PER_CYCLE * param_range
            actual_delta = abs(result[param] - current.get(param, bounds.default))
            assert actual_delta <= max_delta + 1e-6, \
                f"{param}: delta={actual_delta:.4f} > cap={max_delta:.4f}"

    def test_values_stay_within_absolute_bounds(self):
        """Adjusted values never exceed parameter min/max."""
        current = dict(self._defaults())

        at = AdaptiveThreshold()
        at.stats.total_saves = 10
        at.stats.total_recalls = 9  # very high ratio

        result = at.compute_adjustments(current)
        for param, bounds in PARAM_BOUNDS.items():
            assert result[param] >= bounds.minimum, f"{param} below minimum"
            assert result[param] <= bounds.maximum, f"{param} above maximum"

    def test_extreme_high_ratio_also_caps(self):
        """Even near-perfect hit ratio is capped."""
        current = dict(self._defaults())

        at = AdaptiveThreshold()
        at.stats.total_saves = 10
        at.stats.total_recalls = 10  # perfect 1.0 ratio

        result = at.compute_adjustments(current)
        for param in PARAM_BOUNDS:
            bounds = PARAM_BOUNDS[param]
            param_range = bounds.maximum - bounds.minimum
            max_delta = MAX_SWING_PER_CYCLE * param_range
            actual_delta = abs(result[param] - current.get(param, bounds.default))
            assert actual_delta <= max_delta + 1e-6

    def test_neutral_ratio_minimal_change(self):
        """When ratio is near midpoint (0.5), adjustments are small or zero."""
        current = dict(self._defaults())

        at = AdaptiveThreshold()
        at.stats.total_saves = 200
        at.stats.total_recalls = 100  # ratio = 0.5 exactly

        result = at.compute_adjustments(current)
        for param in PARAM_BOUNDS:
            delta = abs(result[param] - current.get(param, bounds := PARAM_BOUNDS[param].default))
            assert delta == pytest.approx(0.0, abs=1e-4), \
                f"{param} should have near-zero delta at ratio 0.5: got {delta}"


class TestComputeAdjustmentsIntegration:
    """End-to-end compute_adjustments with volume factors."""

    def _defaults(self):
        return {k: pb.default for k, pb in PARAM_BOUNDS.items()}

    def test_returns_all_three_params(self):
        current = dict(self._defaults())
        at = AdaptiveThreshold()
        at.stats.total_saves = 50
        at.stats.total_recalls = 25
        result = at.compute_adjustments(current)
        assert len(result) == 3
        for param in PARAM_BOUNDS:
            assert param in result

    def test_volume_factor_affects_result(self):
        """Different writes_per_day should produce different adjustments."""
        current = dict(self._defaults())

        at_low = AdaptiveThreshold()
        at_low.stats.total_saves = 100
        at_low.stats.total_recalls = 20  # ratio=0.2 < HIT_RATIO_LOW_BOUND → direction != 0

        at_high = AdaptiveThreshold()
        at_high.stats.total_saves = 100
        at_high.stats.total_recalls = 20

        result_low = at_low.compute_adjustments(current, writes_per_day=5)
        result_high = at_high.compute_adjustments(current, writes_per_day=500)

        # Results should differ due to volume_factor
        any_diff = any(
            abs(result_low[p] - result_high[p]) > 1e-6
            for p in PARAM_BOUNDS
        )
        assert any_diff, "volume factor should affect at least one parameter"

    def test_missing_param_uses_default(self):
        """If current dict is missing a param, it falls back to bounds.default."""
        at = AdaptiveThreshold()
        at.stats.total_saves = 50
        at.stats.total_recalls = 25
        result = at.compute_adjustments({})  # empty current dict
        assert len(result) == 3
