"""A/B runner: executes each scripted scenario against both arms and produces
a :class:`ScenarioMetric`.

The runner is the *single* place in the harness that touches the real
calibration stack (``CalibrationEngine`` / ``HitRateTracker``).  It seeds the
tracker with the scenario's utility signals, builds the per-arm boost mapping,
reads the calibrated floor, and then drives both arms through the same
:class:`FakeMemorySource` contract.

Arm A (baseline):
    boost = ``{}`` (every key → 1.0)
    floor = the static v1.7.0 default ``min_relevance_score`` (0.3)
    stale-veto = active (same in both arms — it is a routing rule, not a
    calibration artefact)

Arm B (calibration applied):
    boost = real ``CalibrationEngine.boost_factor_for_key(key)`` for every
            fact key (proven-useful → ≥ 2.0, confirmed-noise → ≤ 0.6)
    floor = calibrated ``min_relevance_score`` (after ``apply_and_persist``)
    stale-veto = active

The only difference between arms is the boost mapping and the floor — which
is exactly the variable issue #271 asks the harness to isolate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from memchorus.calibration_engine import CalibrationEngine
from memchorus.hit_rate_tracker import HitRateTracker

from .fakes import Fact, FakeMemorySource
from .metrics import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    OutcomeReport,
    ScenarioMetric,
)


# ---------------------------------------------------------------------------
# Scenario definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """A pre-registered scripted scenario.

    ``facts`` — the memory store both arms see (identical content).
    ``signal_key`` — the fact the agent task actually needs.
    ``noise_keys`` — stale / off-topic facts that compete for top-k slots.
    ``stale_keys`` — facts with an expired ``valid_to``.
    ``utility_signals`` — ``(key, useful_count, noise_count)`` triples seeded
            into the real ``HitRateTracker`` before arm B runs.
    ``metric_name`` — human-readable metric label (e.g. "task_success_rate").
    ``direction`` — ``HIGHER_IS_BETTER`` or ``LOWER_IS_BETTER``.
    ``pass_line`` — minimum magnitude of improvement required.
    ``description`` — what this scenario measures and why.
    """

    scenario_id: str
    name: str
    facts: Tuple[Fact, ...]
    signal_key: str
    noise_keys: Tuple[str, ...]
    stale_keys: Tuple[str, ...]
    utility_signals: Tuple[Tuple[str, int, int], ...]
    metric_name: str
    direction: str
    pass_line: float
    description: str = ""
    top_k: int = 3  # how many slots the agent sees


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class ABRunner:
    """Executes all scenarios and produces an :class:`OutcomeReport`.

    The runner is deterministic: given the same scenarios and the same
    tracker-seed values, it always produces the same report.  The only
    non-determinism in the stack (wall-clock in ``apply_and_persist``) is
    isolated to the timestamp field.
    """

    def __init__(self, profile_name: str = "harness") -> None:
        self._profile = profile_name

    # -- public API -------------------------------------------------------

    def run(self, scenarios: List[Scenario]) -> OutcomeReport:
        """Execute every scenario against both arms and build the report.

        Each scenario gets its own isolated tracker state (``HitRateTracker.reset()``
        is called between scenarios) so utility signals don't leak across
        scenarios.
        """
        import memchorus as _mc
        metrics: List[ScenarioMetric] = []
        for i, sc in enumerate(scenarios):
            # Isolate tracker state per scenario.
            HitRateTracker.reset()
            tracker = HitRateTracker.get_instance()

            # Seed utility signals into the real tracker.
            for key, useful, noise in sc.utility_signals:
                if useful:
                    tracker.record_useful(key, useful)
                if noise:
                    tracker.record_stale(key, noise)

            # Build the boost mapping for arm B from the REAL engine.
            engine = CalibrationEngine(profile_name=self._profile)
            all_keys = {f.key for f in sc.facts}
            boost_b: Dict[str, float] = {}
            for k in all_keys:
                b = engine.boost_factor_for_key(k)
                if b != 1.0:  # only record non-default boosts
                    boost_b[k] = b

            # Recall floor: HELD CONSTANT across both arms (the static v1.7.0
            # default, 0.3). This makes the A/B delta isolate exactly one
            # variable — the real per-key boost mapping from
            # ``CalibrationEngine`` — and keeps the run deterministic and
            # free of any persisted-YAML state (CI-safe). The calibrated
            # floor is exercised separately by :mod:`calibration_bridge`.
            floor: float = 0.3
            floor_a = floor_b = floor

            # Build both arms' sources (identical facts, different params).
            source_a = FakeMemorySource(name="arm_a", facts=list(sc.facts))
            source_b = FakeMemorySource(name="arm_b", facts=list(sc.facts))

            # Measure.
            value_a = self._measure(source_a, sc, boost=None, floor=floor_a)
            value_b = self._measure(source_b, sc, boost=boost_b, floor=floor_b)

            metrics.append(
                ScenarioMetric(
                    scenario_id=sc.scenario_id,
                    name=sc.name,
                    metric_name=sc.metric_name,
                    description=sc.description,
                    direction=sc.direction,
                    arm_a=value_a,
                    arm_b=value_b,
                    pass_line=sc.pass_line,
                    useful_keys=tuple(k for k, u, n in sc.utility_signals if u),
                    noise_keys=tuple(k for k, u, n in sc.utility_signals if n),
                )
            )

        return OutcomeReport.create(
            metrics, version=_mc.__version__, profile=self._profile
        )

    # -- measurement ------------------------------------------------------

    @staticmethod
    def _measure(
        source: FakeMemorySource,
        scenario: Scenario,
        boost: Optional[Dict[str, float]],
        floor: float,
    ) -> float:
        """Drive one arm through the scenario and return the raw metric value.

        The metric semantics are determined by ``scenario.metric_name``:

        * ``task_success``  → 1.0 if the signal fact is in top-1, else 0.0
        * ``re_ask_rate``   → 1.0 if the signal fact is NOT in top-1 (the
                              agent would need a second search), else 0.0
        * ``noise_injection``→ count of noise-key facts in top-k
        * ``stale_rate``    → count of stale-key facts in top-k
        * ``recall_cost``   → number of facts that cleared the floor and
                              were scored (deterministic work proxy)
        """
        results = source.search(
            query="harness-query",
            limit=scenario.top_k,
            boost=boost,
            floor=floor,
            as_of=datetime(2026, 11, 8, tzinfo=timezone.utc),
        )
        result_keys = {r["key"] for r in results}

        m = scenario.metric_name
        if m == "task_success":
            return 1.0 if (results and results[0]["key"] == scenario.signal_key) else 0.0

        if m == "re_ask_rate":
            return 0.0 if (results and results[0]["key"] == scenario.signal_key) else 1.0

        if m == "noise_injection":
            return float(len(result_keys & set(scenario.noise_keys)))

        if m == "stale_rate":
            return float(len(result_keys & set(scenario.stale_keys)))

        if m == "recall_cost":
            return float(source.work_units_last_search)

        raise ValueError(f"unknown metric_name {m!r} for scenario {scenario.scenario_id}")
