"""Tests for IMPL #163.4 — Recall Location Standard.

Covers the two new operator-facing surfaces:

1. ``memchorus-recall project <name>`` (``recall_cli.py``) — the ``project``
   subcommand: help, graceful no-data degrade (exit 1, stderr message, no
   traceback), JSON output, and the §3.3 render contract (location / standard
   / reconciled as distinct labeled blocks).

2. ``memchorus-doctor --project <name>`` + the default
   ``check_project_record_resolution`` check (``install_doctor.py``) — the
   focused diagnostic and the install-health gate: ok / no_data /
   no_orchestrator / error branches, exit codes, and the §3.3 render.

Design-spec cross-refs (Bubo_Wisdom …/MemChorus-Recall-LocationStandard-
Design-Spec.md): §3.1 resolver contract, §3.3 return shape, §4.5 base-class
no-op degrade, §4.7 doctor surface, §0 OPSEC glossary.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

import pytest

import memchorus
from memchorus.orchestrator import MemoryOrchestrator
from memchorus import install_doctor as doctor
from memchorus import recall_cli


# ---------------------------------------------------------------------------
# Fixtures — valid §3.3 channels (mirrors test_project_record_schema.py)
# ---------------------------------------------------------------------------

VALID_LOCATION = {
    "canonical_root": "<workspace>/Code/MemChorus/",
    "source": "ssot:ORGANIZATION.md#memchorus",
    "verified_at": None,   # null ⇒ derived-by-rule / unverified (§2.4)
}

VALID_STANDARD = {
    "skill": "development-process",
    "doc_path": "stable/development-process/SKILL.md",
    "gist": "decompose-first, IMPL → REVIEW → RELEASE",
    "topics": ["branching", "merge", "review"],
}


def _make_orchestrator(tmp_path):
    """A MemoryOrchestrator backed by a fresh hermes_default dir."""
    hermes_dir = os.path.join(str(tmp_path), "hermes_mem")
    config = {
        "default_source": "hermes_default",
        "hermes_default_config": {"memory_dir": hermes_dir},
        "mempalace_config": {},
    }
    return MemoryOrchestrator(config)


@pytest.fixture
def orch(tmp_path):
    return _make_orchestrator(tmp_path)


# A deterministic fake source for recall_cli: exposes the two APIs.
class FakeRecallSource:
    def __init__(self, record: Optional[Dict[str, Any]]):
        self._record = record

    def recall_kg(self, entity, hops=1, limit=10, relations=None):
        return []

    def resolve_project_record(self, project_name):
        return self._record


class FakeOrchestrator:
    """Just enough surface for _resolve_source / doctor to use."""

    def __init__(self, record: Optional[Dict[str, Any]]):
        self._record = record

    def recall_kg(self, entity, hops=1, limit=10, relations=None):
        return []

    def resolve_project_record(self, project_name):
        return self._record


@pytest.fixture
def _registry_cleanup(monkeypatch):
    """Pin get_orchestrator to a fake and restore afterwards."""
    monkeypatch.setitem(sys.modules, "memchorus", memchorus)

    def _apply(record):
        fake = FakeOrchestrator(record)
        monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: fake)
        # recall_cli imports get_orchestrator by name at call-site via
        # `from memchorus import get_orchestrator`; patch the module attr too.
        recall_cli.get_orchestrator = fake  # type: ignore[attr-defined]
        return fake

    def _remove():
        # recall_cli does a local import inside _resolve_source, so there is no
        # module attribute to clean; patching memchorus.get_orchestrator is
        # what matters. Nothing else to undo (monkeypatch handles it).
        pass

    return _apply


# ---------------------------------------------------------------------------
# 1. recall_cli — `project <name>` subcommand
# ---------------------------------------------------------------------------

def test_cli_help_lists_project_subcommand(capsys):
    with pytest.raises(SystemExit):
        recall_cli.main(["--help"])
    out = capsys.readouterr().out
    assert "project" in out
    assert "kg" in out


def test_cli_project_missing_name_is_usage_error(monkeypatch, capsys):
    # No resolver anywhere → main returns 1 (source found but no API) OR 2
    # if argparse rejects.  `project` with no name → argparse SystemExit(2).
    with pytest.raises(SystemExit) as exc:
        recall_cli.main(["project"])
    assert exc.value.code == 2


def test_cli_project_resolves_record(monkeypatch, capsys):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [],
    }
    fake = FakeOrchestrator(record)
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: fake)

    rc = recall_cli.main(["project", "memchorus"])
    assert rc == 0
    out = capsys.readouterr().out
    # §3.3 distinct labeled blocks:
    assert "location:" in out
    assert "standard:" in out
    assert "reconciled:" in out
    # location points at the canonical root, source is the SSoT row:
    assert "<workspace>/Code/MemChorus/" in out
    assert "ssot:ORGANIZATION.md#memchorus" in out
    # standard skill pointer is present:
    assert "development-process" in out


def test_cli_project_json_output(monkeypatch, capsys):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [
            {"role": "scratch", "path": "<workspace>/tmp/scratch",
             "relation": "scratch-alias"},
        ],
    }
    fake = FakeOrchestrator(record)
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: fake)

    rc = recall_cli.main(["project", "memchorus", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["location"] == VALID_LOCATION
    assert parsed["standard"] == VALID_STANDARD
    assert parsed["reconciled"][0]["relation"] == "scratch-alias"


def test_cli_project_no_data_degrades_to_exit_1(monkeypatch, capsys):
    # Orchestrator present but resolve_project_record returns None → base
    # no-op degrade (§4.5) → "no data" on stderr, exit 1, no traceback.
    class NoneOrch:
        def recall_kg(self, *a, **kw):
            return []

        def resolve_project_record(self, name):
            return None

    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: NoneOrch())
    rc = recall_cli.main(["project", "memchorus"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no data" in err.lower() or "resolve_project_record" in err
    # No traceback leaked:
    assert "Traceback" not in err


def test_cli_project_resolver_raises_degrades_to_exit_1(monkeypatch, capsys):
    class RaisingOrch:
        def recall_kg(self, *a, **kw):
            return []

        def resolve_project_record(self, name):
            raise RuntimeError("boom")

    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: RaisingOrch())
    rc = recall_cli.main(["project", "memchorus"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "boom" in err
    assert "Traceback" not in err


def test_cli_project_accepts_prefixed_key(monkeypatch, capsys):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [],
    }
    captured = {}

    class PrefixOrch:
        def recall_kg(self, *a, **kw):
            return []

        def resolve_project_record(self, name):
            captured["name"] = name
            return record

    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: PrefixOrch())
    rc = recall_cli.main(["project", "project:memchorus"])
    assert rc == 0
    # The CLI passes the name through verbatim — the resolver normalizes.
    assert captured["name"] == "project:memchorus"


# ---------------------------------------------------------------------------
# 2. install_doctor — check_project_record_resolution (install-health gate)
# ---------------------------------------------------------------------------

def test_doctor_check_passes_when_pipeline_present():
    res = doctor.check_project_record_resolution()
    assert res.status == doctor.PASS
    assert res.name == "project_record_resolution"


def test_doctor_check_uses_known_constants():
    # The check must only emit recognized statuses.
    res = doctor.check_project_record_resolution()
    assert res.status in {doctor.PASS, doctor.WARN, doctor.FAIL}


# ---------------------------------------------------------------------------
# 3. install_doctor — _project_record_report + renderers
# ---------------------------------------------------------------------------

def test_doctor_report_ok_path(monkeypatch):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [],
    }
    fake = FakeOrchestrator(record)
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: fake)

    rep = doctor._project_record_report("memchorus")
    assert rep["status"] == "ok"
    assert rep["project"] == "memchorus"
    assert rep["record"] is record
    assert rep["reason"] is None


def test_doctor_report_no_orchestrator(monkeypatch):
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: None)
    rep = doctor._project_record_report("memchorus")
    assert rep["status"] == "no_orchestrator"
    assert rep["record"] is None
    assert rep["reason"]


def test_doctor_report_no_data(monkeypatch):
    class NoneOrch:
        def resolve_project_record(self, name):
            return None

    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: NoneOrch())
    rep = doctor._project_record_report("memchorus")
    assert rep["status"] == "no_data"
    assert rep["record"] is None


def test_doctor_report_error_is_caught(monkeypatch):
    class RaisingOrch:
        def resolve_project_record(self, name):
            raise ValueError("bad key")

    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: RaisingOrch())
    rep = doctor._project_record_report("memchorus")
    assert rep["status"] == "error"
    assert "bad key" in rep["reason"]
    assert rep["record"] is None


def test_doctor_report_missing_resolver(monkeypatch):
    class NoApiOrch:
        pass  # no resolve_project_record

    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: NoApiOrch())
    rep = doctor._project_record_report("memchorus")
    assert rep["status"] == "no_orchestrator"
    assert "resolve_project_record" in rep["reason"] or "upgrade" in rep["reason"]


def test_doctor_exit_code_mapping(monkeypatch):
    ok = {"status": "ok"}
    nod = {"status": "no_data"}
    noo = {"status": "no_orchestrator"}
    err = {"status": "error"}
    assert doctor._project_exit_code(ok) == 0
    assert doctor._project_exit_code(nod) == 1
    assert doctor._project_exit_code(noo) == 1
    assert doctor._project_exit_code(err) == 1


def test_doctor_render_project_human(capsys, monkeypatch):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [
            {"role": "scratch", "path": "<workspace>/tmp/scratch",
             "relation": "scratch-alias"},
        ],
    }
    report = {
        "project": "memchorus",
        "status": "ok",
        "reason": None,
        "record": record,
    }
    doctor._render_project_report_human(report)
    out = capsys.readouterr().out
    assert "project record: memchorus  [ok]" in out
    assert "location:" in out
    assert "standard:" in out
    assert "reconciled: 1 scratch path(s):" in out
    # OPSEC: no home paths, no agent names.
    assert "/home/" not in out
    assert "cthugha" not in out.lower()
    assert "bubo" not in out.lower()


def test_doctor_render_project_json(capsys):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [],
    }
    report = {
        "project": "memchorus",
        "status": "ok",
        "reason": None,
        "record": record,
    }
    doctor._render_project_report_json(report)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["project"] == "memchorus"
    assert parsed["status"] == "ok"
    assert parsed["record"]["location"] == VALID_LOCATION


def test_doctor_render_project_no_data(capsys):
    report = {
        "project": "memchorus",
        "status": "no_data",
        "reason": "resolve_project_record returned None",
        "record": None,
    }
    doctor._render_project_report_human(report)
    out = capsys.readouterr().out
    assert "project record: memchorus  [no_data]" in out
    assert "resolve_project_record returned None" in out


# ---------------------------------------------------------------------------
# 4. install_doctor — main() --project wiring
# ---------------------------------------------------------------------------

def test_doctor_main_project_ok(monkeypatch, capsys):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [],
    }
    fake = FakeOrchestrator(record)
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: fake)

    rc = doctor.main(["--project", "memchorus"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "location:" in out


def test_doctor_main_project_json(monkeypatch, capsys):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [],
    }
    fake = FakeOrchestrator(record)
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: fake)

    rc = doctor.main(["--project", "memchorus", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["status"] == "ok"


def test_doctor_main_project_missing_name(monkeypatch, capsys):
    rc = doctor.main(["--project"])
    assert rc == 2
    # Usage errors print to stdout — consistent with --recall in the same file.
    out = capsys.readouterr().out
    assert "requires a project name" in out or "--project" in out


def test_doctor_main_project_no_orchestrator(monkeypatch, capsys):
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: None)
    rc = doctor.main(["--project", "memchorus"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "project record:" in out
    assert "no_orchestrator" in out


# ---------------------------------------------------------------------------
# 5. run_checks registry includes the new check
# ---------------------------------------------------------------------------

def test_run_checks_includes_project_record_resolution(monkeypatch):
    # Pin all other checks to PASS so we can assert the new one is present.
    monkeypatch.setattr(doctor, "check_python_version", lambda: _pass("python"))
    results = doctor.run_checks()
    names = [r.name for r in results]
    assert "project_record_resolution" in names


def _pass(name):
    return doctor.CheckResult(name=name, status=doctor.PASS, message="ok")


# ---------------------------------------------------------------------------
# 6. OPSEC — no home paths / agent names in new user-facing strings
# ---------------------------------------------------------------------------

def test_opsec_placeholders_in_rendered_output(monkeypatch, capsys):
    record = {
        "location": VALID_LOCATION,
        "standard": VALID_STANDARD,
        "reconciled": [],
    }
    fake = FakeOrchestrator(record)
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **kw: fake)

    recall_cli.main(["project", "memchorus"])
    out = capsys.readouterr().out
    assert "/home/" not in out
    assert "cthugha" not in out.lower()
    assert "bubo" not in out.lower()
    # The canonical_root is data (from the record), not our string; it uses a
    # <workspace> placeholder in this fixture:
    assert "<workspace>/Code/MemChorus/" in out
