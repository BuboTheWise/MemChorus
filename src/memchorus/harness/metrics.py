"""Outcome metrics for the GH-271 A/B evaluation harness.

This module is the *measurement contract* for the outcome harness: a small,
dependency-free data model that records, for each scripted scenario, how arm A
(the un-tuned baseline) and arm B (calibration applied) each performed, the
delta between them, and whether the pair crosses a pre-registered pass line.

Design rules (issue #271 acceptance criteria):

* Metrics are *outcomes* — did the agent succeed at a scripted task, did it
  re-ask for the same fact, how many tokens were injected per turn, how slow
  was the recall path, and did stale / noise memories surface where they
  should not — not raw retrieval hit-rate.
* Every metric is pre-registered with a **direction** (``higher_is_better`` or
  ``lower_is_better``) and a **pass line** *before* the run, so the harness is
  evaluated against a target, not reverse-engineered from the output.
* The report is JSON-serialisable and version-stamped so a baseline committed
  under ``benchmarks/`` or ``eval/`` is comparable across runs (the same
  discipline ``eval/run_recall_gate.py`` already applies).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# Direction the metric must move to count as an improvement.
HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"
_VALID_DIRECTIONS = (HIGHER_IS_BETTER, LOWER_IS_BETTER)


def _finite(value: Any) -> bool:
    """True if *value* is a finite real number (guards a NaN/Inf from breaking
    JSON round-trip and the pass-line comparison)."""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


@dataclass
class ScenarioMetric:
    """Result of running ONE scripted scenario against both arms.

    ``arm_a`` is the un-tuned baseline measurement; ``arm_b`` is the
    calibration-applied measurement. ``delta`` is ``arm_b - arm_a``. The
    scenario *passes* when the delta moves in the pre-registered ``direction``
    by at least ``pass_line`` (a magnitude threshold; ``0.0`` means "must not
    regress").
    """

    scenario_id: str
    name: str
    metric_name: str
    description: str = ""
    direction: str = HIGHER_IS_BETTER
    arm_a: float = 0.0
    arm_b: float = 0.0
    pass_line: float = 0.0
    # Fact keys the scenario's calibration arm treated as proven-useful /
    # confirmed-noise.  Populated by the runner from the scenario definition
    # (single source of truth) so ``calibration_bridge`` can seed the real
    # tracker with the *same* keys the engine reads — not a scenario id.
    useful_keys: Tuple[str, ...] = field(default_factory=tuple)
    noise_keys: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"unknown direction {self.direction!r}; expected one of {_VALID_DIRECTIONS}"
            )
        if not all(_finite(v) for v in (self.arm_a, self.arm_b, self.pass_line)):
            raise ValueError(
                f"{self.scenario_id}: arm_a/arm_b/pass_line must be finite "
                f"(got a={self.arm_a!r} b={self.arm_b!r} line={self.pass_line!r})"
            )

    @property
    def delta(self) -> float:
        """Change in the raw measurement from baseline to tuned arm."""
        return self.arm_b - self.arm_a

    @property
    def passed(self) -> bool:
        """Whether this scenario meets its pre-registered pass line.

        A *higher-is-better* scenario passes when it did not regress AND the
        improvement is at least ``pass_line``. A *lower-is-better* scenario
        (costs: tokens, latency, stale/fact rate) passes when it did not
        regress AND the reduction is at least ``pass_line``.
        """
        if self.direction == HIGHER_IS_BETTER:
            return self.delta >= -1e-9 and self.delta >= self.pass_line
        return self.delta <= 1e-9 and abs(self.delta) >= self.pass_line

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "metric": self.metric_name,
            "description": self.description,
            "direction": self.direction,
            "arm_a": self.arm_a,
            "arm_b": self.arm_b,
            "delta": round(self.delta, 9),
            "pass_line": self.pass_line,
            "passed": self.passed,
        }


@dataclass
class OutcomeReport:
    """A complete harness run: one :class:`ScenarioMetric` per scripted
    scenario, plus enough provenance to make a committed baseline reproducible.
    """

    scenarios: List[ScenarioMetric] = field(default_factory=list)
    version: str = ""
    timestamp: str = ""
    profile: str = "harness"

    @classmethod
    def create(
        cls,
        scenarios: List[ScenarioMetric],
        *,
        version: str,
        profile: str = "harness",
    ) -> "OutcomeReport":
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return cls(scenarios=list(scenarios), version=version,
                   timestamp=now, profile=profile)

    @property
    def overall_passed(self) -> bool:
        """True when every scenario meets its pass line AND there is at least
        one scenario (an empty run is never a pass)."""
        return len(self.scenarios) > 0 and all(s.passed for s in self.scenarios)

    def failing(self) -> List[ScenarioMetric]:
        return [s for s in self.scenarios if not s.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_kind": "memchorus_outcome_harness",
            "profile": self.profile,
            "version": self.version,
            "timestamp": self.timestamp,
            "overall_passed": self.overall_passed,
            "scenario_count": len(self.scenarios),
            "scenarios": [s.to_dict() for s in self.scenarios],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutcomeReport":
        scenarios = [
            ScenarioMetric(
                scenario_id=s["scenario_id"],
                name=s.get("name", s["scenario_id"]),
                metric_name=s.get("metric", s.get("metric_name", "")),
                description=s.get("description", ""),
                direction=s.get("direction", HIGHER_IS_BETTER),
                arm_a=float(s.get("arm_a", 0.0)),
                arm_b=float(s.get("arm_b", 0.0)),
                pass_line=float(s.get("pass_line", 0.0)),
            )
            for s in data.get("scenarios", [])
        ]
        return cls(
            scenarios=scenarios,
            version=data.get("version", ""),
            timestamp=data.get("timestamp", ""),
            profile=data.get("profile", "harness"),
        )

    # -- comparison against a committed baseline --------------------------
    def compare_to(self, baseline: "OutcomeReport") -> Dict[str, Any]:
        """Per-scenario drift of *this* run's tuned-arm measurement against the
        baseline run's tuned-arm measurement.

        Returns ``{scenario_id: {baseline_b, current_b, drift, direction,
        regressed}}`` where ``regressed`` is True when the current measurement
        moved *against* the scenario's direction by any meaningful amount.
        """
        out: Dict[str, Any] = {}
        base_by_id = {s.scenario_id: s for s in baseline.scenarios}
        for s in self.scenarios:
            base = base_by_id.get(s.scenario_id)
            if base is None:
                continue
            drift = s.arm_b - base.arm_b
            regressed = (
                drift < -1e-9 if s.direction == HIGHER_IS_BETTER else drift > 1e-9
            )
            out[s.scenario_id] = {
                "baseline_b": base.arm_b,
                "current_b": s.arm_b,
                "drift": round(drift, 9),
                "direction": s.direction,
                "regressed": regressed,
            }
        return out


__all__ = [
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "ScenarioMetric",
    "OutcomeReport",
]
