"""Install doctor -- health diagnostic for a MemChorus installation.

Run this after install or upgrade to verify the environment is sound::

    python -m memchorus.install_doctor

Focused dependency-coherence check (OpenTelemetry family / core integrity; the
check that guards against a split OTel stack, see #169)::

    python -m memchorus.install_doctor --deps-check
    python -m memchorus.install_doctor --deps-check --json

Or via the console script (wired through setup.py entry_points)::

    memchorus-doctor
    memchorus-doctor --deps-check

Provenance audit — scans the MemPalace local cache and reports how many stored
entries carry a non-empty ``source_file`` provenance field (IMPL #166)::

    memchorus-doctor --provenance-report
    memchorus-doctor --provenance-report --json

Exit code: 0 = all entries have provenance, 1 = some missing (pre-#166 data),
2 = no accessible cache / unexpected error.

Exit code 0 if all relevant checks pass, 1 if any check fails.
"""

from __future__ import annotations

import importlib.metadata as imp_meta
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from memchorus.hermes_home import hermes_home
from memchorus import palace_path as palace_path_mod
from memchorus.palace_path import classify as palace_path_classify


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
        hermes_home() / "config.yaml",
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
# OpenTelemetry dependency coherence  (--deps-check)
#
# Background (#169): the mempalace/mcp integration imports the OTel runtime at
# module load. A reinstall-from-GitHub of memchorus into a shared venv lets pip
# re-resolve the OTel family independently and split it (observed: opentelemetry-
# api 1.39.1 vs sdk 1.44.0), which bricked `import memchorus` / `import
# mempalace`. This check inspects the *installed* metadata of the OTel family
# and fails if the family is internally inconsistent, regardless of whether
# memchorus itself declared the pins.
#
# The coherence rule mirrors what `pip check` reports for the OTel family but
# is scoped to OTel so the rest of a busy shared venv does not drown the signal:
#   * every opentelemetry-* distribution's requirements ON opentelemetry-*
#     must be satisfiable by the versions actually present, AND
#   * the "line" must be a single coherent one: api / sdk / proto-common /
#     semantic-conventions must not be on three different minor lines at once
#     (the classic split).
# ---------------------------------------------------------------------------

# Packages that must all sit on the same release line for a coherent stack.
_OTEL_LINE_PACKAGES = (
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-semantic-conventions",
    "opentelemetry-exporter-otlp-proto-common",
)

# Requirement fragments we treat as "a constraint on another OTel package".
_OTEL_PREFIXES = ("opentelemetry",)


# ---------------------------------------------------------------------------
# Recall Location Standard  (IMPL #163.4, spec §4.7 / §3.3)
#
# Two surfaces:
#
#   1. A default install-health check (``check_project_record_resolution``)
#      that confirms the resolver pipeline is importable, the orchestrator
#      exposes the API, and the base-class no-op degrade returns the
#      documented (None, None) contract (spec §4.5) — the graceful path a
#      fresh orchestrator (or a source that doesn't back the project
#      namespace) relies on.
#
#   2. A focused ``--project <name>`` diagnostic that resolves a single
#      project's keyed record and renders the §3.3 contract
#      (``location`` / ``standard`` / ``reconciled``) in distinct labeled
#      blocks — the same shape the live agent injects, so what an operator
#      reads in a doctor is the same shape the agent would surface.
# ---------------------------------------------------------------------------

def _project_record_report(project_name: str) -> Dict[str, Any]:
    """Resolve the keyed record for *project_name* (spec §4.7).

    Returns a report dict with ``status`` (
    ``ok`` | ``no_orchestrator`` | ``no_data`` | ``error``
    ), the raw ``record`` when one resolved, and a human-readable
    ``reason`` otherwise. Never raises for expected failure modes —
    mirrors the ``_recall_query`` diagnostic style.
    """
    from memchorus import get_orchestrator

    orchestrator = get_orchestrator()
    if orchestrator is None:
        return {
            "project": project_name,
            "status": "no_orchestrator",
            "reason": (
                "No MemoryOrchestrator is registered in this process. "
                "A keyed project-record resolution requires a source that "
                "backs the project:<name> namespace; a fresh interpreter "
                "has not auto-bootstrapped one yet."
            ),
            "record": None,
        }

    resolver = getattr(orchestrator, "resolve_project_record", None)
    if not callable(resolver):
        return {
            "project": project_name,
            "status": "no_orchestrator",
            "reason": (
                "This orchestrator build lacks resolve_project_record — "
                "upgrade memchorus to the #163.2 release or newer."
            ),
            "record": None,
        }

    try:
        record = resolver(project_name)
    except Exception as exc:  # noqa: BLE001 — doctor must report, not crash
        return {
            "project": project_name,
            "status": "error",
            "reason": (
                f"resolve_project_record({project_name!r}) raised "
                f"{type(exc).__name__}: {exc}"
            ),
            "record": None,
        }

    if record is None:
        return {
            "project": project_name,
            "status": "no_data",
            "reason": (
                "resolve_project_record returned None — the base-class "
                "no-op degrade (spec §4.5). This typically means no source "
                "in this process backs the project:<name> namespace."
            ),
            "record": None,
        }

    return {
        "project": project_name,
        "status": "ok",
        "reason": None,
        "record": record,
    }


def _render_project_report_human(report: Dict[str, Any]) -> None:
    """Human render for ``--project <name>`` (spec §4.7 / §3.3)."""
    name = report.get("project", "?")
    status = report.get("status", "?")

    print(f"project record: {name}  [{status}]")
    if report.get("reason"):
        print()
        print(report["reason"])

    record = report.get("record")
    if not isinstance(record, dict):
        return

    loc = record.get("location") or {}
    std = record.get("standard") or {}
    rec = record.get("reconciled") or []

    if loc:
        print()
        print(
            "location: {root} (source: {src}, verified_at: {v})".format(
                root=loc.get("canonical_root", "?"),
                src=loc.get("source", "?"),
                v=loc.get("verified_at") or "<derived: unverified>",
            )
        )
    if std:
        topics = std.get("topics") or []
        topics_str = ", ".join(topics) if topics else "<none>"
        print(
            "standard: skill={skill} doc={doc} gist={gist} topics=[{tr}]".format(
                skill=std.get("skill", "?"),
                doc=std.get("doc_path", "?"),
                gist=std.get("gist", ""),
                tr=topics_str,
            )
        )
    if rec:
        print(f"reconciled: {len(rec)} scratch path(s):")
        for e in rec:
            print(
                "  role={role} path={path} ({relation})".format(
                    role=e.get("role", "scratch"),
                    path=e.get("path", "?"),
                    relation=e.get("relation", ""),
                )
            )
    else:
        print("reconciled: <none>")


def _render_project_report_json(report: Dict[str, Any]) -> None:
    """JSON render — stable schema, ``sort_keys`` + ``default=str``."""
    out: Dict[str, Any] = {
        "project": report.get("project", ""),
        "status": report.get("status", ""),
    }
    if report.get("reason"):
        out["reason"] = report["reason"]
    if isinstance(report.get("record"), dict):
        out["record"] = report["record"]
    print(json.dumps(out, indent=2, sort_keys=True, default=str))


def _project_exit_code(report: Dict[str, Any]) -> int:
    """``ok`` -> 0.  ``no_data`` / ``no_orchestrator`` / ``error`` -> 1.

    A diagnostic that ran but found no data is not a crash; operators
    still want a non-zero exit to script-gate on it (e.g. CI check that
    a given project name resolves in *this* environment).
    """
    return 0 if report.get("status") == "ok" else 1


def check_project_record_resolution() -> CheckResult:
    """The keyed project-record pipeline is importable and degrades cleanly.

    IMPL #163.4 (spec §4.7): the install-health surface confirms

      * ``MemoryOrchestrator.resolve_project_record`` exists (spec §3.1);
      * ``MemorySource.resolve_project_record`` base hook exists (the
        no-op degrade path, spec §4.5);
      * ``memchorus.project_record`` module is importable (schema —
        #163.1); and
      * the base-class no-op returns the documented
        ``(None, None)`` contract rather than raising — the graceful
        path a fresh orchestrator with no project backing relies on.
    """
    try:
        from memchorus.orchestrator import MemoryOrchestrator as _Orch
        from memchorus.memory_source import MemorySource as _Src
        import memchorus.project_record  # noqa: F401
    except ImportError as exc:
        return CheckResult(
            name="project_record_resolution",
            status=FAIL,
            message=f"Cannot import project-record pipeline: {exc}",
            hint="pip install --upgrade memchorus",
        )

    orch_ok = hasattr(_Orch, "resolve_project_record")
    src_ok = hasattr(_Src, "resolve_project_record")

    if not orch_ok:
        return CheckResult(
            name="project_record_resolution",
            status=FAIL,
            message="MemoryOrchestrator lacks resolve_project_record (spec §3.1)",
            hint="pip install --upgrade memchorus (need the #163.2 release)",
        )

    # Verify the base-class no-op degrades to an explicit opt-out signal,
    # not an unraised exception (spec §4.5).  MemorySource is an ABC, so use
    # the same minimal concrete stub the #163.1 test suite uses.
    no_op_ok = True
    no_op_err = ""
    if src_ok:
        class _StubSource(_Src):
            SUPPORTED_METHODS = ['save', 'retrieve', 'search', 'get_source_info']

            def __init__(self, name="doctor_stub", config=None):
                self.name = name
                self.config = config or {}

            @property
            def is_available(self):
                return True

            def save(self, key, value):
                return True

            def retrieve(self, key):
                return None

            def search(self, query, limit=10):
                return []

            def get_source_info(self):
                return {"name": self.name}

            def proactive_check(self, context=None):
                return {}

            def proactive_save(self, key, value, context=None):
                return True

            def delete(self, key):
                return False

        inst = _StubSource()
        try:
            res = inst.resolve_project_record("project:sample")
        except Exception as exc:  # noqa: BLE001
            no_op_ok = False
            no_op_err = f"{type(exc).__name__}: {exc}"
        else:
            # §4.5 contract: either None or the (None, None) tuple — both
            # channels opted out, no crash.
            if not (res is None or (isinstance(res, tuple) and res == (None, None))):
                no_op_ok = False
                no_op_err = f"expected None / (None, None), got {type(res).__name__}"
    else:
        no_op_ok = False
        no_op_err = "base-class hook missing"

    if not no_op_ok:
        return CheckResult(
            name="project_record_resolution",
            status=WARN,
            message=f"Base-class no-op contract not met: {no_op_err}",
            hint="Verify MemorySource.resolve_project_record returns (None, None).",
        )

    return CheckResult(
        name="project_record_resolution",
        status=PASS,
        message=(
            "resolve_project_record present on MemoryOrchestrator; "
            "base-class no-op degrades to (None, None)"
        ),
    )


def _otel_packages_present() -> Dict[str, str]:
    """Return {distribution_name: version} for installed opentelemetry-* dists.

    Uses importlib.metadata (PEP 503-normalised lower-hyphen names). Duplicates
    are impossible by name; we record the first (pip only has one per name).
    """
    found: Dict[str, str] = {}
    for dist in imp_meta.distributions():
        name = getattr(dist, "name", None)
        if not name:
            continue
        norm = name.lower()
        if any(norm == p or norm.startswith(p + "-") for p in _OTEL_PREFIXES):
            try:
                ver = dist.version
            except Exception:
                ver = "unknown"
            found.setdefault(norm, ver)
    return found


def _parse_major_minor(version: str) -> Optional[str]:
    """'1.44.0' -> '1.44'; '0.60b1' -> '0.60'; best-effort, None if unparsable."""
    if not version:
        return None
    m = re.match(r"^(\d+)\.(\d+)", version.strip())
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.match(r"^(\d+)", version.strip())
    if m:
        return m.group(1)
    return None


def check_otel_consistency() -> CheckResult:
    """Verify the installed OpenTelemetry family is a single coherent stack.

    PASS  -> api + sdk (at minimum) present and every inter-OTel requirement is
            satisfied by the installed set (no ``pip check``-style conflict).
    WARN  -> OTel not installed at all (memchorus core still works; the mempalace
            MCP path is what needs it) or only a partial set that self-satisfies.
    FAIL  -> OTel present but split/skewed (a requirement on another OTel package
            is unsatisfied by the installed version, or the release lines diverge).
    """
    try:
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
    except ImportError:
        # packaging is a core dependency now; if it is somehow absent we cannot
        # evaluate specifiers, so surface that instead of masking a real skew.
        return CheckResult(
            name="otel_consistency",
            status=FAIL,
            message="Unable to evaluate OTel requirements: 'packaging' is missing",
            hint="pip install packaging (a memchorus core dependency)",
        )

    installed = _otel_packages_present()
    api = installed.get("opentelemetry-api")
    sdk = installed.get("opentelemetry-sdk")

    # Nothing OTel anywhere -> core is fine, the MCP path is just uninstalled.
    if not (api or sdk):
        return CheckResult(
            name="otel_consistency",
            status=WARN,
            message="OpenTelemetry not installed (mempalace/mcp OTel path not present)",
            hint="Install the mempalace/mcp extra if you use the MemPalace MCP recall path.",
        )

    def _parse_req(entry: Any):
        """Normalise a requires entry (PkgInfo str OR Requirement) to (name, SpecifierSet).

        ``Distribution.requires`` is not guaranteed to be a list of
        ``packaging.requirements.Requirement`` — on some metadata backends (and
        here, CPython 3.11 with the pip-installed dists) it yields plain strings
        like ``"opentelemetry-api==1.44.0"``. Parse both shapes. Conditional
        ``extra ==`` deps are returned with an empty specifier so the caller can
        skip them (they are not active in this environment).
        """
        if entry is None:
            return None
        name = getattr(entry, "name", None)
        if name is not None:
            # Requirement/object form.
            spec = SpecifierSet(str(getattr(entry, "specifier", "") or ""))
            marker = getattr(entry, "marker", None)
            return (str(name), spec, marker)
        if isinstance(entry, str):
            try:
                req = Requirement(entry)
            except Exception:
                return None
            return (req.name, SpecifierSet(str(req.specifier)), req.marker)
        return None

    skews: List[str] = []

    # 1) Inter-OTel requirement satisfiability (the precise pip-check signal).
    for dist in imp_meta.distributions():
        dist_name = (getattr(dist, "name", "") or "").lower()
        if not any(dist_name == p or dist_name.startswith(p + "-") for p in _OTEL_PREFIXES):
            continue
        for entry in dist.requires or []:
            parsed = _parse_req(entry)
            if parsed is None:
                continue
            req_name, spec, marker = parsed
            low = req_name.lower()
            if not any(low == p or low.startswith(p + "-") for p in _OTEL_PREFIXES):
                continue
            if low not in installed:
                continue  # not in the installed set -> pip will resolve, not a skew here
            if not spec:
                continue
            # Skip conditional (extra ==) deps that are not active here.
            if marker is not None and "extra" in str(marker):
                continue
            try:
                have = Version(installed[low])
            except Exception:
                continue
            if have not in spec:
                skews.append(
                    f"{dist_name} requires {low}{spec} but installed is {installed[low]}"
                )

    # 2) Release-line divergence among the line-locked packages.
    lines: Dict[str, str] = {}
    for pkg in _OTEL_LINE_PACKAGES:
        if pkg in installed:
            mm = _parse_major_minor(installed[pkg])
            if mm is not None:
                lines[pkg] = mm
    # Only flag when >=2 distinct minor lines exist among the line packages AND
    # at least one of them is a major==1 runtime package (api/sdk/proto-common).
    # semantic-conventions carries a 0.x line, so exclude the 0.x line from the
    # "major" comparison to avoid a false positive on its own 0.5x track.
    major1_lines = {v for k, v in lines.items() if v.split(".")[0] == "1"}
    if len(major1_lines) > 1:
        skews.append(
            "OpenTelemetry 1.x line split: "
            + ", ".join(f"{k}={v}" for k, v in sorted(lines.items()) if v in major1_lines)
        )

    if skews:
        hint = (
            "Reinstall a coherent stack, e.g. "
            "`pip install --upgrade 'opentelemetry-api' 'opentelemetry-sdk' "
            "'opentelemetry-exporter-otlp-proto-grpc' "
            "'opentelemetry-exporter-otlp-proto-common'` "
            "then `pip check`."
        )
        summary = f"OpenTelemetry stack is SPLIT (api={api}, sdk={sdk})"
        if len(skews) <= 3:
            summary += "; " + "; ".join(skews)
        else:
            summary += f"; {len(skews)} constraints unsatisfied (first 3: " + "; ".join(skews[:3]) + ")"
        return CheckResult(
            name="otel_consistency",
            status=FAIL,
            message=summary,
            hint=hint,
        )

    shown = ", ".join(f"{k}={v}" for k, v in sorted(installed.items()))
    return CheckResult(
        name="otel_consistency",
        status=PASS,
        message=f"OpenTelemetry stack coherent ({shown})",
    )


def deps_check_report() -> List[CheckResult]:
    """Focused dependency-coherence report for ``--deps-check``.

    Runs the dependency-integrity check (core packages importable) and the OTel
    coherence check. These two are the install-time invariants #169 guards.
    """
    return [check_dependency_integrity(), check_otel_consistency()]


# ---------------------------------------------------------------------------
# Recall explainability  (--recall "<query>")
#
# Runs the *real* recall pipeline for a query and explains, item by item, exactly
# why each memory scored what it did and whether it would be injected, suppressed
# (GH-141 cross-turn window), or dropped by the char budget — so an operator can
# answer "why did / didn't the agent see X?" without reading a stack trace.
#
# Design constraints (from #173):
#   * It reuses the SAME scorer instance the live agent uses (``orchestrator._scorer``
#     via ``orchestrator.search``), never a fresh default — so the explanation is
#     the explanation, not a re-approximation of it.
#   * Render is read-only: ``hooks.simulate_recall_render`` runs the shared
#     ``_build_context_entries`` core but does NOT mutate the suppression window,
#     so running this diagnostic can never collapse a later live render.
#   * Every number surfaced is a direct output of the production path; the only
#     transformation the doctor applies is JSON/human formatting.
# ---------------------------------------------------------------------------

def _recall_query(query: str, limit: int) -> Dict[str, Any]:
    """Execute the live recall path for *query* and assemble the explanation.

    Returns a fully-populated report dict.  Never raises for the "expected"
    failure modes (orchestrator not registered, empty result set) — those are
    reported with ``status`` + ``reason`` so the caller can decide exit code.
    """
    # Lazy imports — pulls in the full hook pipeline only when --recall is asked
    # for, keeping the plain install-doctor path lightweight (mirrors the
    # existing lazy-import pattern in check_plugin_hooks above).
    from memchorus import get_orchestrator
    from memchorus.hooks import simulate_recall_render, suppression_state

    orchestrator = get_orchestrator()  # type: ignore[assignment]
    if orchestrator is None:
        return {
            "query": query,
            "limit": limit,
            "status": "no_orchestrator",
            "reason": (
                "No MemoryOrchestrator is registered in this process. "
                "--recall evaluates the *live* auto-bootstrap pipeline, which is "
                "only present inside a running Hermes agent session; a fresh "
                "python interpreter has not auto-bootstrapped yet."
            ),
            "results": [],
            "render": None,
            "suppression": None,
        }

    # 1) Production search — same scorer, same dedup, same sort as the agent.
    results: List[Dict[str, Any]]
    search_error = None
    try:
        results = orchestrator.search(query, limit=limit)
    except Exception as exc:  # noqa: BLE001 — doctor must report, not crash
        result = {
            "query": query,
            "limit": limit,
            "status": "search_error",
            "reason": f"orchestrator.search({query!r}) raised {type(exc).__name__}: {exc}",
            "results": [],
            "render": None,
            "suppression": None,
        }
        search_error = str(exc)
        return result

    # 2) Read-only render simulation — what WOULD be injected right now.
    render_report = simulate_recall_render(list(results))

    # 3) Live suppression-window snapshot.
    window = suppression_state()

    # 4) Per-result explainability payload.  ``score_breakdown`` is present on
    #    every result dict because the production path (score_and_rank) attaches
    #    it to RankedResult.meta and orchestrator.search spreads **r.meta in.
    explained: List[Dict[str, Any]] = []
    injected_keys = {i["key"] for i in render_report.get("injected", []) if i.get("key")}
    dropped_keys = {d["key"] for d in render_report.get("dropped", []) if d.get("key")}
    for r in results:
        explained.append({
            "key": r.get("key"),
            "source": r.get("source"),
            "score": r.get("score"),
            "score_breakdown": r.get("score_breakdown"),
            "disposition": (
                "injected" if r.get("key") in injected_keys
                else "dropped_by_budget" if r.get("key") in dropped_keys
                else "suppressed_shown_earlier"
            ),
            "content_preview": (r.get("content") or "")[:200],
        })

    return {
        "query": query,
        "limit": limit,
        "status": "ok",
        "reason": None,
        "results": explained,
        "render": render_report,
        "suppression": window,
    }


def _render_recall_human(report: Dict[str, Any]) -> None:
    """Human-readable explanation of a --recall report."""
    q = report["query"]
    limit = report["limit"]
    results = report.get("results") or []
    render = report.get("render")
    window = report.get("suppression")

    print()
    print(f"MemChorus Recall Explainability — {q!r} (limit {limit})".center(72, "="))
    print()

    if report["status"] == "no_orchestrator":
        print(report["reason"])
        print()
        return

    if report["status"] == "search_error":
        print(f"Search failed: {report['reason']}")
        print()
        return

    if not results:
        print("No results returned by the live recall path for this query.")
        print("This can mean: source(s) disabled, no stored match, or the query")
        print("fell below the relevance threshold. See 'score_breakdown' on any")
        print("returned item to see per-dimension scoring when results DO exist.")
        print()
        return

    for r in results:
        disp = r.get("disposition")
        score = r.get("score")
        print(f"  {r.get('key')}  [{r.get('source')}]  score={score}  disposition={disp}")
        bd = r.get("score_breakdown")
        if isinstance(bd, dict):
            # score_breakdown schema — keys defined in RelevanceScorer.score_breakdown:
            # quality, recency, source_prior, weights{...},
            # contributions{quality,recency,source_type,total},
            # calibration_boost, auto_provenance_penalty,
            # penalty{factor, matches:[[label, factor], ...]}, raw, final.
            contribs = (bd.get("contributions") or {})
            parts = [
                f"{dim}={val:.3f}"
                for dim, val in contribs.items()
                if isinstance(val, (int, float))
            ]
            if parts:
                print(f"      contributions: {' '.join(parts)}")
            boost = bd.get("calibration_boost")
            auto_pen = bd.get("auto_provenance_penalty")
            pen = (bd.get("penalty") or {}) if isinstance(bd.get("penalty"), dict) else {}
            boost_s = f"{boost:.3f}" if isinstance(boost, (int, float)) else "n/a"
            auto_s = f"{auto_pen:.3f}" if isinstance(auto_pen, (int, float)) else "1.0"
            pen_s = f"{pen.get('factor'):.3f}" if isinstance(pen.get("factor"), (int, float)) else "1.0"
            final_v = bd.get("final")
            final_s = f"{final_v:.4f}" if isinstance(final_v, (int, float)) else "n/a"
            print(f"      boost={boost_s}  auto_prov_x={auto_s}  penalty_x={pen_s}  -> final={final_s}")
            pm = pen.get("matches") or []
            if pm:
                names = ", ".join(f"{label}[{factor}]" for label, factor in pm)
                print(f"      penalties matched: {names}")
        preview = (r.get("content_preview") or "").strip().replace("\n", " ")
        if preview:
            print(f"      preview: {preview[:120]}")
        print()

    if render is not None:
        inj = render.get("injected") or []
        dropped = render.get("dropped") or []
        print(f"Render (read-only simulation): {len(inj)} would be injected, {len(dropped)} dropped by budget.")
        # (#184) Surface the degradation state as an explicit banner so the
        # operator can see at a glance that at least one hit was served from
        # the local fallback cache rather than the live MemPalace graph.
        if render.get("degraded"):
            print("  ⚠ DEGRADED READ: part of this recall was served from the")
            print("    local fallback cache because the MemPalace MCP backend was")
            print("    unreachable at recall time. Those entries may be stale.")
        for i in inj:
            flag = " (suppressed→marker only)" if i.get('suppressed') else ""
            print(f"    + {i.get('key')} score={i.get('score')}{flag}")
        for d in dropped:
            print(f"    - {d.get('key')} score={d.get('score')} reason={d.get('reason')}")
    else:
        print("Render: (not computed — see search_error above)")

    if window is not None:
        print()
        print(
            f"Suppression window: profile={window.get('profile')} "
            f"window_size={window.get('window_size')} ttl={window.get('ttl_seconds')}s "
            f"configured={window.get('configured')} entries={window.get('entries_total')}"
        )
        iw = window.get("in_window") or {}
        for k, v in list(iw.items())[:10]:
            print(f"    in-window: {k} age={v.get('age_seconds')}s ttl_left={v.get('remaining_ttl_seconds')}s")
    print()


def _render_recall_json(report: Dict[str, Any]) -> None:
    """Machine-readable --recall report."""
    payload = {
        "query": report.get("query"),
        "limit": report.get("limit"),
        "status": report.get("status"),
        "reason": report.get("reason"),
        "results": report.get("results"),
        "render": report.get("render"),
        "suppression": report.get("suppression"),
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _recall_exit_code(report: Dict[str, Any]) -> int:
    """--recall exit code: 0 when the path ran, 1 on pipeline failure.

    Acceptance rule (IMPL #173):
      * ``status == "ok"``                -> 0 (even if the result set is empty)
      * ``status == "search_error"``      -> 1 (the live recall path raised)
      * ``status == "no_orchestrator"``   -> 1 (no live orchestrator is
                                              registered in this process, so the
                                              pipeline could not run at all)
    """
    return 0 if report.get("status") == "ok" else 1

# ---------------------------------------------------------------------------
# Palace layout diagnostic (--palace-layout)
# ---------------------------------------------------------------------------

def _palace_layout_root(explicit: Optional[str]) -> Path:
    """Resolve the root the layout diagnostic should classify.

    Precedence: explicit ``--palace-root`` > ``$PALACE_ROOT`` /
    ``$MEMPALACE_PALACE_PATH`` env > ``~/.mempalace`` (MemPalace's own
    ``config_dir`` default).  This is the *root / parent* — the same value
    the writer is pointed at — and :func:`memchorus.palace_path.palace_data_dir`
    decides whether its real data is here or one level down under ``palace/``.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("PALACE_ROOT") or os.environ.get("MEMPALACE_PALACE_PATH")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".mempalace"


def palace_layout_report(strict: bool = False,
                         explicit_root: Optional[str] = None) -> List[CheckResult]:
    """Focused MemPalace layout diagnostic for ``--palace-layout``.

    Reports, through the **same resolver the reader uses**
    (:func:`memchorus.palace_path.classify`), whether the configured palace
    root points at the directory that actually holds ``chroma.sqlite3``:

    - **canonical**   -> PASS.  Root is already the leaf that holds the data.
    - **legacy-leaf** -> the real data is at ``<root>/palace`` while the
      reader is pointed at ``<root>`` (the Aug 20 split).  ``--strict``
      drives this to **FAIL** (so a CI/gate can block on the fallback);
      otherwise **WARN** with the exact ``--palace`` repoint and
      ``memchorus-init --migrate`` to apply durably.
    - **fresh**       -> PASS (first run; nothing yet to split).

    ``--strict`` is what makes ``memchorus-doctor`` fail fast when the reader
    would silently report 0 rows against an empty shell.
    """
    root = _palace_layout_root(explicit_root)
    layout = palace_path_classify(root)

    if layout.branch == palace_path_mod.BRANCH_LEGACY_LEAF:
        canonical = layout.resolved_dir
        status = FAIL if strict else WARN
        message = (
            f"palace data is at {layout.configured_dir / 'palace'} but the "
            f"configured root is {layout.configured_dir} — the reader would "
            f"open an empty shell and report 0 rows."
        )
        hint = (
            f"Point at the leaf directly (--palace {canonical}) or run "
            f"memchorus-init --migrate {layout.configured_dir} on the "
            f"affected profile's config to re-point it durably."
        )
    elif layout.branch == palace_path_mod.BRANCH_CANONICAL:
        rows = layout.row_count
        row_txt = f"{rows} row(s)" if rows is not None else "data present"
        return [CheckResult(
            name="palace_layout",
            status=PASS,
            message=(
                f"{layout.configured_dir} is the canonical leaf; "
                f"{row_txt} in {layout.resolved_data_file}."
            ),
        )]
    else:  # BRANCH_FRESH
        return [CheckResult(
            name="palace_layout",
            status=PASS,
            message=(
                f"{layout.configured_dir} is a fresh palace (no data yet); "
                f"it will be the canonical leaf on first write."
            ),
        )]

    return [CheckResult(
        name="palace_layout",
        status=status,
        message=message,
        hint=hint,
    )]
# Provenance report  (--provenance-report)
#
# Scans the MemPalace local cache (JSON files) and reports how many stored
# entries carry a non-empty ``source_file`` (provenance) field versus how
# many are missing it.  This gives an operator a quick health signal on
# whether IMPL #166's fix is actually landing drawers with provenance.
#
# Exit code: 0 when all cached entries have non-empty source_file,
#           1 when one or more entries are missing it,
#           2 when the cache directory cannot be found or read.
# ---------------------------------------------------------------------------

def _cache_dir_paths() -> List[Path]:
    """Candidate cache directories to scan (most-specific first)."""
    paths: List[Path] = []
    try:
        from memchorus.hermes_home import hermes_home
        paths.append(hermes_home() / "mempalace_cache")
    except Exception:
        pass
    paths.append(Path.home() / ".hermes" / "mempalace_cache")
    paths.append(Path.home() / ".mempalace")
    return paths


def _scan_cache_provenance(cache_dir: Path) -> Dict[str, Any]:
    """Scan a cache directory and return a provenance coverage report."""
    if not cache_dir.exists() or not cache_dir.is_dir():
        return {
            "cache_dir": str(cache_dir),
            "status": "not_found",
            "total": 0, "with_source_file": 0, "missing_source_file": 0,
            "sample_missing": [],
        }

    files = sorted(cache_dir.glob("*.json"))
    total = len(files)
    with_sf = 0
    missing_sf = 0
    sample_missing: List[str] = []

    for fp in files:
        try:
            with open(fp) as fh:
                data = json.load(fh)
            sf = data.get("source_file") if isinstance(data, dict) else None
            if sf and str(sf).strip():
                with_sf += 1
            else:
                missing_sf += 1
                if len(sample_missing) < 5:
                    sample_missing.append(fp.name)
        except Exception:
            missing_sf += 1
            if len(sample_missing) < 5:
                sample_missing.append(f"{fp.name} (unparseable)")

    pct = (with_sf / total * 100) if total > 0 else 0.0
    return {
        "cache_dir": str(cache_dir),
        "status": "ok",
        "total": total,
        "with_source_file": with_sf,
        "missing_source_file": missing_sf,
        "coverage_pct": round(pct, 1),
        "sample_missing": sample_missing,
    }


def _provenance_report() -> Dict[str, Any]:
    """Find the best cache dir and run a provenance scan on it."""
    candidates = _cache_dir_paths()
    for c in candidates:
        if c.exists() and c.is_dir():
            return _scan_cache_provenance(c)
    return {
        "cache_dir": str(candidates[0]) if candidates else "unknown",
        "status": "not_found",
        "total": 0, "with_source_file": 0, "missing_source_file": 0,
        "sample_missing": [],
    }


def _render_provenance_human(report: Dict[str, Any]) -> None:
    """Human-readable --provenance-report output."""
    print()
    print("MemChorus Provenance Report".center(72, "="))
    print()

    if report.get("status") == "not_found":
        print(f"Cache directory not found: {report.get('cache_dir')}")
        print("No cached MemPalace entries to audit. This is expected if MemChorus")
        print("has not yet auto-stored any memories, or if the cache directory")
        print("has been cleared.")
        print()
        return

    total = report.get("total", 0)
    with_sf = report.get("with_source_file", 0)
    missing = report.get("missing_source_file", 0)
    pct = report.get("coverage_pct", 0.0)

    print(f"Cache directory : {report.get('cache_dir')}")
    print(f"Total entries   : {total}")
    print(f"With provenance : {with_sf}  ({pct}%)")
    print(f"Missing prov  : {missing}")
    print()

    if missing > 0:
        print(f"Sample entries missing source_file (up to 5):")
        for name in report.get("sample_missing", []):
            print(f"  - {name}")
        print()
        if pct < 100:
            print(
                f"  => {missing} of {total} cached entries (i.e. {100 - pct:.1f}%) "
                "do not carry a source_file provenance field."
            )
            print(
                "     This typically means the entry was stored before the IMPL #166"
            )
            print(
                "     fix was applied (when capture_outcome did not set source_file"
            )
            print(
                "     in the payload).  New auto-stored entries should carry provenance."
            )
            print()
            print("  Proposed backfill policy for these pre-#166 entries (one-time migration):")
            print("    Option A) Leave source_file empty for this historical set — the original")
            print("               source cannot be recovered, so no locator is recorded.")
            print("    Option B) Mark these entries with an 'orphan' sentinel value to indicate")
            print("               a pre-#166 record with no source recovered.")
            print("    This is a one-time migration: once applied, the orphan set does not grow,")
            print("    because new auto-stored entries carry provenance automatically. The doctor")
            print("    does NOT apply any backfill itself — it remains read-only and diagnostic.")
        print()
    else:
        print("All cached entries carry a non-empty source_file provenance field.")
        print("IMPL #166 fix is active and provenance is being captured correctly.")
        print()


def _render_provenance_json(report: Dict[str, Any]) -> None:
    """Machine-readable --provenance-report output."""
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


def _provenance_exit_code(report: Dict[str, Any]) -> int:
    """Exit code: 2=not found, 0=all have provenance, 1=some missing."""
    if report.get("status") == "not_found":
        return 2
    if report.get("missing_source_file", 0) > 0:
        return 1
    return 0


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
        check_project_record_resolution,
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

def _result_to_dict(r: CheckResult) -> Dict[str, Any]:
    """Serialise a CheckResult for ``--json`` output."""
    out: Dict[str, Any] = {
        "name": r.name,
        "status": r.status,
        "message": r.message,
    }
    if r.hint:
        out["hint"] = r.hint
    return out


def _emit(results: List[CheckResult], as_json: bool) -> None:
    """Render results either as the human report or as a JSON document."""
    if as_json:
        print(
            json.dumps(
                {
                    "ok": not any(r.status == FAIL for r in results),
                    "results": [_result_to_dict(r) for r in results],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_report(results)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``memchorus-doctor``.

    Supported flags::

        memchorus-doctor                 full install-health report (default)
        memchorus-doctor --deps-check    focused dependency-coherence report
                                         (OTel family / core integrity)
        memchorus-doctor --recall "Q"    recall explainability: run the live recall
                                         pipeline for a query and explain each
                                         candidate's score + budget/suppression
        memchorus-doctor --limit N       --recall max-candidates (default 10)
        memchorus-doctor --palace-layout report whether the configured palace
                                         root points at the dir that holds
                                         chroma.sqlite3 (canonical layout)
        memchorus-doctor --palace-layout --strict
                                         --strict makes a legacy-leaf split
                                         FAIL (non-zero exit) so a gate can
                                         block on it
        memchorus-doctor --palace-root <root>
                                         classify this specific root rather
                                         than ``~/.mempalace``
        memchorus-doctor --project <name>  resolve the keyed ``project:<name>``
                                         record (IMPL #163.4 / spec §4.7) and
                                         print its location / standard /
                                         reconciled channels
        memchorus-doctor --json          emit machine-readable JSON instead of
                                         the human table

    Returns:
      * full report / ``--palace-layout`` / ``--deps-check`` -> 0 when no FAIL,
        1 otherwise (a WARN is not a failure; under ``--strict`` a legacy
        palace layout is promoted to FAIL, see :func:`palace_layout_report`).
      * ``--recall <q>`` -> 0 when the pipeline ran (``status`` ``ok``, even if
        the result set is empty), 1 when the pipeline could not run (no
        registered orchestrator, or ``search`` raised), 2 on bad arguments.
        A diagnostic is always printed.
      * ``--project <name>`` -> 0 when the record resolved (``status`` ``ok``),
        1 when it did not (no orchestrator registered, source opted out, or
        the resolver raised), 2 on bad arguments. A diagnostic is always
        printed (human or ``--json``).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args

    # ------------------------------------------------------------------ #
    # --recall "<query>" — explain a live recall decision (IMPL #173)     #
    # ------------------------------------------------------------------ #
    if "--recall" in args:
        try:
            idx = args.index("--recall")
            if idx + 1 >= len(args):
                print(
                    "error: --recall requires a query string, e.g. "
                    '--recall "deployment note"'
                )
                return 2
            query = args[idx + 1]
        except ValueError:  # unreachable — `in` already gated this branch
            return 2

        limit = 10
        if "--limit" in args:
            try:
                lidx = args.index("--limit")
                if lidx + 1 >= len(args):
                    print("error: --limit requires an integer value")
                    return 2
                limit = max(1, int(args[lidx + 1]))
            except ValueError as exc:
                print(f"error: --limit must be an integer (got {exc})")
                return 2

        report = _recall_query(query, limit)
        if as_json:
            _render_recall_json(report)
        else:
            _render_recall_human(report)
        return _recall_exit_code(report)

    # ------------------------------------------------------------------ #
    # --project "<name>" — resolve the keyed project:<name> record        #
    # (IMPL #163.4, spec §4.7)                                            #
    # ------------------------------------------------------------------ #
    if "--project" in args:
        try:
            idx = args.index("--project")
            if idx + 1 >= len(args):
                print('error: --project requires a project name, e.g. --project "memchorus"')
                return 2
            project_name = args[idx + 1]
        except ValueError:  # unreachable — `in` already gated this branch
            return 2

        report = _project_record_report(project_name)
        if as_json:
            _render_project_report_json(report)
        else:
            _render_project_report_human(report)
        return _project_exit_code(report)

    # ------------------------------------------------------------------ #
    # --palace-layout / --deps-check / full install-health report          #
    # --provenance-report — audit source_file coverage in local cache      #
    # ------------------------------------------------------------------ #
    if "--provenance-report" in args:
        report = _provenance_report()
        if as_json:
            _render_provenance_json(report)
        else:
            _render_provenance_human(report)
        return _provenance_exit_code(report)

    # ------------------------------------------------------------------ #
    # --deps-check / default install-health report                         #
    # ------------------------------------------------------------------ #
    deps_only = "--deps-check" in args
    palace_layout = "--palace-layout" in args
    strict = "--strict" in args

    def _value(flag: str) -> Optional[str]:
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                return args[idx + 1]
        return None

    explicit_root = _value("--palace-root")

    if palace_layout:
        results = palace_layout_report(strict=strict, explicit_root=explicit_root)
    elif deps_only:
        results = deps_check_report()
    else:
        results = run_checks()
    _emit(results, as_json)
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
