"""Install doctor -- health diagnostic for a MemChorus installation.

Run this after install or upgrade to verify the environment is sound::

    python -m memchorus.install_doctor

Or via the console script (wired through setup.py entry_points)::

    memchorus-doctor

Exit code 0 if all checks pass, 1 if any check fails.
"""

from __future__ import annotations

import importlib.metadata as imp_meta
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    status: str  # PASS / WARN / FAIL
    message: str
    hint: str = ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_python_version() -> CheckResult:
    """Python runtime must be >= 3.11."""
    major, minor = sys.version_info[:2]
    if major < 3 or (major == 3 and minor < 11):
        return CheckResult(
            name="python_version",
            status=FAIL,
            message=f"Python {major}.{minor} is too old (need >= 3.11)",
            hint="Upgrade Python or use a 3.11+ virtualenv.",
        )
    return CheckResult(
        name="python_version",
        status=PASS,
        message=f"Python {major}.{minor} meets minimum requirement",
    )


def check_dependency_integrity() -> CheckResult:
    """Core dependencies pydantic and PyYAML are importable."""
    problems: List[str] = []

    # --- pydantic ---------------------------------------------------
    pydantic_ver = None
    try:
        import pydantic  # noqa: F401

        pydantic_ver = imp_meta.version("pydantic")
        major_v = int(pydantic_ver.split(".")[0])
        if major_v < 2:
            problems.append(
                f"pydantic {pydantic_ver} is below v2 (need >= 2.0)"
            )
    except ImportError:
        problems.append("pydantic not installed")

    # --- PyYAML -----------------------------------------------------
    pyyaml_ver = None
    try:
        _v = imp_meta.version("pyyaml")
        pyyaml_ver = _v
    except ImportError:
        problems.append("PyYAML not installed")

    if problems:
        return CheckResult(
            name="dependency_integrity",
            status=FAIL,
            message="; ".join(problems),
            hint="pip install --upgrade memchorus",
        )

    return CheckResult(
        name="dependency_integrity",
        status=PASS,
        message=f"pydantic={pydantic_ver}, PyYAML={pyyaml_ver}",
    )


def check_memory_source_registration() -> CheckResult:
    """MemorySource base class is importable and has _registry."""
    try:
        from memchorus.memory_source import MemorySource  # noqa: F401

        ok = hasattr(MemorySource, "_registry") or hasattr(
            MemorySource, "subclasses"
        )
    except ImportError as exc:
        return CheckResult(
            name="memory_source_registration",
            status=FAIL,
            message=f"Cannot import MemorySource: {exc}",
            hint="pip install --force-reinstall memchorus",
        )

    if not ok:
        return CheckResult(
            name="memory_source_registration",
            status=WARN,
            message="MemorySource exists but subclass registry missing",
            hint="Some sources may not be auto-discoverable.",
        )

    return CheckResult(
        name="memory_source_registration",
        status=PASS,
        message="Memory source base class and registry present",
    )


def check_plugin_hooks() -> CheckResult:
    """Hook module imports and exposes expected lifecycle methods."""
    try:
        from memchorus.hooks import MemChorusHooks  # noqa: F401

        hooks = [
            a
            for a in dir(MemChorusHooks)
            if not a.startswith("_") and callable(getattr(MemChorusHooks, a, None))
        ]
    except (ImportError, AttributeError) as exc:
        return CheckResult(
            name="plugin_hook_state",
            status=FAIL,
            message=f"Cannot import hooks module: {exc}",
            hint="Install memchorus with entry points intact.",
        )

    if not hooks:
        return CheckResult(
            name="plugin_hook_state",
            status=WARN,
            message="MemChorusHooks has no public methods",
            hint="Check setup.py entry_points.",
        )

    return CheckResult(
        name="plugin_hook_state",
        status=PASS,
        message=f"Plugin hooks OK ({len(hooks)} public members)",
    )


def check_config_validation() -> CheckResult:
    """A valid config.yaml can be loaded without errors."""
    candidates = [
        Path.home() / ".hermes" / "config.yaml",
        Path(".hermes/config.yaml").resolve(),
    ]

    found_path: Optional[Path] = None
    for cfg in candidates:
        if cfg.is_file():
            found_path = cfg
            try:
                with open(cfg) as fh:
                    data = yaml.safe_load(fh)
                if not isinstance(data, dict):
                    return CheckResult(
                        name="config_validation",
                        status=WARN,
                        message=(
                            f"Config at {cfg} valid YAML but type "
                            f"{type(data).__name__}, expected mapping"
                        ),
                        hint="Start config.yaml with key: value pairs.",
                    )
            except yaml.YAMLError as exc:
                return CheckResult(
                    name="config_validation",
                    status=FAIL,
                    message=f"YAML error in {cfg}: {exc}",
                    hint="Run 'memchorus-init --profile <name>' to regenerate.",
                )

    if found_path is None:
        return CheckResult(
            name="config_validation",
            status=WARN,
            message="No config.yaml at standard locations",
            hint="Run 'memchorus-init --profile <name>' to create one.",
        )

    return CheckResult(
        name="config_validation",
        status=PASS,
        message=f"Config valid at {found_path}",
    )


def check_auto_tune_pipeline() -> CheckResult:
    """Auto-tuning classes are importable."""
    components = [
        ("memchorus.hit_rate_tracker", "HitRateTracker"),
        ("memchorus.mistake_detector", "MistakeDetector"),
        ("memchorus.calibration_engine", "CalibrationEngine"),
        ("memchorus.adaptive_threshold", "AdaptiveThreshold"),
    ]
    missing: List[str] = []

    for mod_name, cls_name in components:
        spec = importlib.util.find_spec(mod_name)
        if spec is None:
            missing.append(cls_name)
            continue
        mod = importlib.import_module(mod_name)
        if not hasattr(mod, cls_name):
            missing.append(f"{cls_name} (module exists but class missing)")

    if missing:
        return CheckResult(
            name="auto_tune_pipeline",
            status=FAIL,
            message=f"Missing: {', '.join(missing)}",
            hint="pip install --upgrade memchorus",
        )

    return CheckResult(
        name="auto_tune_pipeline",
        status=PASS,
        message="All 4 auto-tuning components present and importable",
    )


def check_data_directory() -> CheckResult:
    """Default data directory is readable and writable."""
    data_dir = Path.home() / ".mempalace"

    if not data_dir.exists():
        return CheckResult(
            name="data_directory",
            status=FAIL,
            message=f"{data_dir} does not exist",
            hint=f"mkdir -p {data_dir}",
        )
    if not data_dir.is_dir():
        return CheckResult(
            name="data_directory",
            status=FAIL,
            message=f"{data_dir} is a file, not directory",
            hint=f"Fix permissions at {data_dir}",
        )
    if not os.access(str(data_dir), os.R_OK):
        return CheckResult(
            name="data_directory",
            status=FAIL,
            message=f"{data_dir} unreadable",
            hint=f"chmod u+rw {data_dir}",
        )
    if not os.access(str(data_dir), os.W_OK):
        return CheckResult(
            name="data_directory",
            status=FAIL,
            message=f"{data_dir} unwritable",
            hint=f"chmod u+rw {data_dir}",
        )

    return CheckResult(
        name="data_directory",
        status=PASS,
        message=f"{data_dir} readable and writable",
    )


def check_test_suite() -> CheckResult:
    """pytest can discover the test suite."""
    try:
        from memchorus import __version__

        spec = importlib.util.find_spec("tests")
        if spec is None:
            return CheckResult(
                name="test_suite",
                status=WARN,
                message="tests/ not in sys.path (expected for pip install)",
                hint="Run 'pytest tests/' from repo root.",
            )
        test_mod = importlib.import_module("tests")
        del test_mod
        return CheckResult(
            name="test_suite",
            status=PASS,
            message=f"Test suite discoverable (v{__version__})",
        )
    except ImportError:
        return CheckResult(
            name="test_suite",
            status=WARN,
            message="Cannot import tests or memchorus directly",
            hint="OK for pip-installed package; run pytest from repo root.",
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_checks() -> List[CheckResult]:
    """Execute every check and return results."""
    checks = [
        check_python_version,
        check_dependency_integrity,
        check_memory_source_registration,
        check_plugin_hooks,
        check_config_validation,
        check_auto_tune_pipeline,
        check_data_directory,
        check_test_suite,
    ]
    return [fn() for fn in checks]


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

_BADGES = {
    PASS: "\u2705",
    WARN: "\u26a0",
    FAIL: "\U0001f534",
}


def print_report(results: List[CheckResult]) -> None:
    """Print human-readable diagnostic report to stdout."""
    width = max(len(r.name) for r in results) + 8 if results else 42

    print()
    print(f"MemChorus Install Doctor".center(width, "="))
    print()

    counts = {PASS: 0, WARN: 0, FAIL: 0}

    for r in results:
        badge = _BADGES.get(r.status, "?")
        label = f"{r.name}:"
        line = f"  {label:<{width}} {badge} {r.message}"
        print(line)
        if r.hint:
            print(f"  {'':>{width}}     {r.hint}")
        counts[r.status] += 1

    # Summary line ---------------------------------------------------
    parts = [f"{counts[PASS]} passed"]
    if counts[WARN]:
        parts.append(f"{counts[WARN]} warnings")
    if counts[FAIL]:
        parts.append(f"{counts[FAIL]} failed")
    print()
    print("Summary: " + ", ".join(parts))
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``memchorus-doctor``.

    Returns exit code 0 when healthy, 1 on any failure.
    """
    results = run_checks()
    print_report(results)

    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
