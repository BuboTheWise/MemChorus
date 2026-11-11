"""Tests for :mod:`memchorus.state_migrate`.

Issue #283 scope items 1–3, 6: one-time migration of legacy state into the
profile home, preflight reporting of paths Hermes cannot preserve, and a
portable ``--palace`` value.

Hermetic: builds a fake ``$HOME`` with legacy state at the documented
locations (``~/.mempalace/<profile>``, ``~/.memchorus.yaml``,
``~/.memchorus-data/prohibitions.jsonl``) and points ``HERMES_HOME`` at a
subdirectory, so ``profile_home("default")`` resolves to ``HERMES_HOME``
and ``_canonical_in_home("default")`` is ``HERMES_HOME/mempalace``.
"""
from __future__ import annotations

import json
import os
import os.path
from pathlib import Path

import pytest

from memchorus import state_migrate as sm
from memchorus.state_migrate import (
    StateMigrationResult,
    legacy_sources,
    marker_path,
    migrate_profile_state,
    read_marker,
    write_marker,
)


def _pin_home(home: Path, monkeypatch) -> None:
    """Force Path.home() and os.path.expanduser to the fake home.

    On Windows these resolve via USERPROFILE, not $HOME, so setting env vars
    alone leaves the resolvers reading the runner's real home. This is
    harmless on Linux (already honored) and required for Windows hermeticity.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home), raising=False)
    monkeypatch.setattr(
        os.path,
        "expanduser",
        lambda p: (
            str(home / p[2:])
            if isinstance(p, str) and p.startswith("~/")
            else (str(home) if p == "~" else p)
        ),
        raising=False,
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _build_legacy_home(tmp_path):
    """Home with legacy state at every documented location."""
    home = tmp_path / "home"
    hh = home / ".hermes"
    (hh).mkdir(parents=True)

    # Legacy palace with real (non-empty) content.
    legacy_palace = home / ".mempalace" / "default"
    (legacy_palace / "palace").mkdir(parents=True)
    (legacy_palace / "palace" / "chroma.sqlite3").write_bytes(b"fake-sqlite")

    # Legacy global config.
    (home / ".memchorus.yaml").write_text("x: 1\n")

    # Legacy prohibitions.
    (home / ".memchorus-data").mkdir()
    (home / ".memchorus-data" / "prohibitions.jsonl").write_text("rule\n")

    return home, hh


@pytest.fixture
def legacy_home(tmp_path, monkeypatch):
    home, hh = _build_legacy_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hh))
    _pin_home(home, monkeypatch)
    return home


# ---------------------------------------------------------------------------
# legacy_sources
# ---------------------------------------------------------------------------

def test_legacy_sources_finds_all_existing(legacy_home):
    sources = legacy_sources("default")
    names = sorted(s.name for s in sources)
    assert "default" in names          # ~/.mempalace/default
    assert ".memchorus.yaml" in names
    assert "prohibitions.jsonl" in names


def test_legacy_sources_only_existing(legacy_home):
    # Remove one candidate — it must no longer appear.
    (legacy_home / ".memchorus.yaml").unlink()
    sources = legacy_sources("default")
    assert all(s.name != ".memchorus.yaml" for s in sources)


def test_legacy_sources_empty_when_no_state(tmp_path, monkeypatch):
    home = tmp_path / "empty_home"
    (home / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    assert legacy_sources("default") == []


def test_legacy_sources_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nope"))
    out = legacy_sources("default")
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# Marker round-trip
# ---------------------------------------------------------------------------

def test_marker_write_read_roundtrip(legacy_home):
    assert read_marker("default") is None
    p = write_marker("default", ["/a", "/b"])
    assert p.is_file()
    data = read_marker("default")
    assert data is not None
    assert data["migrated_from"] == ["/a", "/b"]
    assert "migrated_at" in data


def test_marker_read_corrupt_returns_none(legacy_home):
    p = write_marker("default", ["/a"])
    p.write_text("not-json{{")
    assert read_marker("default") is None


def test_marker_path_under_profile_data_dir(legacy_home):
    p = marker_path("default")
    assert "data/memchorus/state_migrated.json" in p.as_posix() or \
           "data\\memchorus\\state_migrated.json" in p.as_posix()


# ---------------------------------------------------------------------------
# migrate_profile_state — happy path
# ---------------------------------------------------------------------------

def test_migrate_moves_legacy_palace_into_home(legacy_home):
    res = migrate_profile_state("default")
    assert isinstance(res, StateMigrationResult)

    # The legacy palace is gone from its old location…
    legacy_palace = legacy_home / ".mempalace" / "default"
    assert not legacy_palace.exists()
    # …and the in-home canonical dir has the data.
    canonical = legacy_home / ".hermes" / "mempalace"
    assert canonical.exists()
    assert (canonical / "palace" / "chroma.sqlite3").exists()

    # Moved pairs recorded.  (Separator-agnostic: on Windows the legacy path
    # uses backslashes, so a literal "/.mempalace/default" never matches.)
    assert any(
        s[0].replace("\\", "/").endswith("/.mempalace/default")
        for s in res.moved
    )
    # Marker written.
    assert res.marker_written is not None
    assert Path(res.marker_written).is_file()


def test_migrate_moves_legacy_config_and_prohibitions(legacy_home):
    res = migrate_profile_state("default")
    # Legacy config → profile's memchorus.yaml
    assert (legacy_home / ".hermes" / "memchorus.yaml").exists()
    # Legacy prohibitions → data/memchorus/prohibitions.jsonl
    assert (legacy_home / ".hermes" / "data" / "memchorus" / "prohibitions.jsonl").exists()
    # Old locations gone.
    assert not (legacy_home / ".memchorus.yaml").exists()
    assert not (legacy_home / ".memchorus-data").exists()


# ---------------------------------------------------------------------------
# migrate_profile_state — idempotency
# ---------------------------------------------------------------------------

def test_migrate_idempotent_second_run_noop(legacy_home):
    first = migrate_profile_state("default")
    assert first.moved, "first run must have moved something"

    second = migrate_profile_state("default")
    # Second run: marker found, all sources gone → already_migrated.
    assert second.already_migrated is True
    assert second.moved == []
    assert second.preserved == []


def test_migrate_idempotent_preserves_existing_marker(legacy_home):
    first = migrate_profile_state("default")
    marker_after_first = read_marker("default")
    assert marker_after_first is not None

    second = migrate_profile_state("default")
    marker_after_second = read_marker("default")
    # Marker content is preserved (not clobbered by the no-op run).
    assert marker_after_second is not None
    assert marker_after_second["migrated_from"] == marker_after_first["migrated_from"]


# ---------------------------------------------------------------------------
# migrate_profile_state — dry run
# ---------------------------------------------------------------------------

def test_migrate_dry_run_moves_nothing(legacy_home):
    legacy_palace = legacy_home / ".mempalace" / "default"
    legacy_palace_bytes = (legacy_palace / "palace" / "chroma.sqlite3").read_bytes()

    res = migrate_profile_state("default", dry_run=True)

    # Source untouched.
    assert legacy_palace.exists()
    assert (legacy_palace / "palace" / "chroma.sqlite3").read_bytes() == legacy_palace_bytes
    # Legacy config and prohibitions untouched.
    assert (legacy_home / ".memchorus.yaml").exists()
    assert (legacy_home / ".memchorus-data" / "prohibitions.jsonl").exists()
    # No marker written.
    assert read_marker("default") is None
    # The plan is still reported.
    assert res.moved, "dry_run should still report the move plan"


# ---------------------------------------------------------------------------
# migrate_profile_state — destination already populated (preserve)
# ---------------------------------------------------------------------------

def test_migrate_preserves_source_when_dest_populated(legacy_home):
    # Pre-populate the in-home canonical dir with authoritative data.
    canonical = legacy_home / ".hermes" / "mempalace" / "existing"
    canonical.mkdir(parents=True)
    (canonical / "authoritative.sqlite3").write_bytes(b"keep-me")

    res = migrate_profile_state("default")

    # The legacy palace source was preserved as .bak, not clobbered.
    legacy_palace = legacy_home / ".mempalace" / "default"
    assert (legacy_home / ".mempalace" / "default.bak").exists()
    assert not legacy_palace.exists()
    # The authoritative destination is untouched.
    assert (canonical / "authoritative.sqlite3").read_bytes() == b"keep-me"
    assert res.preserved


# ---------------------------------------------------------------------------
# Config rewrite (mempalace_config.data_dir)
# ---------------------------------------------------------------------------

def test_migrate_rewrites_data_dir_to_canonical(legacy_home):
    # The legacy global config has a mempalace_config.data_dir pointing at the
    # legacy location — migration must rewrite it to the in-home canonical.
    legacy_cfg = legacy_home / ".memchorus.yaml"
    legacy_cfg.write_text(
        "mempalace_config:\n"
        "  data_dir: ~/.mempalace/default\n"
    )

    res = migrate_profile_state("default")

    # The config was moved to the profile home and rewritten.
    new_cfg = legacy_home / ".hermes" / "memchorus.yaml"
    assert new_cfg.exists()
    text = new_cfg.read_text()
    canonical = str(legacy_home / ".hermes" / "mempalace")
    # The old legacy location must be gone from the rewritten config.
    assert ".mempalace/default" not in text
    # The canonical path must now be present.
    assert canonical in text
    assert res.config_rewritten


# ---------------------------------------------------------------------------
# Never-raises
# ---------------------------------------------------------------------------

def test_migrate_never_raises_on_empty_home(tmp_path, monkeypatch):
    home = tmp_path / "empty"
    (home / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    res = migrate_profile_state("default")
    assert isinstance(res, StateMigrationResult)
    assert res.moved == []
    assert res.preserved == []


def test_migrate_result_is_frozen(legacy_home):
    res = migrate_profile_state("default")
    with pytest.raises(Exception):
        res.moved = []  # type: ignore[misc]
