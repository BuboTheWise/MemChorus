"""Verify _instance is importable before bootstrap (Bug 2 fix)."""
import subprocess
import sys
from pathlib import Path

# Resolve repo root relative to this test file so CI runners work too.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_from_import_no_crash():
    """from memchorus import _instance must not raise ImportError."""
    result = subprocess.run(
        [sys.executable, "-c", "from memchorus import _instance; print(_instance is None)"],
        capture_output=True, text=True, timeout=30,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, f"Import crashed: {result.stderr}"
    assert "True" in result.stdout


def test_hasattr_pre_bootstrap():
    """hasattr(memchorus, '_instance') before bootstrap."""
    result = subprocess.run(
        [sys.executable, "-c", "import memchorus; print(hasattr(memchorus,'_instance'))"],
        capture_output=True, text=True, timeout=30,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, f"Crash: {result.stderr}"
    assert "True" in result.stdout


def test_post_bootstrap_non_none():
    """_instance is real MemoryOrchestrator after bootstrap."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import memchorus as m; "
         "assert getattr(m,'_instance') is None; "
         "m._trigger_lazy_bootstrap(); "
         "print(type(getattr(m,'_instance')).__name__)"],
        capture_output=True, text=True, timeout=30,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, f"Crash: {result.stderr}"
    assert "MemoryOrchestrator" in result.stdout
