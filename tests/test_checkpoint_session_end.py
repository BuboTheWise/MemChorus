"""Tests for the session-end auto-checkpoint (IMPL #208).

Covers:
* build_checkpoint_record() — produces a §2.3-valid project:<slug> payload
  (verified root + session gist + topics) that survives validate_project_record().
* write_checkpoint() — calls orchestrator.save() with the right key/value/category,
  degrades silently when orchestrator is None, name is empty, or save() is not callable.
* on_session_end() hook integration — the checkpoint block runs (calls save)
  before auto-tuning, and never interrupts teardown on failure.
"""
from typing import Any, Dict, List, Optional, Tuple

import pytest

from memchorus.checkpoint import (
    CHECKPOINT_SOURCE_PREFIX,
    build_checkpoint_record,
    _extract_topics,
    _gist_from_session,
    write_checkpoint,
)
from memchorus.project_record import validate_project_record


class FakeOrchestrator:
    def __init__(self, saved_key: Optional[str] = "project:acme") -> None:
        self.saved_key = saved_key
        self.calls: List[Tuple[str, Any, Optional[str], Optional[str], Optional[Dict[str, Any]]]] = []

    def save(self,
             key: str,
             value: Any,
             source_name: Optional[str] = None,
             category: Optional[str] = None,
             metadata: Optional[dict] = None) -> bool:
        self.calls.append((key, value, source_name, category, metadata))
        return bool(self.saved_key)


# --------------------------------------------------------------------------- #
# build_checkpoint_record                                                     #
# --------------------------------------------------------------------------- #

def test_build_checkpoint_record_produces_valid_location_and_standard() -> None:
    """A built record must survive §2.3 validation — the save gate is the truth."""
    key, value = build_checkpoint_record(
        project_name="Acme Repo",
        cwd="/home/stefan/Projects/Acme-Repro",
        user_texts=["please fix the checkpoint on session end for memchorus"],
    )
    # Key normalisation: case-folded slug (spec §3.1).
    assert key == "project:acme-repo"

    validate_project_record(value)

    # Location (verified, not rule-derived).
    loc = value["location"]
    assert loc["canonical_root"] == "/home/stefan/Projects/Acme-Repro"
    assert loc["source"].startswith(CHECKPOINT_SOURCE_PREFIX + "acme-repo")
    assert loc["verified_at"]  # non-null — we were actually working here

    # Standard (valid §2.2 pointer + session-derived gist/topics).
    std = value["standard"]
    assert std["skill"]  # non-empty slug
    assert std["doc_path"]
    assert "checkpoint" in std.get("gist", "").lower()   # session intent present
    assert isinstance(std.get("topics"), list) and std["topics"]


def test_build_checkpoint_record_empty_session_is_still_valid() -> None:
    """No user text → record still valid (default pointer, no gist/topics)."""
    _, value = build_checkpoint_record(project_name="Bare", cwd="/tmp/Bare", user_texts=[])
    validate_project_record(value)
    loc = value["location"]
    # Source is still the checkpoint marker (we did the work), even with no text.
    assert loc["source"].startswith(CHECKPOINT_SOURCE_PREFIX)
    assert loc["verified_at"] is not None

    std = value["standard"]
    # No gist/topics attached when there is no session content — the §2.4 default
    # pointer stays self-sufficient.
    assert not std.get("topics")
    assert not std.get("gist")


def test_build_checkpoint_record_requires_project_name() -> None:
    with pytest.raises(ValueError):
        build_checkpoint_record(project_name="", cwd="/tmp/x")


def test_cwd_is_used_canonical_root_and_source_is_verified() -> None:
    _, value = build_checkpoint_record(project_name="proj", cwd="/abs/path", user_texts=["hi"])
    validate_project_record(value)
    loc = value["location"]
    assert "/abs/path" == loc["canonical_root"]
    assert "checkpoint" in loc["source"].lower()


# --------------------------------------------------------------------------- #
# write_checkpoint                                                            #
# --------------------------------------------------------------------------- #

def test_write_checkpoint_persists_via_save_and_returns_key() -> None:
    orch = FakeOrchestrator(saved_key="project:acme")
    saved = write_checkpoint(orch, "Acme", cwd="/home/stefan/Projects/Acme",
                             user_texts=["work on checkpoint please"])
    assert saved == "project:acme"
    assert len(orch.calls) == 1
    key, value, source, category, metadata = orch.calls[0]
    assert key == "project:acme"
    assert source is None
    assert category == "SESSION"
    assert metadata and metadata.get("provenance")
    # The persisted value is valid and checkpoint-stamped.
    validate_project_record(value)
    assert value["location"]["canonical_root"] == "/home/stefan/Projects/Acme"
    assert value["location"]["source"].startswith(CHECKPOINT_SOURCE_PREFIX)


def test_write_checkpoint_none_when_orchestrator_none() -> None:
    assert write_checkpoint(None, "Acme", cwd="/tmp/Acme") is None


def test_write_checkpoint_none_when_project_name_empty() -> None:
    orch = FakeOrchestrator()
    assert write_checkpoint(orch, "", cwd="/tmp/Acme") is None
    assert orch.calls == []  # no save should have been attempted


def test_write_checkpoint_none_when_orchestrator_lacks_save() -> None:
    class NoSave:
        pass
    assert write_checkpoint(NoSave(), "Acme", cwd="/tmp/Acme") is None


def test_write_checkpoint_none_when_save_returns_false() -> None:
    orch = FakeOrchestrator(saved_key=None)
    assert write_checkpoint(orch, "Acme", cwd="/tmp/Acme") is None
    assert len(orch.calls) == 1  # save was called, but the source rejected it


# --------------------------------------------------------------------------- #
# summary heuristics                                                          #
# --------------------------------------------------------------------------- #

def test_gist_prefers_latest_user_message_and_caps_length() -> None:
    long_text = "checkpoint on session end for the memchorus repo " * 20
    capped = _gist_from_session(["old intent", long_text])
    assert len(capped) <= 120
    assert capped.endswith("\u2026")

    plain = _gist_from_session(["old intent", "write the checkpoint module now"])
    assert plain == "write the checkpoint module now"
    assert _gist_from_session([]) == ""


def test_extract_topics_ranks_by_frequency_and_filters_stopwords() -> None:
    text = ("checkpoint checkpoint checkpoint on session end "
            "module module work on the checkpoint")
    topics = _extract_topics(text, limit=4)
    assert topics  # non-empty
    assert "checkpoint" in topics
    # Stop-word-only text → no topics.
    assert _extract_topics("please and thanks for the help") == []


# --------------------------------------------------------------------------- #
# on_session_end integration                                                  #
# --------------------------------------------------------------------------- #

def _make_hooks():
    """Import the hooks module fresh so our monkeypatches are deterministic."""
    from memchorus import hooks
    return hooks


def test_on_session_end_runs_checkpoint_and_returns_key(monkeypatch, tmp_path) -> None:
    hooks = _make_hooks()
    # Bypass the real orchestrator/batcher machinery — we are testing the
    # checkpoint block specifically, not the auto-tuning.
    monkeypatch.setattr(hooks, "_CAPTURE_BATCHER", None, raising=False)
    orch = FakeOrchestrator(saved_key="project:acme")
    monkeypatch.setattr(hooks, "_get_orchestrator", lambda: orch, raising=False)

    # Project detection: env not set, so fall through to CWD basename.
    fake_proj = tmp_path / "acme"
    fake_proj.mkdir()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_WORKSPACE", raising=False)
    monkeypatch.chdir(fake_proj)

    result = hooks.on_session_end(
        conversation_history=[
            {"role": "user", "content": "please finish the checkpoint on session end"},
        ]
    )

    # Checkpoint must appear in the teardown result.
    assert result and result.get("checkpoint")
    # And save() must have carried a checkpoint-stamped location.
    assert orch.calls
    loc = orch.calls[0][1]["location"]
    assert loc["source"].startswith(CHECKPOINT_SOURCE_PREFIX)


def test_on_session_end_does_not_interrupt_tearing_down_on_failure(monkeypatch, tmp_path) -> None:
    """When the checkpoint module is unimportable (broken), teardown still completes."""
    hooks = _make_hooks()
    monkeypatch.setattr(hooks, "_CAPTURE_BATCHER", None, raising=False)

    def boom(*_a, **_k):  # noqa: ARG001
        raise RuntimeError("intentional")
    monkeypatch.setattr(hooks, "_get_orchestrator", boom, raising=False)

    # Should NOT propagate — teardown guard catches and completes.
    result = hooks.on_session_end(conversation_history=[])
    assert result and result.get("teardown") == "complete"
    # No checkpoint key in the failure path.
    assert "checkpoint" not in result
