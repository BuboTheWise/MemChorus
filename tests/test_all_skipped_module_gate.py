"""Unit tests for the no-all-skipped-module gate evaluator (all_skip_gate.evaluate).

These tests exercise the pure function directly — no nested pytest run needed —
so they can run in any environment (CI, local, even xdist workers).

Positive cases: modules with at least one executed test are clean.
Negative cases: modules whose entire report stream is skipped/errored are offenders.
Edge case: modules with zero reports (deselected) are exempt.
"""
import os
import sys

# Ensure tests/ is on sys.path so we can import the sibling evaluator.
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from all_skip_gate import evaluate  # noqa: E402


# ── Positive cases (module has at least one executed test) ─────────────────

def test_pass_normal_passed():
    """A module where every test passed is clean."""
    reports = {
        "tests/test_foo.py": [
            ("setup", "passed"), ("call", "passed"), ("teardown", "passed"),
        ]
    }
    ok, offenders = evaluate(reports)
    assert ok is True
    assert offenders == []


def test_pass_one_failed_one_passed():
    """A module with a mix of passed and failed tests is clean (both are executed)."""
    reports = {
        "tests/test_bar.py": [
            ("setup", "passed"), ("call", "passed"), ("teardown", "passed"),
            ("setup", "passed"), ("call", "failed"), ("teardown", "passed"),
        ]
    }
    ok, offenders = evaluate(reports)
    assert ok is True
    assert offenders == []


def test_pass_setup_error_but_later_test_ran():
    """A module where one test errored at setup but another ran is clean."""
    reports = {
        "tests/test_baz.py": [
            ("setup", "error"), ("teardown", "passed"),
            ("setup", "passed"), ("call", "passed"), ("teardown", "passed"),
        ]
    }
    ok, offenders = evaluate(reports)
    assert ok is True


# ── Negative cases (module is 100% skipped or errored) ─────────────────────

def test_fail_all_marker_skipped():
    """A module where every test is marker-skipped at setup is an offender."""
    reports = {
        "tests/test_dead.py": [
            ("setup", "skipped"), ("teardown", "passed"),
            ("setup", "skipped"), ("teardown", "passed"),
            ("setup", "skipped"), ("teardown", "passed"),
        ]
    }
    ok, offenders = evaluate(reports)
    assert ok is False
    assert len(offenders) == 1
    module, evidence = offenders[0]
    assert "test_dead.py" in module
    assert "marker-skipped" in evidence


def test_fail_all_body_skipped():
    """A module where every test calls pytest.skip() in the body is an offender."""
    reports = {
        "tests/test_conditional.py": [
            ("setup", "passed"), ("call", "skipped"), ("teardown", "passed"),
            ("setup", "passed"), ("call", "skipped"), ("teardown", "passed"),
        ]
    }
    ok, offenders = evaluate(reports)
    assert ok is False
    assert len(offenders) == 1
    assert "skipped inside the body" in offenders[0][1]


def test_fail_mixed_marker_and_body_skip():
    """A module with a mix of marker-skip and body-skip is still an offender."""
    reports = {
        "tests/test_mixed.py": [
            ("setup", "skipped"), ("teardown", "passed"),
            ("setup", "passed"), ("call", "skipped"), ("teardown", "passed"),
        ]
    }
    ok, offenders = evaluate(reports)
    assert ok is False
    assert len(offenders) == 1


def test_fail_all_setup_error():
    """A module where every test errors at setup (fixture fails) is an offender."""
    reports = {
        "tests/test_broken.py": [
            ("setup", "error"), ("teardown", "passed"),
            ("setup", "error"), ("teardown", "passed"),
        ]
    }
    ok, offenders = evaluate(reports)
    assert ok is False
    assert len(offenders) == 1
    assert "setup/fixture" in offenders[0][1]


def test_fail_multiple_offending_modules():
    """Multiple all-skipped modules are each reported."""
    reports = {
        "tests/test_a.py": [("setup", "skipped"), ("teardown", "passed")],
        "tests/test_b.py": [("setup", "skipped"), ("teardown", "passed")],
        "tests/test_c.py": [("setup", "passed"), ("call", "passed"), ("teardown", "passed")],
    }
    ok, offenders = evaluate(reports)
    assert ok is False
    assert len(offenders) == 2


# ── Edge cases ─────────────────────────────────────────────────────────────

def test_exempt_deselected_module():
    """A module that was deselected (zero reports) is exempt — not an offender."""
    reports = {
        "tests/test_deselected.py": [],   # zero reports
        "tests/test_alive.py": [("setup", "passed"), ("call", "passed"), ("teardown", "passed")],
    }
    ok, offenders = evaluate(reports)
    assert ok is True
    assert offenders == []


def test_empty_input():
    """An empty reports dict is trivially clean."""
    ok, offenders = evaluate({})
    assert ok is True
    assert offenders == []


def test_teardown_pass_does_not_count_as_executed():
    """teardown=passed alone (no call-phase) does NOT count as executed."""
    # Simulated: setup failed, teardown passed — no call-phase report at all
    reports = {
        "tests/test_edge.py": [
            ("setup", "failed"),
            ("teardown", "passed"),
        ]
    }
    ok, offenders = evaluate(reports)
    assert ok is False
    assert len(offenders) == 1
