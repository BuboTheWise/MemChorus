"""Calibration engine - aggregates hit-rate + mistake signals into parameter adjustments.

Piggybacks existing retention sweep infrastructure (no new scheduler).
Per-profile tuning state stored at:
    ~/.hermes/data/memchorus/_tuning/<profile_name>.yaml

CLI entry point: `memchorus recalibrate`

See docs/AUTOTUNING.md for the full auto-tuning framework spec.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TUNING_DIR = Path.home() / ".hermes" / "data" / "memchorus" / "_tuning"

# v1.7.0 defaults for comparison
_DEFAULT_PARAMS: Dict[str, float] = {
    "min_relevance_score": 0.3,
    "dedup_similarity_threshold": 0.6,
    "retention_scan_interval_days": 14.0,
}


@dataclass
class CalibrationState:
    """Per-profile calibration configuration persisted as YAML."""

    min_relevance_score: float = _DEFAULT_PARAMS["min_relevance_score"]
    dedup_similarity_threshold: float = _DEFAULT_PARAMS["dedup_similarity_threshold"]
    retention_scan_interval_days: float = _DEFAULT_PARAMS["retention_scan_interval_days"]
    calibration_window: int = 50
    profile_volume_writes_per_day: float = 0.0
    last_calibrated_at: str | None = None
    calibration_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calibration_window": self.calibration_window,
            "min_relevance_score": self.min_relevance_score,
            "dedup_similarity_threshold": self.dedup_similarity_threshold,
            "retention_scan_interval_days": self.retention_scan_interval_days,
            "profile_volume_writes_per_day": self.profile_volume_writes_per_day,
            "last_calibrated_at": self.last_calibrated_at,
            "calibration_count": self.calibration_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CalibrationState:  # noqa: PLR0913
        return cls(
            calibration_window=int(data.get("calibration_window", 50)),
            min_relevance_score=float(data.get("min_relevance_score",
                                          _DEFAULT_PARAMS["min_relevance_score"])),
            dedup_similarity_threshold=float(data.get("dedup_similarity_threshold",
                                                   _DEFAULT_PARAMS["dedup_similarity_threshold"])),
            retention_scan_interval_days=float(data.get("retention_scan_interval_days",
                                                     _DEFAULT_PARAMS["retention_scan_interval_days"])),
            profile_volume_writes_per_day=float(
                data.get("profile_volume_writes_per_day", 0.0)
            ),
            last_calibrated_at=data.get("last_calibrated_at"),
            calibration_count=int(data.get("calibration_count", 0)),
        )


class CalibrationEngine:
    """Aggregates HitRateTracker + MistakeDetector into concrete parameter adjustments.

    Runs during the existing retention sweep so there is zero additional scheduling
    overhead. Designed for ≤ 15 us inline impact - all heavy work happens off-path.
    Falls back to v1.7.0 static defaults when optional dependencies are unavailable.
    """

    MIN_OBSERVATIONS = 3  # AC-RTB-MIN — minimum feedback events before boosting

    def __init__(self, profile_name: str = "default") -> None:
        self.profile_name = profile_name
        self.tuning_path = DEFAULT_TUNING_DIR / f"{profile_name}.yaml"
        self.state = self._load_state()

        # Lazy-load optional dependencies (graceful degradation)
        self._adaptive = None
        try:
            from memchorus.adaptive_threshold import AdaptiveThreshold
            win = self.state.calibration_window
            self._adaptive = AdaptiveThreshold(calibration_window=win)
        except (ImportError, ModuleNotFoundError):
            logger.info("AdaptiveThreshold not available - using v1.7.0 static defaults")

        self._tracker_stats: Dict[str, int] = {"saves": 0, "recalls": 0}
        self._mistake_flags: Dict[str, int] = {"noise": 0, "useful": 0}

    # -- persistence -------------------------------------------------------

    def _load_state(self) -> CalibrationState:
        """Read existing tuning YAML or initialise fresh defaults."""
        if self.tuning_path.exists():
            try:
                import yaml  # noqa: TID251
                raw = yaml.safe_load(self.tuning_path.read_text())
                if isinstance(raw, dict):
                    state = CalibrationState.from_dict(raw)
                    logger.info(
                        "loaded tuning config for profile %r (%s)",
                        self.profile_name, self.tuning_path,
                    )
                    return state
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to parse %s: %s - falling back to defaults",
                               self.tuning_path, exc)
        return CalibrationState()

    def _save_state(self) -> None:
        """Write current calibration state back to YAML."""
        try:
            import yaml  # noqa: TID251
        except ImportError:
            logger.warning("PyYAML not installed - cannot persist tuning to %s",
                           self.tuning_path)
            return

        self.tuning_path.parent.mkdir(parents=True, exist_ok=True)
        self.tuning_path.write_text(yaml.dump(self.state.to_dict(), default_flow_style=False))

    # -- data aggregation --------------------------------------------------

    def aggregate_hit_rate_stats(self) -> Tuple[int, int]:
        """Pull save/recall counts from HitRateTracker if available."""
        saves = 0
        recalls = 0
        try:
            from memchorus.hit_rate_tracker import HitRateTracker
            tracker = HitRateTracker.get_instance()
            saves = tracker.total_saves
            recalls = tracker.total_recalls
        except (ImportError, ModuleNotFoundError):
            logger.debug("HitRateTracker unavailable - skipping hit-rate aggregation")

        self._tracker_stats["saves"] = saves
        self._tracker_stats["recalls"] = recalls
        return saves, recalls

    def aggregate_mistake_flags(self) -> Tuple[int, int]:
        """Pull noise/useful flags from MistakeDetector if available."""
        noise = 0
        useful = 0
        try:
            from memchorus.mistake_detector import MistakeDetector
            detector = MistakeDetector.get_instance()
            noise = detector.total_noise_flags
            useful = detector.total_useful_flags
        except (ImportError, ModuleNotFoundError):
            logger.debug("MistakeDetector unavailable - skipping mistake aggregation")

        self._mistake_flags["noise"] = noise
        self._mistake_flags["useful"] = useful
        return noise, useful

    # -- calibration core --------------------------------------------------

    def compute_adjustments(self) -> Dict[str, float]:
        """Run the full calibration pipeline and return new parameter values.

        1. Aggregate statistics from HitRateTracker / MistakeDetector.
        2. Feed counts into AdaptiveThreshold for bounded adjustment.
        3. Return a mapping of all three parameters to their new values.
        """
        saves, recalls = self.aggregate_hit_rate_stats()

        # If no adaptive module loaded, return current defaults unchanged.
        if self._adaptive is None:
            logger.warning(
                "AdaptiveThreshold unavailable - returning unchanged params for %r",
                self.profile_name,
            )
            return dict(_DEFAULT_PARAMS)

        # Feed observed counts directly into AdaptiveThreshold stats.
        self._adaptive.stats.total_saves = max(saves, 1)
        self._adaptive.stats.total_recalls = recalls

        current: Dict[str, float] = {
            "min_relevance_score": self.state.min_relevance_score,
            "dedup_similarity_threshold": self.state.dedup_similarity_threshold,
            "retention_scan_interval_days": self.state.retention_scan_interval_days,
        }

        return self._adaptive.compute_adjustments(
            current, writes_per_day=self.state.profile_volume_writes_per_day or None
        )

    def apply_and_persist(self) -> Dict[str, float]:
        """Compute adjustments, update in-memory state, and persist to disk.

        Returns the final parameter mapping (same keys as _DEFAULT_PARAMS).
        """
        adjusted = self.compute_adjustments()

        # Update tuning state with new values.
        self.state.min_relevance_score = round(adjusted["min_relevance_score"], 4)
        self.state.dedup_similarity_threshold = round(adjusted["dedup_similarity_threshold"], 4)
        self.state.retention_scan_interval_days = round(adjusted["retention_scan_interval_days"], 4)
        self.state.last_calibrated_at = datetime.now(timezone.utc).isoformat()
        self.state.calibration_count += 1

        # Estimate writes_per_day from observed volume (rough heuristic based on tracker).
        saves = self._tracker_stats.get("saves", 0)
        if saves > 0:
            self.state.profile_volume_writes_per_day = round(float(saves), 1)

        self._save_state()

        logger.info(
            "calibration complete for profile %r (cycle #%d): %s",
            self.profile_name, self.state.calibration_count, adjusted,
        )

        return adjusted

    @staticmethod
    def _compute_boost_from_flags(useful: int, noise: int) -> float:
        """Core boost computation for useful/noise flag counts (AC-RTB-1.x).

        Returns the multiplicative score factor in [0.5, 3.0].
        Requires >= MIN_OBSERVATIONS combined flags to deviate from baseline (1.0).
        """
        total = useful + noise

        # Not enough data — no adjustment
        if total < CalibrationEngine.MIN_OBSERVATIONS:
            return 1.0

        hit_rate = useful / total

        # Piecewise mapping from [0, 1] to [0.5, 3.0]:
        if hit_rate >= 0.8:
            boost = 2.0 + (hit_rate - 0.8) * 5.0  # 2.0 at 0.8 -> 3.0 at 1.0
        elif hit_rate < 0.3:
            boost = max(0.5, min(0.6, 0.5 + hit_rate * 2.0))
        else:
            # [0.3, 0.8) -> [0.6, 2.0) — linear interpolation
            boost = 0.6 + (hit_rate - 0.3) * (2.0 - 0.6) / (0.8 - 0.3)

        return float(max(0.5, min(3.0, boost)))

    def boost_factor_for_key(self, key: str) -> float:
        """Recall-time relevance boost multiplier for a single memory key (v1.9).

        Reads HitRateTracker utility signals and returns a score multiplier
        that ranks proven high-utility memories above unproven or low-utility ones.

        Mapping (piecewise, continuous at boundaries):
            hit_rate >= 0.8  -> [2.0, 3.0]   (high utility)
            hit_rate in [0.3, 0.8) -> [0.6, 2.0) (neutral zone)
            hit_rate < 0.3    -> [0.5, 0.6]   (mistake / confirmed waste)

        Falls back to exactly 1.0 when calibration data is absent or unavailable.
        """
        try:
            from memchorus.hit_rate_tracker import HitRateTracker
            tracker = HitRateTracker.get_instance()
            stats = tracker._index.get(key)

            # No history for this key -> unchanged scoring
            if not stats:
                return 1.0

            useful = stats.get("useful_flags", 0)
            noise = stats.get("noise_flags", 0)

            return self._compute_boost_from_flags(useful, noise)
        except Exception:
            logger.debug("boost_factor_for_key unavailable for key %r — returning 1.0", key)
            return 1.0

    @classmethod
    def get_adjusted_params(cls, profile_name: str = "default") -> Dict[str, float]:
        """Class-level convenience to look up current params without triggering calibration.

        Useful for MemoryOrchestrator.__init__ which needs the active thresholds at boot.
        Returns persisted values if they exist, otherwise v1.7.0 defaults.
        """
        engine = cls(profile_name)
        # Do NOT recalibrate here - just read what's on disk.
        return {
            "min_relevance_score": engine.state.min_relevance_score,
            "dedup_similarity_threshold": engine.state.dedup_similarity_threshold,
            "retention_scan_interval_days": engine.state.retention_scan_interval_days,
        }


def main() -> None:
    """CLI entry point for `memchorus recalibrate`."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="memchorus-recalibrate",
        description="Run a manual calibration cycle for MemChorus auto-tuning.",
    )
    parser.add_argument(
        "--profile", default=os.environ.get("HERMES_PROFILE") or "default",
        help="Hermes profile name (default: from env or 'default')",
    )
    args = parser.parse_args()

    engine = CalibrationEngine(profile_name=args.profile)
    print(f"Calibrating profile: {args.profile}")
    print(f"Tuning file: {engine.tuning_path}")
    try:
        adjusted = engine.apply_and_persist()
        for key, val in adjusted.items():
            default_val = _DEFAULT_PARAMS.get(key, "?")
            print(f"  {key}: {default_val} -> {val}")
        print(f"Calibration cycle #{engine.state.calibration_count} persisted.")
    except Exception as exc:
        logger.error("calibration failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
