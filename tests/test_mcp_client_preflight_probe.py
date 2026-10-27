"""#250 — MCP client: (1) prefer the active venv interpreter over stale PATH
shims; (2) surface the real pydantic ImportError (incl. pydantic_core detail)
instead of a swallowed generic failure.

RED (current code):
  • test_discover_python_prefers_active_venv       — FAILS: PATH shim wins
  • test_probe_pydantic_import_surfaces_real_error — FAILS: AttributeError
    (``_McpClient._probe_pydantic_import`` does not exist yet)

GREEN (after fix):
  • test_discover_python_prefers_active_venv       — PASSES: sys.executable
    is consulted before PATH shim / pipx venv
  • test_probe_pydantic_import_surfaces_real_error — PASSES: probe returns
    (False, diagnostic-with-pydantic_core-detail)
  • test_probe_pydantic_import_healthy_ok          — PASSES: healthy
    interpreter returns (True, …), guards against false negatives
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helper: construct a bare _McpClient without triggering the transport detector
# to avoid filesystem I/O unrelated to the test's purpose.
# ---------------------------------------------------------------------------

def _make_client():
    """Construct a bare _McpClient with the transport detector disabled."""
    import memchorus.mempalace_memory_source as mod
    from memchorus.mempalace_memory_source import _McpClient

    with patch.object(mod._McpTransportDetector, "detect", return_value=None):
        return _McpClient(timeout=1, config={})


# ---------------------------------------------------------------------------
# (1) Active-venv-preference test
# ---------------------------------------------------------------------------

def test_discover_python_prefers_active_venv(tmp_path: Path):
    """#250 (1): when a stale PATH shim exists, the active venv interpreter
    (``sys.executable``) must win.

    RED (current code): shim is step 2, sys.executable is step 4
        → resolved != sys.executable
    GREEN (after fix): sys.executable is step 2 (right after config override)
        → resolved == sys.executable (realpath)
    """
    # Ensure tmp_path is on PATH and contains a "mempalace-python" shim
    shim = tmp_path / "mempalace-python"
    shim.write_text("#!/bin/sh\necho 'stale shim'\n")
    os.chmod(shim, 0o755)

    path_with_shim = str(tmp_path) + os.pathsep + os.environ.get("PATH", "")

    client = _make_client()
    # Bypass config-override logic by calling _discover_python directly
    # with sys.executable available. The shim is now visible via PATH.
    with patch.dict(os.environ, {"PATH": path_with_shim}):
        resolved = client._discover_python()

    resolved_real = os.path.realpath(resolved)
    exec_real = os.path.realpath(sys.executable)

    assert resolved_real == exec_real, (
        f"_discover_python must prefer the active venv interpreter "
        f"({exec_real!r}) over a stale PATH shim "
        f"({os.path.realpath(str(shim))!r}); got {resolved_real!r}"
    )


# ---------------------------------------------------------------------------
# (2) Pre-flight pydantic probe tests
# ---------------------------------------------------------------------------

def test_probe_pydantic_import_surfaces_real_error(tmp_path: Path):
    """#250 (2): when ``import pydantic`` fails with a cross-ABI pydantic_core
    error, the probe must return (False, real_diagnostic) that names
    pydantic_core — not a swallowed generic failure.

    RED (current code): AttributeError — _probe_pydantic_import does not exist.
    GREEN (after fix):  (False, "…pydantic_core cross-ABI mismatch…")
    """
    # Inject a shadow pydantic.py that raises ImportError at import time
    (tmp_path / "pydantic.py").write_text(
        "raise ImportError('pydantic_core cross-ABI mismatch (test shadow)')\n"
    )

    client = _make_client()
    ok, diagnostic = client._probe_pydantic_import(sys.executable, extra_path=str(tmp_path))

    assert not ok, f"probe should report failure for shadow module; got ok=True, diagnostic={diagnostic!r}"
    lower = diagnostic.lower()
    assert "pydantic_core" in lower or "cross-abi" in lower or "shadow" in lower, (
        f"probe diagnostic must name pydantic_core / cross-ABI detail; got: {diagnostic!r}"
    )


def test_probe_pydantic_import_healthy_ok():
    """Guard: a healthy interpreter (pydantic + pydantic_core import cleanly)
    must return (True, …), not a false-negative failure.

    RED (current code): AttributeError — method does not exist.
    GREEN (after fix):  (True, "pydantic + pydantic_core import OK")
    """
    client = _make_client()
    ok, diagnostic = client._probe_pydantic_import(sys.executable)

    assert ok, f"healthy interpreter should pass probe; got ok=False, diagnostic={diagnostic!r}"
