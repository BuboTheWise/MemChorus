#!/usr/bin/env python3
"""Outcome-harness runner (issue #271).

Runs the pre-registered A/B task suite, prints the measurement report as
JSON to stdout, and — on request — writes a committed baseline and/or feeds
the real calibration bridge.

Exit codes
----------
* ``0`` — the harness ran and **overall passed** (every scenario met its
  pre-registered pass line in the right direction).
* ``1`` — the harness ran but **failed** (one or more scenarios regressed or
  did not clear their pass line).
* ``2`` — the harness could not run at all (import error, bad args,
  unhandled exception).

This is the gate CI invokes.  A committed baseline under ``benchmarks/`` is
the reference point the gate compares against via ``OutcomeReport.compare_to``.

Example
-------
    # Run + emit JSON to stdout (CI gate)
    python scripts/run_outcome_harness.py

    # Run + write a fresh baseline
    python scripts/run_outcome_harness.py --write-baseline benchmarks/outcome_baseline.json

    # Run + compare against an existing baseline (drift report appended)
    python scripts/run_outcome_harness.py --baseline-file benchmarks/outcome_baseline.json

    # Run + feed the real calibration bridge (seeds the tracker)
    python scripts/run_outcome_harness.py --consume
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_BASELINE = "benchmarks/outcome_baseline.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MemChorus outcome-based A/B harness (issue #271).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--profile", type=str, default="harness",
        help="Profile name recorded in the report provenance.",
    )
    p.add_argument(
        "--write-baseline", type=Path, default=None, metavar="PATH",
        help="Write the report to PATH as a committed baseline JSON.",
    )
    p.add_argument(
        "--baseline-file", type=Path, default=None, metavar="PATH",
        help="Compare this run against an existing baseline at PATH (drift report).",
    )
    p.add_argument(
        "--consume", action="store_true",
        help="Feed the report to the calibration bridge (seeds the real tracker). "
             "Off by default so CI runs stay side-effect-free.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()

    # Import inside main() so a bad import still yields a clean exit code 2
    # with a readable message rather than a raw traceback.
    try:
        import memchorus
        from memchorus.harness import (
            ABRunner,
            OutcomeReport,
            consume_report,
            all_scenarios,
        )
    except Exception as e:  # noqa: BLE001 — top-level guard for exit code 2
        print(json.dumps({"error": f"import failure: {e}"}), file=sys.stderr)
        return 2

    try:
        runner = ABRunner(profile_name=args.profile)
        report = runner.run(all_scenarios())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"harness run failure: {e}"}), file=sys.stderr)
        return 2

    out = report.to_dict()
    out["version"] = out.get("version") or memchorus.__version__

    # Optional: drift comparison against a committed baseline.
    if args.baseline_file is not None:
        try:
            base_data = json.loads(args.baseline_file.read_text())
            baseline = OutcomeReport.from_dict(base_data)
            out["baseline_comparison"] = report.compare_to(baseline)
            out["baseline_version"] = baseline.version
        except Exception as e:  # noqa: BLE001
            out["baseline_comparison"] = {"error": str(e)}

    # Optional: feed the real calibration bridge.
    if args.consume:
        try:
            res = consume_report(report, seed_tracker=True)
            out["calibration"] = {
                "accepted": res.accepted,
                "reason": res.reason,
                "seeded_keys": list(res.seeded_keys),
                "useful_keys": list(res.signal.useful_keys) if res.signal else [],
                "noise_keys": list(res.signal.noise_keys) if res.signal else [],
            }
        except Exception as e:  # noqa: BLE001
            out["calibration"] = {"error": str(e)}

    # Optional: write a committed baseline.
    if args.write_baseline is not None:
        try:
            args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
            # Write the canonical report dict (no CLI-only extras).
            baseline_payload = report.to_dict()
            baseline_payload["version"] = memchorus.__version__
            args.write_baseline.write_text(json.dumps(baseline_payload, indent=2) + "\n")
            out["baseline_written"] = str(args.write_baseline)
        except Exception as e:  # noqa: BLE001
            out["baseline_written_error"] = str(e)

    print(json.dumps(out, indent=2, default=str))
    return 0 if report.overall_passed else 1


if __name__ == "__main__":
    sys.exit(main())
