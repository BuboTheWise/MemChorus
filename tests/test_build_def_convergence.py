"""Regression: the packaging definition must be single-sourced and version-honest.

This locks down the class of drift introduced long ago (see #118/#148), where
a *separate* packaging file carried a stale hard-coded version that shadowed
the canonical one. The invariant now enforced is:

1. There is ONE canonical build definition (the root ``pyproject.toml``). A
   duplicate ``src/pyproject.toml`` is forbidden — it is exactly the redundant
   file that used to shadow the real one.
2. ``setup.py`` and ``pyproject.toml`` must not each carry their own,
   possibly-divergent, concrete version. Any concrete version present must
   equal ``src/memchorus/__init__.py::__version__``. A packaging file may
   instead be *equal-by-construction* (a bare ``setup()`` shim, or a PEP 621
   ``dynamic = ["version"]``) — that cannot drift, so it is allowed.
3. When the package is installed, the version pip actually records (via
   importlib metadata) must equal the runtime ``memchorus.__version__`` and
   the source-of-truth in the checkout — otherwise a user's installed package
   reports one thing while the source claims another.

Tests are hermetic (no network, no install required) except test 3, which
skips gracefully when memchorus is not installed in the environment.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import memchorus  # noqa: F401  (proves the package is importable in the env)

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT = REPO_ROOT / "src" / "memchorus" / "__init__.py"
SETUP = REPO_ROOT / "setup.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
LEGACY_NESTED = REPO_ROOT / "src" / "pyproject.toml"
GATE = REPO_ROOT / "scripts" / "check_version_sync.py"


def _runtime_version() -> str:
    text = INIT.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', text, re.MULTILINE)
    assert m, "could not find __version__ in src/memchorus/__init__.py"
    return m.group(1).strip()


def _project_section(text: str) -> str:
    """Only the body of the ``[project]`` TOML table (not ``[project.*]`` or ``[tool.*]``)."""
    in_project, body = False, []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_project = s == "[project]"
            continue
        if in_project:
            body.append(line)
    return "\n".join(body)


def _project_concrete_version(text: str):
    """The literal ``[project] version = "X.Y.Z"``, or None if dynamic/absent."""
    m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)', _project_section(text), re.MULTILINE)
    return m.group(1).strip() if m else None


def _is_dynamic_version(text: str) -> bool:
    return bool(re.search(r'dynamic\s*=\s*\[[^\]]*["\']version["\']', _project_section(text)))


def test_legacy_nested_pyproject_is_gone():
    """The redundant ``src/pyproject.toml`` must not exist — only the root one."""
    assert not LEGACY_NESTED.exists(), (
        "src/pyproject.toml is a redundant build definition that shadows the "
        "canonical root pyproject.toml; packaging must live in one place."
    )


def test_packaging_versions_do_not_diverge():
    """setup.py / pyproject.toml are honest about the version relative to __init__."""
    canon = _runtime_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", canon), f"unexpected canonical version format: {canon}"

    # --- setup.py ---
    setup_text = SETUP.read_text(encoding="utf-8") if SETUP.exists() else ""
    m = re.search(r'version\s*=\s*["\']([^"\']+)', setup_text)
    if m:
        assert m.group(1).strip() == canon, (
            f"setup.py hard-codes version {m.group(1)} != __init__ {canon} "
            "(a concrete setup.py version must match, or use the shim form)."
        )

    # --- root pyproject.toml ---
    assert PYPROJECT.exists(), "root pyproject.toml is the canonical build definition"
    pp = PYPROJECT.read_text(encoding="utf-8")
    concrete = _project_concrete_version(pp)
    if concrete is not None:
        assert concrete == canon, (
            f"pyproject.toml [project] version {concrete} != __init__ {canon}"
        )
    else:
        assert _is_dynamic_version(pp), (
            "pyproject.toml [project] has no concrete version and is not declared "
            "dynamic — the build would emit an empty/stale version."
        )


def test_version_sync_gate_passes():
    """The committed four-surface gate must pass on the current tree."""
    r = subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True)
    assert r.returncode == 0, (
        f"version-sync gate failed (exit {r.returncode}):\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )


def test_installed_version_matches_source_when_installed():
    """pip's recorded version == runtime __version__ == source __version__."""
    if importlib.util.find_spec("memchorus") is None:
        import pytest
        pytest.skip("memchorus is not installed in this environment")

    import importlib.metadata as md

    installed = md.version("memchorus")
    runtime = memchorus.__version__
    source = _runtime_version()

    # The installed package must report the same version it exposes at runtime.
    assert installed == runtime, (
        f"installed package reports {installed} but memchorus.__version__ is {runtime}"
    )
    # ...and both must match the checkout's source-of-truth (catches a stale install
    # or a build-def version that diverged from __init__).
    assert installed == source, (
        f"installed {installed} != source __version__ {source}"
    )
