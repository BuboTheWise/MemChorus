"""Adaptive threshold adjustment based on rolling hit-rate statistics.

Part of the MemChorus auto-tuning framework (v1.8.0).
See docs/AUTOTUNING.md for design rationale and integration spec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# --- Parameter bounds and v1.7.0 defaults --------------------------------


@dataclass(frozen=True)
class ParameterBounds:
    """Immutable min/max/default for one tunable parameter."""

    default: float
    minimum: float
    maximum: float


# Maps parameter name → bounds/defaults
PARAM_BOUNDS: Dict[str, ParameterBounds] = {
    "min_relevance_score": ParameterBounds(default=0.3, minimum=0.1, maximum=0.8),
    "dedup_similarity_threshold": ParameterBounds(default=0.6, minimum=0.3, maximum=0.9),
    "retention_scan_interval_days": ParameterBounds(default=14.0, minimum=7.0, maximum=60.0),
}

MAX_SWING_PER_CYCLE = 0.40  # 40 % hard-cap on any single adjustment


# --- Hit-rate statistics -------------------------------------------------


@dataclass
class HitRateStats:
    """Rolling hit-ratio over a configurable calibration window."""

    total_saves: int = 0
    total_recalls: int = 0
    calibration_window_size: int = 50
    recent_entries: List[int] = field(default_factory=list)

    @property
    def hit_ratio(self) -> float:
        """Recall efficiency — fraction of saved entries that were recalled."""
        if self.total_saves == 0:
            return 1.0
        return self.total_recalls / self.total_saves


# --- AdaptiveThreshold ---------------------------------------------------


class AdaptiveThreshold:
    """Adjusts v1.7.0 static parameters using empirical hit-rate data.

    Design goals:
    1. ≤ 15 µs inline overhead — all work happens during calibration cycles,
       never on the save/recall hot path.
    2. Bounded ±40 % swing per cycle prevents runaway feedback.
    3. Profile normalization so code-review-heavy profiles do not starve
       low-frequency researcher profiles.
    """

    HIT_RATIO_LOW_BOUND = 0.25   # below → increase thresholds (fewer saves)
    HIT_RATIO_HIGH_BOUND = 0.75  # above → decrease thresholds (more saves)
    EMA_ALPHA = 0.3              # exponential moving average dampening factor

    def __init__(self, calibration_window: int | None = None) -> None:
        window_size = max(10, min(100, calibration_window)) if calibration_window is not None else 50
        self.stats = HitRateStats(calibration_window_size=window_size)

    # -- public API -------------------------------------------------------

    def record_recall(self, entry_key_id: int | None = None) -> None:
        self.stats.total_recalls += 1

    def compute_adjustments(self, current: Dict[str, float],
                           writes_per_day: float | None = None) -> Dict[str, float]:
        """Return adjusted parameter values bounded per-cycle."""
        ratio = self.stats.hit_ratio
        ema_adjustment = self._ema_direction(ratio)

        volume_factor = 1.0
        if writes_per_day is not None:
            volume_factor = self._volume_normalization(writes_per_day)

        adjusted: Dict[str, float] = {}
        for param, bounds in PARAM_BOUNDS.items():
            old_val = current.get(param, bounds.default)
            delta = self._compute_delta(param, ratio, ema_adjustment, volume_factor)
            new_val = max(bounds.minimum, min(bounds.maximum, old_val + delta))
            new_val = self._apply_per_cycle_cap(new_val, current.get(param, bounds.default), param)
            adjusted[param] = round(new_val, 4)
            logger.debug("adjusted %s: %.4f → %.4f (delta=%.4f)",
                         param, current.get(param, bounds.default), new_val, delta)

        return adjusted

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _ema_direction(hit_ratio: float, alpha: float | None = None) -> float:
        """Exponential-moving-average smoothed direction indicator.

        Returns a signed fraction in [-1, +1]:
        negative → ratio is trending below target (raise thresholds),
        positive → ratio is trending above target (lower thresholds).
        """
        alpha = alpha or AdaptiveThreshold.EMA_ALPHA
        midpoint = 0.5
        signed_dist = hit_ratio - midpoint
        return max(-1.0, min(1.0, signed_dist / midpoint)) * (1.0 - alpha)

    @staticmethod
    def _volume_normalization(writes_per_day: float) -> float:
        """Scale factor for profile volume.

        Low-volume profiles (< 20 writes/day) get a +20 % boost to thresholds,
        high-volume (> 200 writes/day) get -15 % so they don't over-prune valid entries.
        Medium stays at neutral (1.0).
        """
        if writes_per_day < 20:
            return 1.20
        elif writes_per_day > 200:
            return 0.85
        else:
            # Linear interpolation between 20 and 200
            scale = 1.0 + (1.20 - 1.0) * max(0, (200.0 - writes_per_day) / 180.0)
            return max(0.85, min(1.20, scale))

    def _compute_delta(self, param: str, ratio: float, ema_dir: float, volume_factor: float) -> float:
        """Compute per-parameter delta based on hit ratio and volume."""
        bounds = PARAM_BOUNDS[param]
        param_range = bounds.maximum - bounds.minimum

        if param == "min_relevance_score":
            # Low hit_ratio (< 0.25): too much saved, few recalled → RAISE the
            # relevance floor so future saves are higher-precision.
            # High hit_ratio (> 0.75): recall efficiency is good → LOWER the
            # floor to admit more (we weren't over-saving in the first place).
            # Direction is the sign applied to the magnitude; a positive value
            # raises the parameter, a negative lowers it.
            direction = 1 if ratio < self.HIT_RATIO_LOW_BOUND else (-1 if ratio > self.HIT_RATIO_HIGH_BOUND else 0)
            magnitude = abs(ratio - 0.5) * param_range * volume_factor
        elif param == "dedup_similarity_threshold":
            # Same semantics: low ratio (over-saving) → tighten dedup (raise the
            # similarity threshold so near-duplicates are suppressed more
            # aggressively); high ratio (good precision) → relax dedup so more
            # saves get through.
            direction = 1 if ratio < self.HIT_RATIO_LOW_BOUND else (-1 if ratio > self.HIT_RATIO_HIGH_BOUND else 0)
            magnitude = abs(ratio - 0.5) * param_range * 0.3  # gentler adjustments for similarity
        elif param == "retention_scan_interval_days":
            # The inverse of the recall-side thresholds: a LOW ratio means we
            # over-saved, so RELAX the scan interval (longer between sweeps);
            # a HIGH ratio means recall is healthy, so TIGHTEN the interval
            # (more frequent sweeps).
            direction = -1 if ratio > self.HIT_RATIO_HIGH_BOUND else (1 if ratio < self.HIT_RATIO_LOW_BOUND else 0)
            magnitude = abs(ratio - 0.5) * param_range * 0.2  # very gentle for intervals
        else:
            return 0.0

        delta = direction * magnitude * max(0.03, abs(ema_dir))
        return round(delta, 4)

    @staticmethod
    def _apply_per_cycle_cap(new_val: float, old_val: float, param: str) -> float:
        """Hard ±40 % cap relative to the parameter's range."""
        bounds = PARAM_BOUNDS[param]
        param_range = bounds.maximum - bounds.minimum
        max_delta = MAX_SWING_PER_CYCLE * param_range
        actual_delta = new_val - old_val

        if abs(actual_delta) > max_delta:
            capped = round(old_val + max(min(actual_delta, max_delta), -max_delta), 4)
            logger.warning(
                "parameter %s adjustment %.4f exceeded ±%.0f%% cap (range=%.2f); "
                "capped to delta=%.4f",
                param, actual_delta, MAX_SWING_PER_CYCLE * 100,
                param_range, capped - old_val,
            )
            return capped

        return new_val
