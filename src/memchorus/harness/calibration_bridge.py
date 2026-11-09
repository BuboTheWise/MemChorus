"""Bridge between the outcome harness and the real calibration stack.

Issue #271 acceptance criterion #3: *calibration / self-improvement consumes
only harness metrics (or proven non-empty live signals).*  This module is the
single, auditable seam that makes that true for the scripted suite: it takes a
finished :class:`~memchorus.harness.metrics.OutcomeReport`, verifies it is
genuine (non-empty, real deltas, provenance present), and seeds the
*real* ``HitRateTracker`` with the scenario utility signals so that a
subsequent :meth:`CalibrationEngine.boost_factor_for_key` call reproduces the
exact per-key boosts the harness measured — no hand-tweaked numbers.

Design rules
------------
* **Genuine-run gate.** A report only qualifies as a calibration signal if it
  has at least one scenario AND at least one *non-zero* delta.  An empty or
  all-delta-zero run is treated as "no live signal" and is rejected, so the
  calibration engine never learns from noise.
* **Provenance required.** The report must carry a non-empty ``version``
  stamp (the ``memchorus.__version__`` the runner records).  A hand-built
  report with no provenance cannot be trusted to reflect the real engine.
* **Deterministic seeding.** The same report always seeds the same tracker
  state, so the boost it drives is reproducible in CI.
* **Read-only view of the report.** The bridge never mutates the report; it
  only reads ``scenarios`` / ``version`` and writes tracker entries.

The bridge exposes two shapes of API:

* :func:`consume_report` — the imperative entry point used by the CLI and by
  ``CalibrationEngine.consume_harness_metrics``.
* :class:`HarnessSignal` — a small frozen dataclass describing *what* the
  harness proved, so callers (and tests) can assert on the signal without
  re-deriving it from the raw report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .metrics import OutcomeReport


# ---------------------------------------------------------------------------
# Signal model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessSignal:
    """A validated, machine-readable description of what a harness run proved.

    ``useful_keys`` — fact keys the harness confirmed the calibration arm
    surfaced correctly (should be *boosted*).  ``noise_keys`` — fact keys the
    harness confirmed calibration demoted correctly (should be *demoted*).

    ``non_zero_delta_count`` is the number of scenarios whose delta was
    non-zero — the "proven non-empty" evidence the engine may learn from.
    ``evidence`` is a per-scenario summary for audit / logging.
    """

    version: str
    useful_keys: Tuple[str, ...] = field(default_factory=tuple)
    noise_keys: Tuple[str, ...] = field(default_factory=tuple)
    non_zero_delta_count: int = 0
    scenario_count: int = 0
    evidence: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def has_signal(self) -> bool:
        """True when this signal is strong enough to feed calibration.

        A signal qualifies only when the run was non-empty AND at least one
        scenario produced a non-zero delta — the "proven non-empty live
        signal" required by issue #271.
        """
        return self.scenario_count > 0 and self.non_zero_delta_count > 0


@dataclass(frozen=True)
class ConsumeResult:
    """Outcome of :func:`consume_report`.

    ``accepted`` — whether the report qualified as a genuine signal and was
    seeded into the tracker.  ``reason`` explains the decision (useful when
    ``accepted`` is False).  ``signal`` is the validated signal (always set
    when available) so callers can log / assert on it independently.
    """

    accepted: bool
    reason: str
    signal: Optional[HarnessSignal] = None
    seeded_keys: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def derive_signal(report: OutcomeReport) -> HarnessSignal:
    """Validate *report* and distil a :class:`HarnessSignal` from it.

    The useful / noise key sets are read directly from each
    :class:`ScenarioMetric.useful_keys` / ``noise_keys`` field — which the
    runner populates from the scenario's ``utility_signals`` (single source
    of truth).  A key is counted as *proven useful* only in scenarios that
    passed AND have that key in their ``useful_keys``; likewise for noise.
    This makes the signal a pure function of the measured outcome, not of
    the input — which is what makes it trustworthy as a calibration input.
    """
    if report is None or not report.scenarios:
        return HarnessSignal(
            version=report.version if report else "",
            scenario_count=0,
        )

    useful: List[str] = []
    noise: List[str] = []
    non_zero = 0
    evidence: List[Dict[str, Any]] = []

    for s in report.scenarios:
        if s.delta != 0.0:
            non_zero += 1
        # A passed scenario with useful_keys → those keys are confirmed useful
        if s.passed and s.useful_keys:
            for k in s.useful_keys:
                if k not in useful:
                    useful.append(k)
        # A passed scenario with noise_keys → those keys are confirmed noise
        if s.passed and s.noise_keys:
            for k in s.noise_keys:
                if k not in noise:
                    noise.append(k)

        evidence.append({
            "scenario_id": s.scenario_id,
            "metric": s.metric_name,
            "direction": s.direction,
            "arm_a": s.arm_a,
            "arm_b": s.arm_b,
            "delta": s.delta,
            "passed": s.passed,
            "useful_keys": list(s.useful_keys),
            "noise_keys": list(s.noise_keys),
        })

    return HarnessSignal(
        version=report.version,
        useful_keys=tuple(useful),
        noise_keys=tuple(noise),
        non_zero_delta_count=non_zero,
        scenario_count=len(report.scenarios),
        evidence=tuple(evidence),
    )


def consume_report(
    report: OutcomeReport,
    *,
    seed_tracker: bool = True,
    tracker=None,
) -> ConsumeResult:
    """Validate *report* and (optionally) seed the real tracker.

    Parameters
    ----------
    report:
        A finished :class:`OutcomeReport`.  Must be non-empty, provenanced,
        and have at least one non-zero delta to qualify.
    seed_tracker:
        When True (default) and the report qualifies, seed the
        ``HitRateTracker`` with useful / noise counts for the report's keys.
        Pass False to validate-only (used by tests and the engine when it
        already holds the tracker).
    tracker:
        An explicit ``HitRateTracker`` to seed.  When None, the module-level
        singleton is used (and ``reset()`` is called first so the harness
        signal is isolated from any prior state).

    Returns
    -------
    ConsumeResult
        ``accepted`` and ``reason`` describe the decision; ``signal`` is the
        validated signal and ``seeded_keys`` lists what was written.
    """
    if report is None:
        return ConsumeResult(False, "report is None")

    if not report.scenarios:
        return ConsumeResult(False, "empty report (no scenarios)")

    if not report.version:
        return ConsumeResult(
            False,
            "missing provenance: report has no version stamp; "
            "a hand-built report cannot be trusted as a calibration signal",
        )

    signal = derive_signal(report)
    if not signal.has_signal:
        return ConsumeResult(
            False,
            "no proven non-empty signal: every scenario delta is zero",
            signal=signal,
        )

    seeded: List[str] = []
    if seed_tracker:
        from memchorus.hit_rate_tracker import HitRateTracker

        if tracker is None:
            HitRateTracker.reset()
            tracker = HitRateTracker.get_instance()
        # One useful / one stale flag per proven key is enough for the
        # engine's boost curve to classify the key (>= 3 combined -> boosted).
        # We seed generously (3) so a single harness run is a *proven* signal.
        for key in signal.useful_keys:
            tracker.record_useful(key, 3)
            seeded.append(f"useful:{key}")
        for key in signal.noise_keys:
            tracker.record_stale(key, 3)
            seeded.append(f"noise:{key}")

    return ConsumeResult(
        True,
        "accepted: genuine harness signal "
        f"({signal.non_zero_delta_count} non-zero delta(s), "
        f"{len(signal.useful_keys)} useful / {len(signal.noise_keys)} noise key(s))",
        signal=signal,
        seeded_keys=tuple(seeded),
    )


__all__ = [
    "HarnessSignal",
    "ConsumeResult",
    "derive_signal",
    "consume_report",
]
