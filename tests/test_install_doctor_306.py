"""#306 — shebang repair for malformed CLI launchers.

RED→GREEN proof for the GH #306 fix:

* ``memchorus.shebang_repair`` — classify / resolve / repair of uv-style
  relocatable wrappers and the doubled-path ``#!/bin/sh`` exec wrapper that
  #306 reports (``.../venv/bin//.../venv/bin/python: No such file or
  directory``);
* ``install_doctor.check_bin_entrypoints`` — the detection path tags the
  broken launcher with ``GH #306`` and points at ``--repair`` instead of the
  generic re-provision hint;
* ``install_doctor._repair_entrypoints_main`` — the ``--repair`` entry point
  and its exit-code contract (0 = healthy/repaired+verified, 1 = at least
  one launcher unrepairable).

Determinism
-----------
All launchers are synthetic files written under a clean ``/tmp`` root
(never under ``~/.hermes``), and the bin directory is passed explicitly to
every code path, so no real install, profile, or symlink tree is touched.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from memchorus import shebang_repair
import memchorus.install_doctor as doctor
from memchorus.install_doctor import FAIL, PASS, check_bin_entrypoints


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _clean_root() -> Path:
    """A writable root with no ``profiles``/``profile`` path segment, so the
    doctor's home-based probes (GH #298) can never leak into the real tree.
    (Same convention as ``test_install_doctor_252.py``.)

    On Windows the real tempdir is preferred first: CI runners ship
    ``C:\\tmp`` pre-created, so ``/tmp`` *exists* there yet is
    root-relative under ``ntpath`` (not absolute) — and a doubled-path
    token built on a non-absolute root takes the non-absolute → healthy
    branch of ``_token_ok`` (the shape reserved for uv relocatable
    wrappers). A drive-absolute root reproduces real Windows venv shapes
    and keeps the broken-launcher tests meaningful instead of vacuous.
    """
    if sys.platform == "win32":
        cands = [tempfile.gettempdir()]
    else:
        cands = ["/tmp", "/var/tmp"]
    if tempfile.gettempdir() not in cands:
        cands.append(tempfile.gettempdir())
    for cand in cands:
        p = Path(cand)
        if p.exists() and not any(
            seg in ("profiles", "profile") for seg in p.parts
        ):
            return p
    return Path(tempfile.gettempdir())


@pytest.fixture
def venv_bin() -> Path:
    """A scratch ``venv/bin`` with a real ``python`` (symlink to the
    interpreter running the test)."""
    root = Path(tempfile.mkdtemp(prefix="memchorus306_", dir=str(_clean_root())))
    bin_dir = root / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    return bin_dir


def _write_wrapper(path: Path, interpreter_expr: str, body: str = '') -> None:
    """Write a uv-style ``#!/bin/sh`` console-script launcher.

    Line 2 is both a shell command (``exec "<interp>" "$0" "$@"``) and, once
    Python runs the file, a harmless triple-quoted string expression.
    """
    path.write_text(
        "#!/bin/sh\n"
        f"'''exec' \"{interpreter_expr}\" \"$0\" \"$@\"\n"
        "'''\n"
        + (body or 'import sys\nprint("memchorus-recall launched OK")\nsys.exit(0)\n')
    )
    path.chmod(0o755)


UV_EXPR = '$(dirname -- "$(realpath -- "$0")")"/\'python\''


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def test_classify_healthy_uv_wrapper(venv_bin):
    p = venv_bin / "memchorus-recall"
    _write_wrapper(p, UV_EXPR)
    info = shebang_repair.classify(p)
    assert info.ok, f"healthy uv wrapper misclassified: {info.kind}"
    resolved = shebang_repair.resolve_token(info.interpreter, p) if info.interpreter else None
    assert resolved is not None and resolved.exists()


def test_classify_broken_doubled_path(venv_bin):
    p = venv_bin / "memchorus-recall"
    # the GH #306 shape: bin/ prefix concatenated with an absolute path
    _write_wrapper(p, f"{venv_bin}/{venv_bin}/python")
    info = shebang_repair.classify(p)
    assert not info.ok, "doubled-path launcher must classify as broken"


# ---------------------------------------------------------------------------
# repair (library level)
# ---------------------------------------------------------------------------

def test_repair_doubled_path_and_launch(venv_bin):
    p = venv_bin / "memchorus-recall"
    _write_wrapper(p, f"{venv_bin}/{venv_bin}/python")
    before = p.read_bytes()

    results = shebang_repair.repair_entrypoints(bin_dir=venv_bin, dry_run=False)
    recall = next(r for r in results if r.name == "memchorus-recall")
    assert recall.action == "repaired"
    assert recall.found_interpreter != recall.set_interpreter
    assert p.read_bytes() != before

    # the repaired launcher must actually launch (shell exec + python body)
    proc = subprocess.run(["sh", str(p)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "launched OK" in proc.stdout


def test_repair_leaves_healthy_wrapper_untouched(venv_bin):
    p = venv_bin / "memchorus-doctor"
    _write_wrapper(p, UV_EXPR)
    before = p.read_bytes()

    results = shebang_repair.repair_entrypoints(bin_dir=venv_bin, dry_run=False)
    doc = next(r for r in results if r.name == "memchorus-doctor")
    assert doc.action == "ok"
    assert p.read_bytes() == before


def test_repair_dry_run_writes_nothing(venv_bin):
    p = venv_bin / "memchorus-recall"
    _write_wrapper(p, f"{venv_bin}/{venv_bin}/python")
    before = p.read_bytes()

    results = shebang_repair.repair_entrypoints(bin_dir=venv_bin, dry_run=True)
    recall = next(r for r in results if r.name == "memchorus-recall")
    assert recall.action == "dry-run"
    assert p.read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX-only exec-bit semantics — Windows st_mode "
                           "carries no exec bits for regular files")
def test_repair_preserves_exec_bit(venv_bin):
    p = venv_bin / "memchorus-recall"
    _write_wrapper(p, f"{venv_bin}/{venv_bin}/python")
    shebang_repair.repair_entrypoints(bin_dir=venv_bin, dry_run=False)
    import os
    mode = os.stat(p).st_mode
    assert mode & 0o100 and mode & 0o010, f"exec bits lost: {oct(mode)}"


# ---------------------------------------------------------------------------
# --repair CLI exit codes
# ---------------------------------------------------------------------------

def test_repair_main_exit_0_when_repaired(venv_bin, capsys):
    p = venv_bin / "memchorus-recall"
    _write_wrapper(p, f"{venv_bin}/{venv_bin}/python")
    rc = doctor._repair_entrypoints_main(["--repair", str(venv_bin)], False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "memchorus-recall" in out and "repaired" in out


def test_repair_main_exit_1_when_unrepairable(venv_bin, capsys):
    p = venv_bin / "memchorus-recall"
    p.write_text("garbage not a launcher\n")
    p.chmod(0o755)
    rc = doctor._repair_entrypoints_main(["--repair", str(venv_bin)], False)
    assert rc == 1
    out = capsys.readouterr().out
    assert "not repairable" in out


def test_repair_main_exit_0_when_all_healthy(venv_bin, capsys):
    for name in ("memchorus-recall", "memchorus-doctor"):
        _write_wrapper(venv_bin / name, UV_EXPR)
    rc = doctor._repair_entrypoints_main(["--repair", str(venv_bin)], False)
    assert rc == 0
    capsys.readouterr()


# ---------------------------------------------------------------------------
# detection path (check_bin_entrypoints)
# ---------------------------------------------------------------------------

def test_detect_tags_gh306_and_hints_repair(venv_bin, monkeypatch):
    p = venv_bin / "memchorus-recall"
    _write_wrapper(p, f"{venv_bin}/{venv_bin}/python")
    monkeypatch.setattr(doctor, "_bin_search_dirs", lambda: [venv_bin])

    result = check_bin_entrypoints(strict=True)
    assert result.status == FAIL, result.message
    assert "GH #306" in result.message
    assert "--repair" in result.hint


def test_detect_passes_for_healthy_launchers(venv_bin, monkeypatch):
    for name in ("memchorus-recall", "memchorus-doctor"):
        _write_wrapper(venv_bin / name, UV_EXPR)
    monkeypatch.setattr(doctor, "_bin_search_dirs", lambda: [venv_bin])

    result = check_bin_entrypoints(strict=True)
    assert result.status == PASS, result.message
    assert "GH #306" not in result.message
