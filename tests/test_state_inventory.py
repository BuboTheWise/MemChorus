"""Tests for :mod:`memchorus.state_inventory`.

Issue #283 — single source of truth for per-profile state.

These tests are hermetic: they build a fake ``$HOME`` tree under ``tmp_path``
and monkeypatch ``HOME`` + ``HERMES_HOME`` so the inventory/migrate resolvers
see a controlled filesystem. The test-suite conftest already pins a private
test home, but each test re-pins to ``tmp_path`` so we get full control over
what exists and what the inventory reports.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from memchorus import state_inventory as si
from memchorus.state_inventory import (
    StateCategory,
    StateEntry,
    backup_paths_for_profile,
    expand_portable_path,
    in_home_outside_hermes_entries,
    inventory_for_profile,
    outside_home_entries,
)


# ---------------------------------------------------------------------------
# Fixture: deterministic fake home tree
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Build a fake $HOME tree and point the resolvers at it.

    Layout:

    home/
      .hermes/                       <- HERMES_HOME (default-profile base)
        memchorus.yaml               <- profile config (exists)
        auth.json                    <- secret (should be excluded)
        .env                         <- secret (should be excluded)
        data/memchorus/prohibitions.jsonl
        data/memchorus/_tuning/      <- tuning dir (exists)
        memories/                    <- memory dir (exists)
          _hit_rate_index.json
      .cache/memchorus_feedback_corrections.json   <- in-home/outside-hermes
      .memchorus.yaml                <- legacy global config (exists)
      .mempalace/default/palace/chroma.sqlite3     <- palace (exists)

    The home/HERMES_HOME relationship (``home/.hermes``) matches the
    real default-profile layout, so ``backup_paths_for_profile`` can be
    tested both positive (paths under home but outside HERMES_HOME) and
    negative (paths under HERMES_HOME, or secret paths).
    """
    home = tmp_path / "home"
    hh = home / ".hermes"

    # In-home, inside HERMES_HOME (excluded from backup_paths)
    (hh / "data" / "memchorus").mkdir(parents=True)
    (hh / "data" / "memchorus" / "prohibitions.jsonl").write_text("rule\n")
    (hh / "data" / "memchorus" / "_tuning").mkdir()
    (hh / "memories").mkdir()
    (hh / "memories" / "_hit_rate_index.json").write_text("{}\n")
    (hh / "memchorus.yaml").write_text("data_dir: ~/.mempalace/default\n")
    (hh / ".env").write_text("API_KEY=x\n")
    (hh / "auth.json").write_text("{}\n")

    # In-home, OUTSIDE HERMES_HOME (included in backup_paths)
    (home / ".cache").mkdir()
    (home / ".cache" / "memchorus_feedback_corrections.json").write_text("{}\n")

    # Legacy global config (exists, in-home, outside hermes)
    (home / ".memchorus.yaml").write_text("x: 1\n")

    # Palace (exists, in-home, outside hermes)
    (home / ".mempalace" / "default" / "palace").mkdir(parents=True)
    (home / ".mempalace" / "default" / "palace" / "chroma.sqlite3").write_bytes(b"")

    import os
    import os.path

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hh))

    # Windows: Path.home() and os.path.expanduser("~") resolve via
    # USERPROFILE, not $HOME, so the fake home above would be invisible to
    # them and the resolvers would read the runner's real home. Pin both to
    # the tmp home so the suite is hermetic on every platform.
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

    return home


# ---------------------------------------------------------------------------
# inventory_for_profile
# ---------------------------------------------------------------------------

def test_inventory_returns_all_state_entries(fake_home):
    entries = inventory_for_profile("default")
    assert isinstance(entries, list)
    assert len(entries) >= 8  # at least the 8 core entries

    by_name = {e.name: e for e in entries}

    # Every expected state entry is present, with the right category.
    assert "profile-config" in by_name
    assert by_name["profile-config"].category == StateCategory.CONFIG
    assert not by_name["profile-config"].is_dir

    assert "prohibitions" in by_name
    assert by_name["prohibitions"].category == StateCategory.PROHIBITIONS

    assert "memory-storage" in by_name
    assert by_name["memory-storage"].category == StateCategory.MEMORY
    assert by_name["memory-storage"].is_dir

    assert "hit-rate-index" in by_name
    assert by_name["hit-rate-index"].category == StateCategory.HIT_RATE

    assert "tuning-state" in by_name
    assert by_name["tuning-state"].category == StateCategory.TUNING
    assert by_name["tuning-state"].is_dir

    assert "feedback-corrections" in by_name
    assert by_name["feedback-corrections"].category == StateCategory.FEEDBACK

    assert "legacy-global-config" in by_name
    assert by_name["legacy-global-config"].category == StateCategory.CONFIG

    assert "mempalace-palace" in by_name
    assert by_name["mempalace-palace"].category == StateCategory.PALACE
    assert by_name["mempalace-palace"].is_dir


def test_inventory_marks_secrets(fake_home):
    entries = inventory_for_profile("default")
    by_name = {e.name: e for e in entries}

    assert by_name["credentials"].contains_secrets is True
    assert by_name["auth-json"].contains_secrets is True

    # Non-secret entries must not be flagged.
    assert by_name["profile-config"].contains_secrets is False
    assert by_name["memory-storage"].contains_secrets is False
    assert by_name["prohibitions"].contains_secrets is False


def test_inventory_never_raises_on_bad_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nope"))
    out = inventory_for_profile("nonexistent-profile")
    # Never raises: returns a list (possibly empty).
    assert isinstance(out, list)


def test_state_entry_is_frozen(fake_home):
    entry = StateEntry(
        name="x", path=Path("/tmp/x"), category=StateCategory.CONFIG
    )
    with pytest.raises(Exception):
        entry.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# backup_paths_for_profile — the core #283 contract
# ---------------------------------------------------------------------------

def test_backup_paths_includes_in_home_outside_hermes(fake_home):
    bp = backup_paths_for_profile("default")
    # The in-home/outside-hermes paths must appear:
    assert any(
        "memchorus_feedback_corrections.json" in p for p in bp
    ), f"feedback-corrections missing from backup_paths: {bp}"
    assert any(
        p.endswith(".memchorus.yaml") or "/.memchorus.yaml" in p for p in bp
    ), f"legacy-global-config missing from backup_paths: {bp}"


def test_backup_paths_excludes_secrets(fake_home):
    bp = backup_paths_for_profile("default")
    for p in bp:
        assert ".env" not in p.split("/")[-1] or not p.endswith(".env")
        assert not p.endswith("auth.json"), f"secret leaked: {p}"


def test_backup_paths_excludes_inside_hermes_home(fake_home):
    bp = backup_paths_for_profile("default")
    hh = (fake_home / ".hermes").resolve()
    for p in bp:
        resolved = Path(p).resolve()
        try:
            resolved.relative_to(hh)
            raise AssertionError(f"path inside HERMES_HOME leaked: {p}")
        except ValueError:
            pass  # correctly outside HERMES_HOME


def test_backup_paths_only_existing_paths(fake_home):
    # Remove a path the inventory would declare, then it must not appear.
    target = fake_home / ".memchorus.yaml"
    target.unlink()
    bp = backup_paths_for_profile("default")
    for p in bp:
        assert not p.endswith(".memchorus.yaml"), f"missing path leaked: {p}"


def test_backup_paths_deduplicates_parent_child(fake_home):
    """If both a directory and a file inside it are declared, the broadest
    (the directory) wins and the child is dropped."""
    bp = backup_paths_for_profile("default")
    resolved = [Path(p).resolve() for p in bp]
    for a in resolved:
        for b in resolved:
            if a == b:
                continue
            # If a contains b, b should have been dropped.
            try:
                b.relative_to(a)
                raise AssertionError(
                    f"child {b} not deduplicated against parent {a}"
                )
            except ValueError:
                pass


def test_backup_paths_returns_strings(fake_home):
    bp = backup_paths_for_profile("default")
    assert all(isinstance(p, str) for p in bp)


def test_backup_paths_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "missing-hermes"))
    out = backup_paths_for_profile("default")
    assert isinstance(out, list)
    assert all(isinstance(p, str) for p in out)


# ---------------------------------------------------------------------------
# outside_home_entries / in_home_outside_hermes_entries
# ---------------------------------------------------------------------------

def test_outside_home_entries_reports_non_home_paths(fake_home, tmp_path, monkeypatch):
    # Make the HERMES_HOME a *sibling* of HOME so that the palace, which we
    # point at a location outside HOME, gets reported.
    outer = tmp_path / "outside"
    (outer / "palace").mkdir(parents=True)
    (outer / "palace" / "chroma.sqlite3").write_bytes(b"")
    # Rewrite the profile config to point data_dir at the outer location.
    cfg = fake_home / ".hermes" / "memchorus.yaml"
    cfg.write_text(f"data_dir: {outer}/palace\n")

    out = outside_home_entries("default")
    names = [e.name for e in out]
    assert "mempalace-palace" in names, f"expected palace outside-home, got {names}"


def test_in_home_outside_hermes_entries_matches_backup_paths(fake_home):
    entries = in_home_outside_hermes_entries("default")
    bp = backup_paths_for_profile("default")
    # The entry set and the backup_paths set should agree on which paths.
    entry_paths = {str(e.path.resolve()) for e in entries}
    bp_paths = {str(Path(p).resolve()) for p in bp}
    assert entry_paths == bp_paths, f"divergence: entries={entry_paths} bp={bp_paths}"


def test_in_home_outside_hermes_entries_is_empty_when_all_inside_hermes(
    tmp_path, monkeypatch
):
    # Build a home where EVERY state path falls inside HERMES_HOME.
    home = tmp_path / "h2"
    hh = home / ".hermes"
    (hh / "memories").mkdir(parents=True)
    (hh / "data" / "memchorus").mkdir(parents=True)
    (hh / "data" / "memchorus" / "_tuning").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hh))
    entries = in_home_outside_hermes_entries("default")
    assert entries == []


# ---------------------------------------------------------------------------
# expand_portable_path (item 4)
# ---------------------------------------------------------------------------

def test_expand_portable_path_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("MC_TEST_VAR", "/var/test")
    assert expand_portable_path("${MC_TEST_VAR}/data") == "/var/test/data"


def test_expand_portable_path_hermes_env_syntax(tmp_path, monkeypatch):
    monkeypatch.setenv("MC_TEST_VAR2", "/opt/x")
    assert expand_portable_path("${env:MC_TEST_VAR2}/y") == "/opt/x/y"


def test_expand_portable_path_tilde(tmp_path, monkeypatch):
    home = tmp_path / "h"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Pin the resolvers so the fake home is honored on every platform
    # (Windows resolves ~ via USERPROFILE, not $HOME).
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home), raising=False)
    monkeypatch.setattr(
        os.path, "expanduser",
        lambda p: str(home / p[2:]) if isinstance(p, str) and p.startswith("~/") else p,
        raising=False,
    )
    out = expand_portable_path("~/data")
    assert Path(out) == home / "data"


def test_expand_portable_path_returns_input_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # An unresolvable ${VAR} is returned by expandvars as-is (POSIX) — no
    # exception, input preserved.
    out = expand_portable_path("${UNSET_MC_VAR_XYZ}")
    assert isinstance(out, str)
    assert len(out) > 0
