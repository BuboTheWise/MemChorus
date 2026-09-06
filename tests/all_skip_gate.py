"""Suite-level gate: no test module may ship green with 0 executed tests (#183).

A module whose entire test set was skipped — or errored at setup before any test
body ran — reports as "skipped" in the summary line while contributing zero
coverage. That is an unexplained pass (a coverage defect), not a pass. A well-
controlled failure is more valuable than an unexplained green.

Mechanics (verified empirically against pytest's real report stream):

1. ``pytest_runtest_logreport`` fires in the **main controller process** under
   pytest-xdist (-n N) — workers ship their reports back over the queue and the
   controller replays them. (Confirmed by capturing reports inside a conftest
   hook and printing them after the run.) This means we can tally per-module
   outcomes in the controller across the whole suite and evaluate them all at
   once — no coordination dance needed.

2. Every test yields a ``(when, outcome)`` triple of ``setup`` / ``call`` /
   ``teardown``. The ONLY reliable signal that "the test body actually ran" is
   that the ``call``-phase report has a non-skipped outcome. Under that rule:

       @pytest.mark.skip          → setup=skipped, teardown=passed        (skipped)
       pytest.skip() in body      → setup=passed,  call=skipped          (skipped)
       fixture raises             → setup=failed,  teardown=passed       (setup error)
       assert X fails             → setup=passed,  call=failed           (EXECUTED)
       raise in body              → setup=passed,  call=failed           (EXECUTED)
       normal pass                → setup=passed,  call=passed           (EXECUTED)

   (teardown=passed appears even when setup fails — do NOT count it as executed.)

3. A module is an offender iff: it has one or more reports in this run (meaning
   pytest collected at least one of its tests AND attempted it), AND none of
   its call-phase reports had a non-skipped outcome. A module that produced
   zero reports at all (deselected — e.g. the ``recall_battery`` marker when
   ``--recall-battery`` is absent, or a Windows-only ``--deselect``) is exempt:
   it was not "all skipped", it was "not selected".

4. When offenders exist, ``pytest_terminal_summary`` returns a non-zero exit
   code via ``pytest.ExitCode.USAGE_ERROR`` — the suite fails with a clear
   module-level message rather than a bare "N skipped" summary line.

The evaluator itself is a pure ``(module_reports) -> (ok, offenders)`` function
so the negative / positive cases can be exercised without a nested pytest run;
see ``test_all_skipped_module_gate.py``.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# A "report" is a (when, outcome) tuple.  ``when`` is one of setup/call/teardown
# and ``outcome`` is one of passed/failed/skipped/error (pytest's report values).
Report = Tuple[str, str]


def _module_key(nodeid: str) -> str:
    """Return the .py module portion of a nodeid (strip ``:: Class :: test`` part)."""
    return nodeid.split("::", 1)[0]


def evaluate(module_reports: Dict[str, List[Report]]) -> Tuple[bool, List[Tuple[str, str]]]:
    """Evaluate per-module outcome tallies against the all-skipped rule.

    Parameters
    ----------
    module_reports:
        Mapping from module nodeid (e.g. ``tests/test_some.py``) to the list of
        ``(when, outcome)`` reports pytest emitted for tests in that module
        during this run.

    Returns
    -------
    (ok, offenders)
        ``ok`` is True when no module violated the rule.  ``offenders`` is a
        list of ``(module, evidence_summary)`` tuples — one entry per
        offending module with a short human-readable explanation of what was
        observed for it (e.g. ``"3 test(s) marker-skipped at setup"``).
    """
    offenders: List[Tuple[str, str]] = []
    for module, reports in module_reports.items():
        if not reports:
            # No reports at all — module was either not collected or every
            # one of its tests was deselected (recall_battery without
            # --recall-battery, Windows --deselect, etc.).  Not an offender.
            continue
        # A test "executed" iff the call-phase report it produced had a
        # non-skipped outcome (passed, failed, error).  setup-phase results
        # are NOT evidence that the body ran; teardown is emitted even after
        # a setup failure.
        executed = any(when == "call" and outcome != "skipped" for when, outcome in reports)
        if executed:
            continue
        # Count for the evidence line: how many call=skipped + how many setup
        # outcomes we saw (marker-skip vs fixture-boom).
        n_call_skip = sum(1 for when, outcome in reports if when == "call" and outcome == "skipped")
        n_marker_skip = sum(1 for when, outcome in reports if when == "setup" and outcome == "skipped")
        n_setup_fail = sum(1 for when, outcome in reports if when == "setup" and outcome in ("failed", "error"))
        n_teardown = sum(1 for when, _ in reports if when == "teardown")
        pieces = []
        if n_marker_skip:
            pieces.append(f"{n_marker_skip} test(s) marker-skipped at setup")
        if n_call_skip:
            pieces.append(f"{n_call_skip} test(s) skipped inside the body")
        if n_setup_fail:
            pieces.append(f"{n_setup_fail} test(s) error at setup/fixture")
        if not pieces:
            # Unusual but possible (e.g. collection error on the module itself,
            # where pytest emits a single "collection" report we didn't tag
            # with when/outcome that we recognize).  Surface as a catch-all.
            pieces.append(f"{len(reports)} report(s) without an executed call phase")
        offenders.append((module, "; ".join(pieces) + f" (teardown reports: {n_teardown})"))
    return (not offenders), offenders
