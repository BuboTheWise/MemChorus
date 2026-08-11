"""Verify _instance is importable before bootstrap (Bug 2 fix)."""
import os
import subprocess
import sys
from pathlib import Path

# Resolve repo root relative to this test file so CI runners work too.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _subproc(code, timeout=30):
    """Run a Python snippet in a child process with PYTHONPATH pointing at src/."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=timeout,
        cwd=_REPO_ROOT, env=env,
    )


def test_from_import_no_crash():
    """from memchorus import _instance must not raise ImportError."""
    result = _subproc("from memchorus import _instance; print(_instance is None)")
    assert result.returncode == 0, f"Import crashed: {result.stderr}"
    assert "True" in result.stdout


def test_hasattr_pre_bootstrap():
    """hasattr(memchorus, '_instance') before bootstrap."""
    result = _subproc("import memchorus; print(hasattr(memchorus,'_instance'))")
    assert result.returncode == 0, f"Crash: {result.stderr}"
    assert "True" in result.stdout


def test_post_bootstrap_non_none():
    """_instance is real MemoryOrchestrator after bootstrap."""
    code = (
        "import memchorus as m; "
        "assert getattr(m,'_instance') is None; "
        "m._trigger_lazy_bootstrap(); "
        "print(type(getattr(m,'_instance')).__name__)"
    )
    result = _subproc(code)
    assert result.returncode == 0, f"Crash: {result.stderr}"
    assert "MemoryOrchestrator" in result.stdout
