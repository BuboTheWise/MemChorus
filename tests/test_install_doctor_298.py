"""GH-#298 — ``check_bin_entrypoints`` must resolve the bin dir against the
**base** Hermes home, not the profile-scoped ``hermes_home()``.

Under a *named* profile (``hermes -p <name>`` / the Kanban dispatcher)
``$HERMES_HOME`` is ``~/.hermes/profiles/<name>`` and ``hermes_home()``
therefore returns that profile directory. The advertised console-script
symlinks, however, always live in the **base** home's ``bin``
(``~/.hermes/bin``) — that is where ``pip`` / the installer drops them,
independent of which profile is active.

Before the fix the probe computed ``bin_dir = hermes_home() / "bin"``, so a
named-profile run looked in ``~/.hermes/profiles/<name>/bin`` (absent) and
emitted a permanent false warning *and* could not see a genuinely dangling
link in the real ``~/.hermes/bin`` (the #252 failure class). After the fix
the probe checks the base-home bin first (then the profile bin as a fallback)
and reports the true state.

RED on the pre-fix head (the named-profile tests hit the absent profile bin
and get the "no advertised bin entrypoints found" WARN); GREEN after the fix.

The synthetic trees are built under a path that contains **no** ``profiles``
/ ``profile`` segment in its ancestors. That matters: on this fleet pytest's
``tmp_path`` is nested under ``~/.hermes/profiles/cthugha/cache/scratch`` —
which itself contains a real ``profiles`` segment — so ``_base_home()`` would
strip at the *real* profile instead of our synthetic one and the test would
probe the wrong directory.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

import memchorus.hermes_home as hh
import memchorus.install_doctor as doctor
from memchorus.install_doctor import FAIL, PASS, WARN, check_bin_entrypoints


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------

def _clean_root() -> Path:
    """A writable root dir whose path has no ``profiles``/``profile`` segment.

    ``_base_home()`` returns the prefix up to (but excluding) the first such
    segment in ``$HERMES_HOME``. Building our synthetic tree under a clean root
    guarantees that the *only* ``profiles`` segment in the path is the one we
    create, so ``_base_home()`` resolves to *our* synthetic base.
    """
    def _clean(p: Path) -> bool:
        return not any(seg in ("profiles", "profile") for seg in p.parts)

    for cand in ("/tmp", "/var/tmp", tempfile.gettempdir()):
        p = Path(cand)
        if p.exists() and _clean(p):
            return p
    return Path("/tmp")


def _make_hermes_tree(request) -> tuple[Path, Path]:
    """Create ``<base>/bin`` + ``<base>/profiles/cthugha`` under a clean root.

    Returns ``(base, base_bin)``. Registers cleanup on the pytest request so
    the scratch tree is removed after the test.
    """
    scratch = Path(tempfile.mkdtemp(prefix="memchorus298_", dir=str(_clean_root())))
    request.addfinalizer(lambda: shutil.rmtree(scratch, ignore_errors=True))

    base = scratch / "hermes"
    profile = base / "profiles" / "cthugha"
    profile.mkdir(parents=True)
    base_bin = base / "bin"
    base_bin.mkdir(parents=True)
    return base, base_bin


@pytest.fixture
def named_profile_home(request, monkeypatch):
    """A scratch base home with a *named* profile active (the GH-#298 shape).

        <base>/bin/                    <- where advertised symlinks belong
        <base>/profiles/cthugha/       <- $HERMES_HOME (no bin/ subdir)

    Returns the base bin dir.
    """
    base, base_bin = _make_hermes_tree(request)
    profile = base / "profiles" / "cthugha"
    monkeypatch.setenv("HERMES_HOME", str(profile))

    # Resolvers must agree: hermes_home() is the profile dir; _base_home()
    # is the true root that owns ``bin``. The profile dir has no bin subdir —
    # exactly the condition that produced the false warning in GH-#298.
    assert doctor.hermes_home() == profile
    assert hh._base_home() == base
    assert not (profile / "bin").is_dir()
    return base_bin


@pytest.fixture
def default_profile_home(request, monkeypatch):
    """A scratch base home with the *default* profile active (base == profile).

    Returns the (single) bin dir.
    """
    base, base_bin = _make_hermes_tree(request)
    # For the default profile, HERMES_HOME *is* the base dir.
    monkeypatch.setenv("HERMES_HOME", str(base))
    assert doctor.hermes_home() == base
    assert hh._base_home() == base
    return base_bin


def _symlink(bin_dir: Path, name: str, target: str) -> Path:
    link = bin_dir / name
    link.symlink_to(target)
    return link


# ---------------------------------------------------------------------------
# AC — a healthy install under a NAMED profile must report PASS, not a false
#     "no advertised bin entrypoints found" WARN (the GH-#298 red herring).
# ---------------------------------------------------------------------------

def test_named_profile_healthy_base_bin_reports_pass(named_profile_home):
    """Symlinks live in the base bin; the profile has none -> must PASS.

    Pre-fix this returns WARN ("no advertised bin entrypoints found under
    <profile>/bin") because the probe never looks at the base bin. Post-fix
    it finds the base bin and reports PASS.
    """
    for name in (
        "memchorus-doctor",
        "memchorus-init",
        "memchorus-recall",
        "memchorus-recalibrate",
    ):
        # sys.executable is the repo test venv, which imports ``memchorus``.
        _symlink(named_profile_home, name, sys.executable)
    # mempalace-mcp intentionally absent -> not checked on this machine.

    result = check_bin_entrypoints(strict=False)

    assert result.status == PASS, (
        f"expected PASS on a healthy named-profile install, got "
        f"{result.status}: {result.message}"
    )
    # It must name the entries it actually verified in the base bin.
    assert "memchorus-doctor" in result.message


# ---------------------------------------------------------------------------
# AC — a *genuinely* dangling advertised link in the base bin must still be
#     detected under a named profile (GH-#298 consequence #2: the #252 failure
#     class was invisible to doctor when running under a named profile).
# ---------------------------------------------------------------------------

def test_named_profile_dangling_base_bin_link_is_flagged(named_profile_home):
    """A dangling base-bin link must surface even under a named profile.

    Pre-fix the probe points at the (absent) profile bin, so a real dangling
    link in the base bin is hidden behind a generic "no advertised ... found"
    WARN that never names the entrypoint. Post-fix it flags the specific
    entrypoint as "target missing".
    """
    _symlink(
        named_profile_home,
        "memchorus-doctor",
        str(named_profile_home / "gone" / "python3.14"),
    )

    normal = check_bin_entrypoints(strict=False)
    strict = check_bin_entrypoints(strict=True)

    # Both must name the specific broken entrypoint (a generic "no advertised"
    # WARN would not contain it).
    assert "memchorus-doctor" in normal.message
    assert "target missing" in normal.message
    # Status semantics from #252 (AC2): WARN in the normal report, FAIL strict.
    assert normal.status == WARN, f"normal should WARN, got {normal.status}"
    assert strict.status == FAIL, f"strict should FAIL, got {strict.status}"


# ---------------------------------------------------------------------------
# Regression guard — the DEFAULT-profile layout (base == profile) must be
# unchanged: healthy base bin still PASSes, empty bin still informative WARN.
# ---------------------------------------------------------------------------

def test_default_profile_healthy_still_passes(default_profile_home):
    """No named profile: base and profile bin are the same dir -> PASS."""
    for name in ("memchorus-doctor", "memchorus-init"):
        _symlink(default_profile_home, name, sys.executable)

    result = check_bin_entrypoints(strict=False)
    assert result.status == PASS


def test_default_profile_empty_bin_still_informative_warn(default_profile_home):
    """Empty bin on a fresh default-profile machine -> WARN, not FAIL."""
    result = check_bin_entrypoints(strict=False)
    assert result.status == WARN
    assert result.status is not FAIL
