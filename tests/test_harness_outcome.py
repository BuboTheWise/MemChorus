"""Outcome-harness tests (issue #271).

These tests prove the *measurement contract* end to end:

* **Suite composition** — exactly the 5 pre-registered scenarios, each with a
  valid direction and a non-trivial pass line.
* **Runner behaviour** — the A/B runner produces a provenanced report, one
  metric per scenario, and the *tuned arm beats the baseline arm* in the
  direction each scenario pre-registered.  This is the RED→GREEN heart: on a
  base commit where the harness package does not exist these imports fail
  (RED); at head they pass (GREEN).
* **Per-scenario semantics** — the specific outcome each scenario claims
  (signal reaches top-1, re-ask avoided, noise/stale demoted, cost reduced).
* **Calibration bridge** — a genuine run is *accepted* and seeds the real
  tracker with the correct fact keys; an empty / unprovenanced /
  all-delta-zero report is *rejected* (the "calibration consumes only proven
  signals" acceptance criterion).
* **Report round-trip** — ``to_dict`` / ``from_dict`` / ``compare_to`` preserve
  the measurement, so a committed baseline is reproducible.

The tests are written against the *public* ``memchorus.harness`` API only.
"""

from __future__ import annotations

import pytest

import memchorus as _mc
from memchorus.calibration_engine import CalibrationEngine
from memchorus.hit_rate_tracker import HitRateTracker

from memchorus.harness import (
    ABRunner,
    ConsumeResult,
    HarnessSignal,
    OutcomeReport,
    Scenario,
    ScenarioMetric,
    consume_report,
    derive_signal,
    all_scenarios,
    scenario_by_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def runner() -> ABRunner:
    return ABRunner(profile_name="harness")


@pytest.fixture
def report(runner: ABRunner) -> OutcomeReport:
    """A completed, accepted harness run."""
    return runner.run(all_scenarios())


# ---------------------------------------------------------------------------
# Suite composition
# ---------------------------------------------------------------------------

def test_suite_has_exactly_five_pre_registered_scenarios():
    scen = all_scenarios()
    assert len(scen) == 5
    ids = {s.scenario_id for s in scen}
    assert len(ids) == 5  # no duplicate ids


def test_each_scenario_has_direction_and_pass_line():
    for s in all_scenarios():
        assert s.direction in ("higher_is_better", "lower_is_better")
        assert s.pass_line >= 0.0
        assert s.metric_name  # non-empty metric label
        assert s.scenario_id  # non-empty id


def test_scenario_by_id_round_trip():
    scen = all_scenarios()
    for s in scen:
        assert scenario_by_id(s.scenario_id).scenario_id == s.scenario_id


def test_scenario_by_id_unknown_raises():
    with pytest.raises(KeyError):
        scenario_by_id("does-not-exist")


# ---------------------------------------------------------------------------
# Runner behaviour (the RED→GREEN core)
# ---------------------------------------------------------------------------

def test_runner_produces_one_metric_per_scenario(report: OutcomeReport):
    assert len(report.scenarios) == 5
    assert {s.scenario_id for s in report.scenarios} == {
        s.scenario_id for s in all_scenarios()
    }


def test_report_is_provenanced(report: OutcomeReport):
    assert report.version == _mc.__version__
    assert report.timestamp  # set by OutcomeReport.create


def test_report_overall_passed(report: OutcomeReport):
    # At head, every scenario must meet its pass line.
    assert report.overall_passed is True
    assert report.failing() == []


def test_tuned_arm_beats_baseline_in_registered_direction(report: OutcomeReport):
    for s in report.scenarios:
        if s.direction == "higher_is_better":
            assert s.arm_b >= s.arm_a - 1e-9, (
                f"{s.scenario_id}: tuned arm {s.arm_b} regressed vs baseline {s.arm_a}"
            )
        else:
            assert s.arm_b <= s.arm_a + 1e-9, (
                f"{s.scenario_id}: tuned arm {s.arm_b} worsened vs baseline {s.arm_a}"
            )


def test_each_scenario_has_a_nonzero_delta(report: OutcomeReport):
    # A genuine harness run must show a measured effect — at least one scenario
    # moves.  In fact, at head *every* scenario is engineered to move.
    deltas = [s.delta for s in report.scenarios]
    assert any(abs(d) > 1e-9 for d in deltas), "no scenario showed any measured delta"
    # Stronger: the suite is built so each scenario demonstrates its outcome.
    assert all(abs(d) > 1e-9 for d in deltas)


def test_metrics_carry_their_fact_keys(report: OutcomeReport):
    # The bridge depends on each metric carrying the fact keys the scenario
    # treated as useful / noise.  At least one useful and one noise key must be
    # present across the report.
    useful = {k for s in report.scenarios for k in s.useful_keys}
    noise = {k for s in report.scenarios for k in s.noise_keys}
    assert len(useful) > 0
    assert len(noise) > 0


# ---------------------------------------------------------------------------
# Per-scenario semantic assertions
# ---------------------------------------------------------------------------

def test_s1_task_success_improves(report: OutcomeReport):
    s = next(m for m in report.scenarios if m.metric_name == "task_success")
    assert s.arm_b == 1.0          # signal reaches top-1 under calibration
    assert s.arm_a == 0.0          # not in top-1 without calibration
    assert s.delta == 1.0


def test_s2_re_ask_rate_improves(report: OutcomeReport):
    s = next(m for m in report.scenarios if m.metric_name == "re_ask_rate")
    assert s.arm_b == 0.0          # no re-ask under calibration
    assert s.arm_a == 1.0          # would re-ask without calibration
    assert s.delta == -1.0


def test_s3_noise_injection_drops(report: OutcomeReport):
    s = next(m for m in report.scenarios if m.metric_name == "noise_injection")
    assert s.arm_b < s.arm_a       # fewer noise facts in top-k under calibration
    assert s.delta < 0.0


def test_s4_stale_rate_drops(report: OutcomeReport):
    s = next(m for m in report.scenarios if m.metric_name == "stale_rate")
    assert s.arm_b < s.arm_a       # fewer stale facts surface under calibration
    assert s.delta < 0.0


def test_s5_recall_cost_drops(report: OutcomeReport):
    s = next(m for m in report.scenarios if m.metric_name == "recall_cost")
    assert s.arm_b < s.arm_a       # fewer facts cleared the floor under calibration
    assert s.delta < 0.0


# ---------------------------------------------------------------------------
# Calibration bridge — "consumes only proven signals"
# ---------------------------------------------------------------------------

def test_bridge_accepts_genuine_report(report: OutcomeReport):
    res = consume_report(report, seed_tracker=False)
    assert isinstance(res, ConsumeResult)
    assert res.accepted is True
    assert res.signal is not None
    assert res.signal.has_signal is True
    assert res.signal.scenario_count == 5
    assert res.signal.non_zero_delta_count == 5


def test_bridge_seeds_real_tracker(report: OutcomeReport):
    res = consume_report(report, seed_tracker=True)
    assert res.accepted is True
    # The seeded keys must be the *fact keys* the scenarios used, not scenario ids.
    assert any(k.startswith("useful:") for k in res.seeded_keys)
    assert any(k.startswith("noise:") for k in res.seeded_keys)
    # Every seeded useful key is one the report's scenarios listed as useful.
    reported_useful = {k for s in report.scenarios for k in s.useful_keys}
    for seed in res.seeded_keys:
        if seed.startswith("useful:"):
            assert seed[len("useful:"):] in reported_useful


def test_bridge_rejects_empty_report():
    res = consume_report(OutcomeReport(scenarios=[], version="1.0.0"), seed_tracker=False)
    assert res.accepted is False
    assert "empty" in res.reason.lower()


def test_bridge_rejects_unprovenanced_report():
    metric = ScenarioMetric(
        scenario_id="x", name="x", metric_name="task_success",
        arm_a=0.0, arm_b=1.0, pass_line=0.0,
    )
    res = consume_report(
        OutcomeReport(scenarios=[metric], version=""),  # no provenance
        seed_tracker=False,
    )
    assert res.accepted is False
    assert "provenance" in res.reason.lower()


def test_bridge_rejects_all_zero_delta_report():
    metric = ScenarioMetric(
        scenario_id="x", name="x", metric_name="task_success",
        arm_a=1.0, arm_b=1.0, pass_line=0.0,  # no measured effect
    )
    res = consume_report(
        OutcomeReport(scenarios=[metric], version="2.0.73"),
        seed_tracker=False,
    )
    assert res.accepted is False
    assert "non-empty" in res.reason.lower() or "delta" in res.reason.lower()


def test_derive_signal_is_a_pure_function_of_the_outcome(report: OutcomeReport):
    sig = derive_signal(report)
    assert isinstance(sig, HarnessSignal)
    assert sig.version == report.version
    # Useful keys are drawn strictly from the report's scenario useful_keys.
    reported_useful = {k for s in report.scenarios for k in s.useful_keys}
    for k in sig.useful_keys:
        assert k in reported_useful
    reported_noise = {k for s in report.scenarios for k in s.noise_keys}
    for k in sig.noise_keys:
        assert k in reported_noise


# ---------------------------------------------------------------------------
# Report round-trip & baseline comparison
# ---------------------------------------------------------------------------

def test_report_dict_round_trip(report: OutcomeReport):
    data = report.to_dict()
    assert data["report_kind"] == "memchorus_outcome_harness"
    assert data["version"] == report.version
    assert data["scenario_count"] == len(report.scenarios)
    restored = OutcomeReport.from_dict(data)
    assert len(restored.scenarios) == len(report.scenarios)
    assert restored.version == report.version
    # Per-scenario measurements preserved.
    orig = {s.scenario_id: s for s in report.scenarios}
    for s in restored.scenarios:
        o = orig[s.scenario_id]
        assert s.arm_a == o.arm_a
        assert s.arm_b == o.arm_b
        assert s.delta == o.delta
        assert s.passed == o.passed


def test_compare_to_self_has_no_regression(report: OutcomeReport):
    cmp = report.compare_to(report)
    assert set(cmp.keys()) == {s.scenario_id for s in report.scenarios}
    for sid, entry in cmp.items():
        assert abs(entry["drift"]) < 1e-9
        assert entry["regressed"] is False


def test_json_serialisable(report: OutcomeReport):
    import json
    blob = json.dumps(report.to_dict())
    assert isinstance(blob, str)
    assert len(blob) > 0
    round_trip = json.loads(blob)
    assert round_trip["overall_passed"] == report.overall_passed


# ---------------------------------------------------------------------------
# Engine interop — the bridge's seeded keys are readable by the real engine
# ---------------------------------------------------------------------------

def test_seeded_keys_are_boostable_by_real_engine(report: OutcomeReport):
    """After the bridge seeds the tracker, the REAL engine must return a
    non-default boost for the seeded useful key — proving the bridge feeds
    the calibration stack, not a parallel universe."""
    # Seed (reset isolates from other tests).
    res = consume_report(report, seed_tracker=True)
    assert res.accepted

    engine = CalibrationEngine(profile_name="harness")
    seeded_useful = [k[len("useful:"):] for k in res.seeded_keys if k.startswith("useful:")]
    seeded_noise = [k[len("noise:"):] for k in res.seeded_keys if k.startswith("noise:")]

    # At least one useful key must be boosted above 1.0 by the real engine.
    useful_boosts = [engine.boost_factor_for_key(k) for k in seeded_useful]
    assert any(b > 1.0 + 1e-9 for b in useful_boosts), useful_boosts

    # At least one noise key must be demoted below 1.0 by the real engine.
    noise_boosts = [engine.boost_factor_for_key(k) for k in seeded_noise]
    assert any(b < 1.0 - 1e-9 for b in noise_boosts), noise_boosts
