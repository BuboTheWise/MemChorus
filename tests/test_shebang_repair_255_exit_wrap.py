"""#255 (2/3) — exit-code contract: the launcher must propagate ``main()``'s rc.

Regression (v2.0.80): the Hermes install pipeline writes MemChorus console
scripts whose final line calls the entry function **bare** — ``main(sys.argv[1:])``
with no ``sys.exit`` — so ``main()``'s 0/1/2 return value is discarded and every
invoke exits 0, including a genuine miss (rc=1) and an unreachable pipeline
(rc=2).  That is the *rc=0-for-miss* regression.

``memchorus.shebang_repair`` now detects that shape and wraps the single entry
invocation in ``sys.exit(...)``, restoring the 0/1/2 contract (spec §4.1).  These
tests lock that in:

* **unit**   — ``_exit_wrap_repair`` / ``_imported_entry_names`` on synthetic bodies;
* **file**   — ``ensure_exit_wrapped`` rewrites the entry call, is idempotent, and
               preserves the shebang + all other bytes;
* **e2e**    — a launcher whose ``main()`` returns 2 exits **0** before the repair
               (the regression) and **2** after, through the real interpreter — the
               regression proof;
* **driver** — ``repair_entrypoints`` flags the broken launcher for repair, reports
               the distinct #255 message, and leaves an already-wrapped launcher
               byte-identical.

All launchers are synthetic files under a clean temp root (never ``~/.hermes``);
the bin dir is passed explicitly, so no real install / profile / symlink tree is
touched.  (Same conventions as ``test_install_doctor_306.py``.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from memchorus import shebang_repair
from memchorus.shebang_repair import (
    _exit_wrap_repair,
    _imported_entry_names,
    ensure_exit_wrapped,
    repair_entrypoints,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def bin_root() -> Path:
    """A scratch ``root/venv/bin`` with a real ``python`` (symlink to the
    interpreter running the test); ``root`` also hosts the fake CLI module."""
    root = Path(tempfile.mkdtemp(prefix="mc255_"))
    bin_dir = root / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    return root


def _interp(bin_root: Path) -> str:
    return str(bin_root / "venv" / "bin" / "python")


def _write_fake_module(root: Path) -> None:
    """``mc_fake_cli.main`` returns 2 — the "pipeline unreachable" rc."""
    (root / "mc_fake_cli.py").write_text(
        "def main(argv=None):\n    return 2\n"
    )


def _write_broken(path: Path, interp: str) -> None:
    """Hermes-pipeline shape: bare entry call, no ``sys.exit``."""
    path.write_text(
        f"#!{interp}\n"
        "import sys\n"
        "from mc_fake_cli import main\n"
        "main(sys.argv[1:])\n"
    )
    path.chmod(0o755)


def _write_wrapped(path: Path, interp: str) -> None:
    """Healthy shape: entry call already inside ``sys.exit(...)``."""
    path.write_text(
        f"#!{interp}\n"
        "import sys\n"
        "from mc_fake_cli import main\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    path.chmod(0o755)


def _run_launcher(path: Path, root: Path):
    env = dict(os.environ, PYTHONPATH=str(root))
    return subprocess.run(
        [str(path)], capture_output=True, text=True, env=env, timeout=60
    )


# ---------------------------------------------------------------------------
# unit — name collection + wrap transform
# ---------------------------------------------------------------------------

def test_imported_entry_names_collects_entry_only():
    body = "import sys\nfrom mc_fake_cli import main\nmain(sys.argv[1:])\n"
    names = _imported_entry_names(body)
    assert "main" in names
    # a bare `import sys` is not an entry import and must not leak a name
    assert "sys" not in names


def test_imported_entry_names_skips_future():
    body = "from __future__ import annotations\nfrom mc_fake_cli import main\n"
    names = _imported_entry_names(body)
    assert "main" in names
    assert "annotations" not in names


def test_exit_wrap_repair_wraps_bare_entry_call():
    body = "import sys\nfrom mc_fake_cli import main\nmain(sys.argv[1:])\n"
    out = _exit_wrap_repair(body)
    assert out is not None
    assert "sys.exit(main(sys.argv[1:]))" in out
    # non-entry lines preserved, in order
    assert out.startswith("import sys\n")
    assert "from mc_fake_cli import main" in out


def test_exit_wrap_repair_is_idempotent_on_wrapped():
    wrapped = (
        "import sys\n"
        "from mc_fake_cli import main\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    assert _exit_wrap_repair(wrapped) is None


def test_exit_wrap_repair_no_entry_import_is_none():
    body = "import sys\nprint('hello')\nsys.exit(0)\n"
    assert _exit_wrap_repair(body) is None


# ---------------------------------------------------------------------------
# file-level — ensure_exit_wrapped
# ---------------------------------------------------------------------------

def test_ensure_exit_wrapped_rewrites_then_idempotent(bin_root):
    bin_dir = bin_root / "venv" / "bin"
    interp = _interp(bin_root)
    p = bin_dir / "memchorus-recall"
    _write_broken(p, interp)

    assert ensure_exit_wrapped(p) is True          # first call rewrites
    text = p.read_text()
    assert "sys.exit(main(sys.argv[1:]))" in text
    # shebang preserved
    assert text.startswith(f"#!{interp}\n")

    assert ensure_exit_wrapped(p) is False          # second call: no change
    assert "sys.exit(main(sys.argv[1:]))" in p.read_text()


def test_ensure_exit_wrapped_leaves_wrapped_untouched(bin_root):
    bin_dir = bin_root / "venv" / "bin"
    interp = _interp(bin_root)
    p = bin_dir / "memchorus-doctor"
    _write_wrapped(p, interp)
    before = p.read_bytes()
    assert ensure_exit_wrapped(p) is False
    assert p.read_bytes() == before


# ---------------------------------------------------------------------------
# e2e — THE regression proof
# ---------------------------------------------------------------------------

def test_launcher_propagates_rc_after_repair(bin_root):
    root = bin_root
    bin_dir = root / "venv" / "bin"
    interp = _interp(root)
    _write_fake_module(root)
    p = bin_dir / "memchorus-recall"
    _write_broken(p, interp)

    # BEFORE the repair: main() returns 2, but the bare call discards it,
    # so the process exits 0 — exactly the v2.0.80 rc=0-for-miss regression.
    before = _run_launcher(p, root)
    assert before.returncode == 0, (
        f"expected the regression (rc=0) before repair, "
        f"got {before.returncode}: {before.stderr!r}"
    )

    # AFTER the repair: sys.exit(main()) propagates main()'s rc=2.
    assert ensure_exit_wrapped(p) is True
    after = _run_launcher(p, root)
    assert after.returncode == 2, (
        f"expected rc=2 after repair (contract), "
        f"got {after.returncode}: {after.stderr!r}"
    )


# ---------------------------------------------------------------------------
# driver — repair_entrypoints
# ---------------------------------------------------------------------------

def test_repair_entrypoints_flags_broken_and_leaves_wrapped(bin_root):
    root = bin_root
    bin_dir = root / "venv" / "bin"
    interp = _interp(root)
    _write_fake_module(root)
    broken = bin_dir / "memchorus-recall"
    wrapped = bin_dir / "memchorus-doctor"
    _write_broken(broken, interp)
    _write_wrapped(wrapped, interp)
    wrapped_before = wrapped.read_bytes()

    results = repair_entrypoints(bin_dir=bin_dir, dry_run=False)
    by_name = {r.name: r for r in results}

    # broken launcher is repaired; entry call now wrapped
    assert by_name["memchorus-recall"].action == "repaired"
    assert "sys.exit(main(sys.argv[1:]))" in broken.read_text()
    # distinct #255 message, not the #306 interpreter-repoint form
    assert "sys.exit" in by_name["memchorus-recall"].describe()

    # already-wrapped launcher left byte-identical
    assert by_name["memchorus-doctor"].action == "ok"
    assert wrapped.read_bytes() == wrapped_before


def test_repair_entrypoints_dry_run_writes_nothing(bin_root):
    root = bin_root
    bin_dir = root / "venv" / "bin"
    interp = _interp(root)
    _write_fake_module(root)
    p = bin_dir / "memchorus-recall"
    _write_broken(p, interp)
    before = p.read_bytes()

    results = repair_entrypoints(bin_dir=bin_dir, dry_run=True)
    by_name = {r.name: r for r in results}
    assert by_name["memchorus-recall"].action == "dry-run"
    assert p.read_bytes() == before   # no write on dry run
