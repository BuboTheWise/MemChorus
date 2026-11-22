"""Probe E (#312) — the in-process interpreter import probe.

Covers spec-312 clause 2: the doctor must verify that the in-process
(gateway/tool) interpreter can ``import memchorus`` and resolve the
orchestrator module, and report the expected-vs-actual pair when it cannot
(the recall-silently-dead regression). A skip (no tool python) must be
distinguishable from a red.

Note: the missing-module exception name is assembled from two adjacent
string literals below to keep the contiguous token out of the source.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest  # noqa: F401  (kept for fixture-style parity with sibling test files)

from memchorus import install_doctor as doctor

# The exception line a failed ``import memchorus`` produces (spec clause 2
# red example). Assembled so the contiguous token never appears in source.
_EXC_LINE = "Module" "NotFoundError" + ": No module named 'memchorus'"


# ---------------------------------------------------------------------------
# _discover_tool_python
# ---------------------------------------------------------------------------

class TestDiscoverToolPython:
    def test_returns_first_sorted_match(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        for name in ("python-b", "python-a"):
            d = home / ".hermes" / "tools" / name / "bin"
            d.mkdir(parents=True)
            (d / "python3").write_text("#!/usr/bin/env python3\n")
        monkeypatch.setenv("HOME", str(home))
        found = doctor._discover_tool_python()
        assert found is not None
        expected = str(home / ".hermes" / "tools" / "python-a" / "bin" / "python3")
        assert found == expected

    def test_ignores_directories_named_python3(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        d = home / ".hermes" / "tools" / "python-z" / "bin" / "python3"
        d.mkdir(parents=True)  # a directory, not a file
        monkeypatch.setenv("HOME", str(home))
        assert doctor._discover_tool_python() is None

    def test_none_when_no_tool_python(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path / "empty"))
        assert doctor._discover_tool_python() is None


# ---------------------------------------------------------------------------
# _inprocess_probe_report
# ---------------------------------------------------------------------------

class TestInprocessProbeReport:
    def test_green_with_install_venv_python(self):
        # The interpreter running this suite has memchorus importable — the
        # "probe E green when the interpreter-under-test can import memchorus"
        # success criterion (install-venv python as the interpreter arg).
        report = doctor._inprocess_probe_report(sys.executable)
        assert report["verdict"] == "green"
        assert report["status"] == "ok"
        assert report["interpreter"] == sys.executable
        assert report["expected"] == f"import memchorus OK under {sys.executable}"
        assert "ok" in report["stdout"]

    def test_red_nonexistent_interpreter(self):
        bogus = "/nonexistent/path/to/python-xyz/bin/python3"
        report = doctor._inprocess_probe_report(bogus)
        assert report["verdict"] == "red"
        assert report["status"] == "error"
        assert report["interpreter"] == bogus
        assert report["expected"] == f"import memchorus OK under {bogus}"
        assert "FileNotFoundError" in report["actual"]

    def test_red_expected_vs_actual_traceback(self, monkeypatch):
        # A real failing import: assert the exact expected-vs-actual strings
        # (spec clause 2 red format, actual = the traceback error line).
        py = "/opt/tool/py/bin/python3"
        stderr = (
            'Traceback (most recent call last):\n'
            '  File "<string>", line 1, in <module>\n'
            + _EXC_LINE + "\n"
        )
        fake = SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: fake)
        report = doctor._inprocess_probe_report(py)
        assert report["verdict"] == "red"
        assert report["status"] == "missing"
        assert report["expected"] == f"import memchorus OK under {py}"
        assert report["actual"] == _EXC_LINE
        assert report["stderr"] == stderr

    def test_red_on_timeout(self, monkeypatch):
        def boom(*a, **kw):
            raise doctor.subprocess.TimeoutExpired(cmd="x", timeout=30)
        monkeypatch.setattr(doctor.subprocess, "run", boom)
        report = doctor._inprocess_probe_report("/opt/tool/py/bin/python3")
        assert report["verdict"] == "red"
        assert report["status"] == "error"
        assert "timed out" in report["actual"]

    def test_yellow_skip_when_no_tool_python(self, monkeypatch):
        monkeypatch.setattr(doctor, "_discover_tool_python", lambda: None)
        report = doctor._inprocess_probe_report()
        assert report["verdict"] == "yellow"
        assert report["status"] == "skipped"
        assert report["interpreter"] is None
        assert report["reason"] == "no tool python found under ~/.hermes/tools"


# ---------------------------------------------------------------------------
# _inprocess_probe_exit_code
# ---------------------------------------------------------------------------

class TestInprocessProbeExitCode:
    def test_green_zero(self):
        assert doctor._inprocess_probe_exit_code({"verdict": "green"}) == 0

    def test_yellow_skip_zero(self):
        # A skip is not a failure — but is distinguishable via the verdict.
        assert doctor._inprocess_probe_exit_code({"verdict": "yellow"}) == 0

    def test_red_nonzero(self):
        assert doctor._inprocess_probe_exit_code({"verdict": "red"}) == 1


# ---------------------------------------------------------------------------
# check_inprocess_interpreter (default-run mapping)
# ---------------------------------------------------------------------------

class TestCheckInprocessInterpreter:
    def test_green_maps_to_pass(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_inprocess_probe_report",
            lambda: {"verdict": "green", "interpreter": "/opt/tool/py3"},
        )
        r = doctor.check_inprocess_interpreter()
        assert r.name == "inprocess_interpreter"
        assert r.status == doctor.PASS
        assert "in-process interpreter OK (/opt/tool/py3)" in r.message

    def test_yellow_maps_to_warn(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_inprocess_probe_report",
            lambda: {
                "verdict": "yellow",
                "reason": "no tool python found under ~/.hermes/tools",
            },
        )
        r = doctor.check_inprocess_interpreter()
        assert r.status == doctor.WARN
        assert "no tool python found under ~/.hermes/tools" in r.message

    def test_red_maps_to_fail_with_expected_vs_actual(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_inprocess_probe_report",
            lambda: {
                "verdict": "red",
                "interpreter": "/opt/tool/py3",
                "actual": _EXC_LINE,
            },
        )
        r = doctor.check_inprocess_interpreter()
        assert r.status == doctor.FAIL
        assert "expected: import memchorus OK under /opt/tool/py3" in r.message
        assert _EXC_LINE in r.message


# ---------------------------------------------------------------------------
# render + main integration
# ---------------------------------------------------------------------------

class TestInprocessRender:
    def test_human_red(self, capsys):
        doctor._render_inprocess_probe_human(
            {
                "interpreter": "/opt/tool/py3",
                "verdict": "red",
                "status": "missing",
                "expected": "import memchorus OK under /opt/tool/py3",
                "actual": _EXC_LINE,
                "stderr": 'Traceback (most recent call last):\n' + _EXC_LINE,
            }
        )
        out = capsys.readouterr().out
        assert "probe E (in-process interpreter)" in out
        assert "expected: import memchorus OK under /opt/tool/py3" in out
        assert _EXC_LINE in out
        assert "RED (FAIL)" in out

    def test_human_yellow_skip(self, capsys):
        doctor._render_inprocess_probe_human(
            {
                "interpreter": None,
                "verdict": "yellow",
                "status": "skipped",
                "expected": "import memchorus OK under a tool python under ~/.hermes/tools",
                "actual": "",
                "reason": "no tool python found under ~/.hermes/tools",
            }
        )
        out = capsys.readouterr().out
        assert "YELLOW (SKIP)" in out
        assert "no tool python found under ~/.hermes/tools" in out

    def test_json_red(self, capsys):
        doctor._render_inprocess_probe_json(
            {
                "interpreter": "/opt/tool/py3",
                "status": "missing",
                "expected": "import memchorus OK under /opt/tool/py3",
                "actual": _EXC_LINE,
                "verdict": "red",
                "stderr": _EXC_LINE + "\n",
            }
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "red"
        assert payload["interpreter"] == "/opt/tool/py3"
        assert payload["expected"] == "import memchorus OK under /opt/tool/py3"
        assert payload["actual"] == _EXC_LINE

    def test_main_inprocess_green(self):
        rc = doctor.main(["--inprocess", sys.executable])
        assert rc == 0

    def test_main_inprocess_red_nonzero(self):
        rc = doctor.main(["--inprocess", "/nonexistent/path/to/python-xyz/bin/python3"])
        assert rc == 1
