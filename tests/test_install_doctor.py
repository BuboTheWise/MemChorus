"""Tests for memchorus.install_doctor -- diagnostic CLI module."""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, PropertyMock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
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
# check_python_version
# ---------------------------------------------------------------------------

class TestCheckPythonVersion:
    def test_pass_on_current(self):
        from memchorus.install_doctor import check_python_version, PASS
        r = check_python_version()
        assert r.status == PASS
        assert "meets minimum" in r.message

    def test_fail_below_minimum(self):
        from memchorus.install_doctor import check_python_version, FAIL

        with patch("sys.version_info", (3, 9, 0)):
            r = check_python_version()
            assert r.status == FAIL
            assert "too old" in r.message

    def test_name(self):
        from memchorus.install_doctor import check_python_version
        assert check_python_version().name == "python_version"


# ---------------------------------------------------------------------------
# check_dependency_integrity
# ---------------------------------------------------------------------------

class TestCheckDependencyIntegrity:
    def test_pass(self):
        from memchorus.install_doctor import (
            check_dependency_integrity, PASS, WARN
        )
        r = check_dependency_integrity()
        assert r.status in (PASS, WARN)  # pydantic might be absent in tests
        assert "dependency" in r.name

    def test_fail_when_pydantic_missing(self):
        from memchorus.install_doctor import check_dependency_integrity, FAIL
        with patch.dict("sys.modules", {"pydantic": None}):
            r = check_dependency_integrity()
            assert r.status == FAIL


# ---------------------------------------------------------------------------
# check_memory_source_registration
# ---------------------------------------------------------------------------

class TestCheckMemorySourceRegistration:
    def test_imports(self):
        from memchorus.install_doctor import (
            check_memory_source_registration, PASS, WARN
        )
        r = check_memory_source_registration()
        assert r.status in (PASS, WARN)

    def test_name(self):
        from memchorus.install_doctor import check_memory_source_registration
        assert check_memory_source_registration().name == "memory_source_registration"


# ---------------------------------------------------------------------------
# check_plugin_hooks
# ---------------------------------------------------------------------------

class TestCheckPluginHooks:
    def test_imports(self):
        from memchorus.install_doctor import check_plugin_hooks, PASS, WARN
        r = check_plugin_hooks()
        assert r.status in (PASS, WARN)

    def test_name(self):
        from memchorus.install_doctor import check_plugin_hooks
        assert check_plugin_hooks().name == "plugin_hook_state"


# ---------------------------------------------------------------------------
# check_config_validation
# ---------------------------------------------------------------------------

class TestCheckConfigValidation:
    def test_pass_when_valid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from memchorus.install_doctor import (
            check_config_validation, PASS, WARN
        )

        hermes_dir = tmp_path / ".hermes"
        if not hermes_dir.exists():
            hermes_dir.mkdir()
        cfg = hermes_dir / "config.yaml"
        if not cfg.exists():
            cfg.write_text("profile:\n  name: test\n")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        r = check_config_validation()
        assert r.status in (PASS, WARN)

    def test_warn_when_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from memchorus.install_doctor import check_config_validation, WARN

        empty = tmp_path / "empty"
        empty.mkdir(exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(empty))

        r = check_config_validation()
        assert r.status == WARN

    def test_name(self):
        from memchorus.install_doctor import check_config_validation
        assert check_config_validation().name == "config_validation"


# ---------------------------------------------------------------------------
# check_auto_tune_pipeline
# ---------------------------------------------------------------------------

class TestCheckAutoTunePipeline:
    def test_all_present(self):
        from memchorus.install_doctor import check_auto_tune_pipeline, PASS
        r = check_auto_tune_pipeline()
        assert r.status == PASS

    def test_name(self):
        from memchorus.install_doctor import check_auto_tune_pipeline
        assert check_auto_tune_pipeline().name == "auto_tune_pipeline"


# ---------------------------------------------------------------------------
# check_data_directory
# ---------------------------------------------------------------------------

class TestCheckDataDirectory:
    def test_pass_when_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from memchorus.install_doctor import check_data_directory, PASS

        fake_mp = tmp_path / ".mempalace"
        fake_mp.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        r = check_data_directory()
        assert r.status == PASS

    def test_fail_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from memchorus.install_doctor import check_data_directory, FAIL

        empty = tmp_path / "empty"
        empty.mkdir(exist_ok=True)
        monkeypatch.setattr(Path, "home", lambda: empty)

        r = check_data_directory()
        assert r.status == FAIL

    def test_name(self):
        from memchorus.install_doctor import check_data_directory
        assert check_data_directory().name == "data_directory"


# ---------------------------------------------------------------------------
# check_test_suite
# ---------------------------------------------------------------------------

class TestCheckTestSuite:
    def test_returns_result(self):
        from memchorus.install_doctor import check_test_suite, PASS, WARN
        r = check_test_suite()
        assert r.status in (PASS, WARN)

    def test_name(self):
        from memchorus.install_doctor import check_test_suite
        assert check_test_suite().name == "test_suite"


# ---------------------------------------------------------------------------
# run_checks
# ---------------------------------------------------------------------------

class TestRunChecks:
    def test_returns_8_results(self):
        from memchorus.install_doctor import run_checks
        results = run_checks()
        assert len(results) == 8

    def test_all_have_name_and_status(self):
        from memchorus.install_doctor import run_checks
        for r in run_checks():
            assert isinstance(r.name, str) and r.name
            assert r.status in ("PASS", "WARN", "FAIL")


# ---------------------------------------------------------------------------
# print_report
# ---------------------------------------------------------------------------

class TestPrintReport:
    def test_captures_output(self, capsys):
        from memchorus.install_doctor import (
            print_report, CheckResult, PASS
        )
        results = [CheckResult(name="test_check", status=PASS, message="ok")]
        print_report(results)
        out = capsys.readouterr()
        assert "MemChorus Install Doctor" in out.out
        assert "1 passed" in out.out

    def test_includes_hints(self, capsys):
        from memchorus.install_doctor import (
            print_report, CheckResult, WARN
        )
        results = [
            CheckResult(name="hinty", status=WARN, message="warn", hint="fix this")
        ]
        print_report(results)
        out = capsys.readouterr()
        assert "fix this" in out.out

    def test_summary_counts(self, capsys):
        from memchorus.install_doctor import (
            print_report, CheckResult
        )
        results = [
            CheckResult(name="a", status="PASS", message="ok"),
            CheckResult(name="b", status="FAIL", message="bad"),
            CheckResult(name="c", status="WARN", message="hmm"),
        ]
        print_report(results)
        out = capsys.readouterr()
        assert "1 passed" in out.out
        assert "1 failed" in out.out


# ---------------------------------------------------------------------------
# main  -- CLI entry point
# ---------------------------------------------------------------------------

class TestMain:
    def test_returns_zero_or_one(self):
        from memchorus.install_doctor import main
        code = main()
        assert code in (0, 1)

    def test_callable_as_module(self):
        result = subprocess.run(
            [sys.executable, "-m", "memchorus.install_doctor"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode in (0, 1), f"exit {result.returncode}: {result.stderr}"
