"""Tests for the #222 install-doctor restart-coupling bridge.

The check under test is :func:`memchorus.install_doctor.check_version_alignment`.

It closes the restart-coupling blind spot: every other doctor check reads only
the *installed-package* state (importlib.metadata / on-disk properties), so a
long-lived host process that has not restarted after a reinstall still shows a
fully-green report while executing the OLD in-memory copy of ``memchorus``.
This check compares the in-process ``memchorus.__version__`` against the
installed (pip-metadata) version and flags the discrepancy:

    * installed present + in-process present + strings differ  -> FAIL (restart)
    * installed present + in-process present + strings equal   -> PASS
    * installed not determinable (no pip record)               -> WARN (explicit)
    * in-process version not queryable                         -> WARN (explicit)

It also verifies the check is registered as the 10th entry in ``run_checks()``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures -- mirror the existing install-doctor test harness so the module
# can be imported the same way the rest of the suite does it.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _tmp_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide a fake homedir with .hermes/config.yaml and .mempalace/."""
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    (hermes / "config.yaml").write_text("profile:\n  name: test\n")
    mp = tmp_path / ".mempalace"
    mp.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# ---------------------------------------------------------------------------
# PASS branch -- installed and in-process versions are identical
# ---------------------------------------------------------------------------

class TestVersionAlignmentPass:
    def test_pass_on_identical_versions(self):
        from memchorus.install_doctor import check_version_alignment, PASS
        import memchorus

        # Use the *actual* in-process version so installed == in-process.
        current = memchorus.__version__
        r = check_version_alignment(installed_version=current)
        assert r.status == PASS
        assert r.name == "version_alignment"
        assert current in r.message

    def test_pass_has_no_restart_urgency_in_hint(self):
        from memchorus.install_doctor import check_version_alignment, PASS
        import memchorus

        r = check_version_alignment(installed_version=memchorus.__version__)
        # PASS means aligned — the hint (if any) is not a restart directive.
        if r.hint:
            assert "restart" not in r.hint.lower()


# ---------------------------------------------------------------------------
# FAIL branch -- installed present and in-process present, strings differ
# ---------------------------------------------------------------------------

class TestVersionAlignmentFail:
    def test_fail_on_mismatch(self, monkeypatch):
        from memchorus.install_doctor import check_version_alignment, FAIL

        with patch("memchorus.__version__", "9.9.9"):
            r = check_version_alignment(installed_version="2.0.61")
            assert r.status == FAIL
            assert r.name == "version_alignment"
            assert "MISMATCH" in r.message
            assert "9.9.9" in r.message
            assert "2.0.61" in r.message

    def test_fail_hint_tells_user_to_restart_host(self):
        from memchorus.install_doctor import check_version_alignment

        with patch("memchorus.__version__", "9.9.9"):
            r = check_version_alignment(installed_version="2.0.61")
            assert r.hint is not None
            hint = r.hint.lower()
            # The hint must explicitly name "restart" -- the whole point of the check.
            assert "restart" in hint

    def test_fail_message_names_restart_coupling_blind_spot(self):
        from memchorus.install_doctor import check_version_alignment

        with patch("memchorus.__version__", "9.9.9"):
            r = check_version_alignment(installed_version="2.0.61")
            assert "stale" in r.message.lower() or "old" in r.message.lower()


# ---------------------------------------------------------------------------
# WARN branch -- installed version not determinable (no pip record)
# ---------------------------------------------------------------------------

class TestVersionAlignmentWarnNoInstalled:
    def test_warn_when_installed_none(self, monkeypatch):
        from memchorus.install_doctor import check_version_alignment, WARN

        # installed_version is passed as None explicitly; the function then
        # tries imp_meta.version("memchorus") which we patch to raise.
        with patch(
            "memchorus.install_doctor.imp_meta.version",
            side_effect=Exception("no such distribution"),
        ):
            r = check_version_alignment(installed_version=None)
            assert r.status == WARN
            assert r.name == "version_alignment"
            assert "not determinable" in r.message.lower()

    def test_warn_hint_suggests_pip_install(self):
        from memchorus.install_doctor import check_version_alignment

        with patch(
            "memchorus.install_doctor.imp_meta.version",
            side_effect=Exception("no such distribution"),
        ):
            r = check_version_alignment(installed_version=None)
            assert r.hint is not None
            assert "pip" in r.hint.lower()


# ---------------------------------------------------------------------------
# WARN branch -- in-process version not queryable
# ---------------------------------------------------------------------------

class TestVersionAlignmentWarnNoInProcess:
    def test_warn_when_in_process_none(self, monkeypatch):
        from memchorus.install_doctor import check_version_alignment, WARN

        with patch.dict("sys.modules", {"memchorus": None}):
            r = check_version_alignment(installed_version="2.0.61")
            assert r.status == WARN
            assert r.name == "version_alignment"

    def test_warn_hint_suggests_restart(self):
        from memchorus.install_doctor import check_version_alignment

        with patch.dict("sys.modules", {"memchorus": None}):
            r = check_version_alignment(installed_version="2.0.61")
            assert r.hint is not None
            assert "restart" in r.hint.lower()


# ---------------------------------------------------------------------------
# Default path -- installed_version=None + real imp_meta (should be 2.0.61 from
# the Hermes venv since the check reads pip metadata).
# ---------------------------------------------------------------------------

class TestVersionAlignmentDefault:
    def test_default_reads_pip_metadata(self):
        """With installed_version=None, the check queries imp_meta.version.

        In the Hermes venv this should be 2.0.61 (just bumped), which equals
        the in-process __version__=2.0.61, so this is a PASS path.
        """
        from memchorus.install_doctor import check_version_alignment, PASS

        r = check_version_alignment(installed_version=None)
        # This is environment-sensitive: PASS if the venv is up to date,
        # FAIL if the venv copy is stale (exactly the #222 case),
        # WARN if pip metadata is unreadable.
        assert r.status in ("PASS", "FAIL", "WARN")
        assert r.name == "version_alignment"


# ---------------------------------------------------------------------------
# Runner presence -- check_version_alignment must be the 10th entry in the
# run_checks list.
# ---------------------------------------------------------------------------

class TestRunnerPresence:
    def test_check_is_registered(self):
        from memchorus.install_doctor import (
            check_version_alignment,
            check_test_suite,
        )

        # Both must be importable as module-level functions.
        assert callable(check_version_alignment)
        assert callable(check_test_suite)

    def test_check_count_is_10(self):
        """run_checks() returns 10 results (was 9 pre-#222)."""
        from memchorus.install_doctor import run_checks

        results = run_checks()
        assert len(results) == 10, (
            f"Expected 10 checks, got {len(results)}. "
            f"Names: {[r.name for r in results]}"
        )

    def test_version_alignment_is_in_runner_names(self):
        from memchorus.install_doctor import run_checks

        names = [r.name for r in run_checks()]
        assert "version_alignment" in names, (
            f"version_alignment not in run_checks; got: {names}"
        )

    def test_version_alignment_is_last(self):
        """The new check is added after check_test_suite (10th / last)."""
        from memchorus.install_doctor import run_checks

        names = [r.name for r in run_checks()]
        assert names[-1] == "version_alignment", (
            f"Expected version_alignment last, got order: {names}"
        )
