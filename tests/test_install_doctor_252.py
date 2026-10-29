"""#252 — advertised bin-entrypoint probe (``install_doctor``).

RED→GREEN proof for the new probe that detects dangling / broken advertised
bin entrypoints (``~/.hermes/bin/{memchorus-doctor,memchorus-init,memchorus-
recall,memchorus-recalibrate,mempalace-mcp}``), the silent state left behind
when a host toolchain re-provision drops the pip-installed packages but
leaves the symlinks.

Design notes / determinism
--------------------------
* The probe's ``bin_dir`` is ``hermes_home() / "bin"`` and ``hermes_home()``
  honours ``$HERMES_HOME`` when it points at an existing directory, so every
  test redirects it at a fresh tmp tree and never touches the real
  ``~/.hermes/bin``.
* AC1a (link target missing) is exercised by a *dangling* symlink — whose
  ``is_symlink()`` is True on every platform yet whose ``resolve()`` does not
  exist.
* AC1b (target exists but the owning package is gone from the interpreter)
  is exercised by pointing ``mempalace-mcp`` at ``sys.executable`` — the
  in-repo venv, which ships ``memchorus`` but NOT ``mempalace``. The test
  asserts the precondition first and skips cleanly on any machine where the
  repo venv happens to also carry mempalace, so it stays green on CI without
  hardcoding a base-interpreter path.
* Status semantics under test (AC2): a broken entrypoint is ``WARN`` in the
  normal report and ``FAIL`` under ``strict=True``.

On the pre-fix head, ``check_bin_entrypoints`` does not exist yet, so the
import below raises and the whole module is RED; after the fix it is GREEN.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memchorus.install_doctor import (
    FAIL,
    PASS,
    WARN,
    check_bin_entrypoints,
)
import memchorus.install_doctor as doctor


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bin_home(tmp_path, monkeypatch):
    """A scratch HERMES_HOME with an empty bin/ dir, wired into the doctor."""
    home = tmp_path / "home"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # the probe resolves hermes_home() at call time; ensure it reads our dir
    assert doctor.hermes_home() == home
    return bin_dir


def _symlink(bin_dir: Path, name: str, target: str) -> Path:
    link = bin_dir / name
    link.symlink_to(target)
    return link


# ---------------------------------------------------------------------------
# AC1a — dangling symlink (link target missing after a re-provision)
# ---------------------------------------------------------------------------

def test_dangling_symlink_is_flagged_normal_report(bin_home):
    """A dangling advertised link must surface in the *normal* report (AC2)."""
    _symlink(bin_home, "memchorus-doctor", str(bin_home / "gone" / "python3.13"))

    result = check_bin_entrypoints(strict=False)

    # non-PASS ...
    assert result.status in (WARN, FAIL)
    # ... and it must name the specific broken entrypoint
    assert "memchorus-doctor" in result.message
    # it must be flagged as dangling, not importable-then-broken
    assert "target missing" in result.message


def test_dangling_symlink_is_fail_under_strict(bin_home):
    """AC2: the same broken link is a *failure code* under --strict."""
    _symlink(bin_home, "memchorus-doctor", str(bin_home / "gone" / "python3.13"))

    normal = check_bin_entrypoints(strict=False)
    strict = check_bin_entrypoints(strict=True)

    assert normal.status == WARN, f"normal report should WARN, got {normal.status}"
    assert strict.status == FAIL, f"strict report should FAIL, got {strict.status}"


# ---------------------------------------------------------------------------
# AC1b — target exists but the owning package is gone from its interpreter
# ---------------------------------------------------------------------------

def _repo_python_cannot_import_mempalace() -> bool:
    import importlib.util

    can = importlib.util.find_spec("mempalace") is not None
    return not can


def test_unimportable_entrypoint_is_flagged(bin_home):
    """mempalace-mcp → interpreter that no longer has mempalace (AC1b)."""
    if not _repo_python_cannot_import_mempalace():
        pytest.skip("repo venv can import mempalace here; cannot fabricate a miss")

    # sys.executable is the in-repo venv: has memchorus, NOT mempalace.
    _symlink(bin_home, "mempalace-mcp", sys.executable)

    result = check_bin_entrypoints(strict=False)

    assert result.status in (WARN, FAIL)
    assert "mempalace-mcp" in result.message
    assert "mempalace" in result.message  # names the missing package


# ---------------------------------------------------------------------------
# healthy path — every advertised entrypoint intact and importable
# ---------------------------------------------------------------------------

def test_all_healthy_reports_pass(bin_home):
    """Healthy memchorus entrypoints (→ repo venv, which imports memchorus)."""
    for name in ("memchorus-doctor", "memchorus-init", "memchorus-recall",
                 "memchorus-recalibrate"):
        _symlink(bin_home, name, sys.executable)
    # mempalace-mcp is intentionally absent -> not checked on this machine.

    result = check_bin_entrypoints(strict=False)

    assert result.status == PASS
    assert "memchorus-doctor" in result.message
    assert "intact" in result.message.lower() or "importable" in result.message.lower()


# ---------------------------------------------------------------------------
# no advertised bin entries present -> informational WARN, not a false PASS
# ---------------------------------------------------------------------------

def test_no_advertised_entries_is_informative_warn(bin_home):
    """Empty bin/ dir (fresh machine): no links to check -> WARN, not FAIL."""
    result = check_bin_entrypoints(strict=False)

    assert result.status == WARN
    assert result.status is not FAIL
