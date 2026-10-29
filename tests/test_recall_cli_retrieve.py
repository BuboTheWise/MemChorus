"""IMPL #253 (D1) — tests for the ``memchorus-recall retrieve`` subcommand.

Spec: ``docs/spec-253-recall-resolver.md``.  Two layers, mirroring the spec's
acceptance language ("run the CLI subcommand via subprocess", exit codes 0/1/2,
body-to-stdout vs hint-to-stderr):

1. **In-process glue tests** — fast unit tests of ``run_retrieve`` + the
   ``RETRIEVE_MISS``→``None`` folding boundary, using a duck-typed fake
   orchestrator.  These pin the contract without spawning a process.

2. **Subprocess acceptance tests** — spawn a child process that builds a *real*
   ``MemoryOrchestrator`` and calls ``recall_cli.main(["retrieve", key, …])``,
   then assert the exit code + stdout/stderr split.  This is the runtime proof
   the spec asks for (see ``_recall_cli_retrieve_child.py``).

The in-process tests and the subprocess tests are deliberately independent: a
regression in the CLI glue shows up as a fast red in layer 1; a regression in the
real orchestrator wiring shows up in layer 2.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import memchorus
from memchorus import recall_cli

try:
    from memchorus.mempalace_memory_source import RETRIEVE_MISS
    _HAS_SENTINEL = True
except Exception:  # pragma: no cover - source module should always be present
    RETRIEVE_MISS = None  # type: ignore[assignment]
    _HAS_SENTINEL = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class FakeOrchestrator:
    """Duck-typed stand-in for ``MemoryOrchestrator``.

    ``store`` maps key → body; a key that is absent (or mapped to ``None``) is
    a retrieve-miss.  ``with_source`` optionally switches on the provenance
    form so ``--json`` takes the ``retrieve_with_source`` path.
    """

    def __init__(self, store, with_source=False, source_name="stub-profile"):
        self._store = dict(store)
        self._with_source = with_source
        self._source_name = source_name

    def retrieve(self, key):
        return self._store.get(key)

    def retrieve_with_source(self, key):
        body = self._store.get(key)
        if body is None:
            return None
        return {"key": key, "content": body, "source_name": self._source_name}


@pytest.fixture
def _patch_orch(monkeypatch):
    """Return a factory that installs a FakeOrchestrator as the active one.

    ``run_retrieve`` does ``from memchorus import get_orchestrator`` at call
    time, so patching the package attribute is the seam.
    """
    def _install(store, with_source=False):
        fake = FakeOrchestrator(store, with_source)
        monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **k: fake)
        return fake
    return _install


@pytest.fixture
def _no_orch(monkeypatch):
    monkeypatch.setattr(memchorus, "get_orchestrator", lambda *a, **k: None)


def _run(args, capsys):
    code = recall_cli.main(args)
    out = capsys.readouterr()
    return code, out.out, out.err


# --------------------------------------------------------------------------- #
# Spec §4.1 — exit codes / stdout-stderr split (in-process glue)
# --------------------------------------------------------------------------- #

def test_run_retrieve_hit_prints_body_and_returns_0(_patch_orch, capsys):
    _patch_orch({"decision-abc123def4567890": "the resolved decision body"})
    code, out, err = _run(["retrieve", "decision-abc123def4567890"], capsys)
    assert code == 0, f"expected exit 0, got {code}; stderr={err!r}"
    # Plain-text mode prints the stored payload verbatim, with no repr
    # wrapping and nothing else on stdout.
    assert out.strip() == "the resolved decision body"
    assert err == ""


def test_run_retrieve_hit_dict_body_rendered_as_json(_patch_orch, capsys):
    body = {"wing": "decisions", "note": "structured body"}
    _patch_orch({"decision-dict1234567890123": body})
    code, out, err = _run(["retrieve", "decision-dict1234567890123"], capsys)
    assert code == 0
    parsed = json.loads(out)
    assert parsed == body
    assert err == ""


def test_run_retrieve_miss_returns_1_with_hint(_patch_orch, capsys):
    # Store is empty for the key under test → cold miss.
    _patch_orch({"some-other-key": "irrelevant"})
    code, out, err = _run(["retrieve", "decision-never1234567890"], capsys)
    assert code == 1, f"expected exit 1 (miss), got {code}"
    assert "never saved" in err or "cold cache" in err
    assert "not found" in err


def test_run_retrieve_empty_key_returns_2(_patch_orch):
    _patch_orch({"k": "v"})
    code = recall_cli.main(["retrieve", "   "])
    assert code == 2


def test_run_retrieve_no_orchestrator_returns_2(_no_orch, capsys):
    code, out, err = _run(["retrieve", "decision-anywhere123456"], capsys)
    assert code == 2
    assert "no MemoryOrchestrator" in err or "not registered" in err


def test_run_retrieve_json_hit_prefers_provenance(_patch_orch, capsys):
    _patch_orch(
        {"decision-js0000000000000001": "plain body text"},
        with_source=True,
    )
    code, out, err = _run(
        ["retrieve", "decision-js0000000000000001", "--json"], capsys
    )
    assert code == 0
    parsed = json.loads(out)
    # The provenance form is emitted verbatim — source_name is the piece a
    # machine reader (jq) needs.
    assert parsed["key"] == "decision-js0000000000000001"
    assert parsed["content"] == "plain body text"
    assert parsed["source_name"] == "stub-profile"


def test_run_retrieve_json_miss_returns_1(_patch_orch, capsys):
    _patch_orch({"x": "y"}, with_source=True)
    code, out, err = _run(
        ["retrieve", "decision-miss0000000001", "--json"], capsys
    )
    assert code == 1
    assert "not found" in err


# --------------------------------------------------------------------------- #
# §4.4/#221 — RETRIEVE_MISS sentinel folds to None at the CLI boundary
# --------------------------------------------------------------------------- #

def test_unwrap_retrieve_result_folds_sentinel_to_none():
    if not _HAS_SENTINEL:
        pytest.skip("RETRIEVE_MISS sentinel not importable in this build")
    assert recall_cli._unwrap_retrieve_result(None) is None
    assert recall_cli._unwrap_retrieve_result(RETRIEVE_MISS) is None


def test_unwrap_retrieve_result_passes_values_through():
    sentinel = "not-a-sentinel"
    assert recall_cli._unwrap_retrieve_result(sentinel) == sentinel
    assert recall_cli._unwrap_retrieve_result({"a": 1}) == {"a": 1}


# --------------------------------------------------------------------------- #
# §4.1 — subprocess acceptance (the spec's runtime proof)
# --------------------------------------------------------------------------- #

# The child driver lives beside this file.  It builds a real MemoryOrchestrator
# and calls recall_cli.main() in a fresh interpreter, so the CLI sees genuine
# process boundaries (no shared registry state leaking in from the test session).
_TESTS_DIR = Path(__file__).resolve().parent
_CHILD_DRIVER = _TESTS_DIR / "_recall_cli_retrieve_child.py"
# The child script must import the same ``memchorus`` as the tree we're
# testing.  conftest.py puts ``src/`` on sys.path; surface it to the child too
# so an editable install elsewhere can't shadow it.
_SRC_PARENT = _TESTS_DIR.parent
_REPO_SRC = _SRC_PARENT / "src"


def _child_env():
    env = dict(os.environ)
    # Point the child at THIS worktree's src so it exercises the worktree code
    # (the repo's .venv has an editable install that may point at another tree).
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{_REPO_SRC}{os.pathsep}{existing}" if existing else str(_REPO_SRC)
    )
    return env


def _spawn_child(args, timeout=60):
    return subprocess.run(
        [sys.executable, str(_CHILD_DRIVER)] + args,
        cwd=str(_SRC_PARENT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_subprocess_retrieve_hit_exit_0_body_on_stdout():
    res = _spawn_child(
        ["decision-sub0000000000001", "hit"]
    )
    assert res.returncode == 0, (
        f"expected exit 0, got {res.returncode}\n"
        f"stdout={res.stdout!r}\nstderr={res.stderr!r}"
    )
    assert "decision-sub0000000000001" in res.stdout
    assert "resolved-body:decision-sub0000000000001" in res.stdout
    # A clean hit must not leak the miss-hint onto stderr.
    assert "cold cache" not in res.stderr


def test_subprocess_retrieve_hit_json_exit_0():
    res = _spawn_child(["decision-subj0000000001", "hit", "--json"])
    assert res.returncode == 0, (
        f"expected exit 0, got {res.returncode}\n"
        f"stdout={res.stdout!r}\nstderr={res.stderr!r}"
    )
    # --json mode must yield a machine-parseable payload on stdout.
    payload = json.loads(res.stdout)
    assert payload.get("key") == "decision-subj0000000001"


def test_subprocess_retrieve_miss_exit_1_hint_on_stderr():
    res = _spawn_child(["decision-submiss00000001", "miss"])
    assert res.returncode == 1, (
        f"expected exit 1 (miss), got {res.returncode}\n"
        f"stdout={res.stdout!r}\nstderr={res.stderr!r}"
    )
    assert "not found" in res.stderr
    # The actionability hint from C13b is what makes the miss actionable.
    assert "cold cache" in res.stderr or "re-fetch" in res.stderr


def test_subprocess_retrieve_no_orchestrator_exit_2():
    res = _spawn_child(["decision-subno000000001", "noorche"])
    assert res.returncode == 2, (
        f"expected exit 2 (no orchestrator), got {res.returncode}\n"
        f"stdout={res.stdout!r}\nstderr={res.stderr!r}"
    )
    assert "MemoryOrchestrator" in res.stderr
