"""Outcome-based A/B evaluation harness for MemChorus (issue #271).

This package provides the scripted task suite, the A/B runner, the outcome
metric data model, and the calibration bridge.  It is the *measurement
instrument* for issue #271's acceptance criteria:

* The suite is runnable in CI or nightly (``scripts/run_outcome_harness.py``).
* A baseline is published under ``benchmarks/`` (``OutcomeReport.to_json``).
* Calibration consumes only harness metrics via :mod:`calibration_bridge`.

Public API
----------
* :class:`~memchorus.harness.metrics.OutcomeReport` — the measurement contract.
* :class:`~memchorus.harness.ab_runner.ABRunner` — runs all scenarios.
* :class:`~memchorus.harness.ab_runner.Scenario` — a scripted scenario.
* :func:`~memchorus.harness.task_suite.all_scenarios` — the 5 pre-registered scenarios.
* :func:`~memchorus.harness.calibration_bridge.consume_report` — seed the real
  tracker from a finished report.
"""

from __future__ import annotations

from .ab_runner import ABRunner, Scenario
from .calibration_bridge import (
    ConsumeResult,
    HarnessSignal,
    consume_report,
    derive_signal,
)
from .fakes import Fact, FakeMemorySource
from .metrics import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    OutcomeReport,
    ScenarioMetric,
)
from .task_suite import all_scenarios, scenario_by_id

__version__ = "1.0.0"

__all__ = [
    "ABRunner",
    "Scenario",
    "ConsumeResult",
    "HarnessSignal",
    "consume_report",
    "derive_signal",
    "Fact",
    "FakeMemorySource",
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "OutcomeReport",
    "ScenarioMetric",
    "all_scenarios",
    "scenario_by_id",
    "__version__",
]
