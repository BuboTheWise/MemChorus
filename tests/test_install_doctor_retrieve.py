"""Acceptance tests for the ``memchorus-doctor --retrieve`` probe — **probe D**,
issue #255 card 3/3 (Kanban t_858fea41).

Probe D asserts **both** the exit code **and** the response body of
``memchorus-recall retrieve <key>``, matching the real retrieve contract:

  * exit 0  = resolved   (a real body must be present on stdout)
  * exit 1  = miss       (hint on stderr, no body)
  * exit 2  = unreachable (hint on stderr, no body)

Before card 2/3 the probe encoded the stale "expect exit 2" expectation, so it
could not distinguish a resolve from a miss. Now it must go **GREEN** on a
resolvable key and a **clear RED** on a miss — a miss must *fail* the probe,
not pass it — and the rendered report shows the expected-vs-actual exit code.

The subprocess is faked so the tests are deterministic and do not depend on the
version of ``memchorus-recall`` installed in the test venv.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import memchorus.install_doctor as doctor
from memchorus.install_doctor import main


def _fake_run(rc: int, stdout: str = "", stderr: str = ""):
    """Return a stand-in for ``subprocess.run`` with a controlled result."""

    def _run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)

    return _run


@pytest.fixture(autouse=True)
def _fake_binary(monkeypatch):
    """Point probe D at a fixed (fake) binary path so PATH resolution is
    irrelevant and the subprocess result is fully controlled per test."""
    monkeypatch.setattr(doctor, "_locate_memchorus_recall", lambda: "/fake/memchorus-recall")
    yield


# ---------------------------------------------------------------------------
# (a) GREEN — exit 0 AND a real body on stdout
# ---------------------------------------------------------------------------

def test_probe_d_green_on_resolve(capsys, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run",
                        _fake_run(0, stdout='{"text": "real body"}'))
    code = main(["--retrieve", "learning-resolvable"])
    assert code == 0
    out = capsys.readouterr().out
    assert "GREEN (PASS)" in out
    # Both expected and actual show exit 0.
    assert "expected: exit 0" in out
    assert "actual:   exit 0" in out
    # The body assertion fired and passed.
    assert "body present on stdout" in out


def test_probe_d_green_json_schema(capsys, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run",
                        _fake_run(0, stdout='{"text": "real body"}'))
    code = main(["--retrieve", "learning-resolvable", "--json"])
    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "resolved"
    assert doc["actual_rc"] == 0
    assert doc["expected_rc"] == 0
    assert doc["body_present"] is True
    assert doc["verdict"] == "green"


# ---------------------------------------------------------------------------
# (b) RED — exit 1 (miss): hint on stderr, no body. Must FAIL the probe.
# ---------------------------------------------------------------------------

def test_probe_d_red_on_miss(capsys, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run",
                        _fake_run(1, stdout="",
                                  stderr="Key not found: 'no-such-key'\n"
                                        "cold cache or never saved — re-fetch the live source"))
    code = main(["--retrieve", "no-such-key"])
    assert code == 1  # a miss must fail the probe, not pass it
    out = capsys.readouterr().out
    assert "RED (FAIL)" in out
    assert "expected: exit 0" in out
    assert "actual:   exit 1" in out
    assert "no real body on stdout" in out


def test_probe_d_red_on_unreachable(capsys, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run",
                        _fake_run(2, stdout="", stderr="cannot reach mempalace (MCP unavailable)"))
    code = main(["--retrieve", "k"])
    # Doctor-level: any non-resolve is a failed probe → exit 1.
    assert code == 1
    out = capsys.readouterr().out
    assert "RED (FAIL)" in out
    assert "actual:   exit 2" in out


# ---------------------------------------------------------------------------
# (c) The critical regression — exit code alone is NOT enough: the probe must
#     also require a real body. exit 0 with an empty body is still a miss.
# ---------------------------------------------------------------------------

def test_probe_d_exit_0_without_body_is_a_miss(capsys, monkeypatch):
    """Locks the 'assert BOTH' contract: the stale probe only checked the code.
    A resolve must carry a real body; exit 0 alone must not pass."""
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run(0, stdout="", stderr=""))
    code = main(["--retrieve", "k"])
    assert code == 1
    out = capsys.readouterr().out
    assert "RED (FAIL)" in out
    assert "actual:   exit 0" in out  # code was a resolve, but...
    assert "no real body on stdout" in out  # ...no body → still a miss


# ---------------------------------------------------------------------------
# (d) usage / error paths
# ---------------------------------------------------------------------------

def test_probe_d_missing_key_is_usage_error(capsys):
    code = main(["--retrieve"])
    assert code == 2
    assert "--retrieve requires a key" in capsys.readouterr().out


def test_probe_d_subprocess_error_is_red(capsys, monkeypatch):
    def _boom(cmd, **kw):
        raise FileNotFoundError("memchorus-recall not found")

    monkeypatch.setattr(doctor.subprocess, "run", _boom)
    code = main(["--retrieve", "k"])
    assert code == 1
    out = capsys.readouterr().out
    assert "RED (FAIL)" in out
    assert "retrieve stderr" in out
    assert "FileNotFoundError" in out


def test_probe_d_json_schema_on_miss(capsys, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run",
                        _fake_run(1, stdout="", stderr="Key not found: 'k'"))
    code = main(["--retrieve", "k", "--json"])
    assert code == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "miss"
    assert doc["actual_rc"] == 1
    assert doc["body_present"] is False
    assert doc["verdict"] == "red"
