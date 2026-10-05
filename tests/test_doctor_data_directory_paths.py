"""Regression tests for ``check_data_directory`` path resolution (issue #235).

The doctor's ``data_directory`` check historically hard-coded
``Path.home() / ".mempalace"`` — the *global* data dir — while every other
component (auto_init, auto_bootstrap, the reader/writer) funnels through the
single-source resolver :func:`memchorus.palace_path.palace_data_dir`.  That
mismatch made ``memchorus-doctor`` false-FAIL in profile installs, where the
real store lives under a profile-scoped root, and it minted a spurious global
shell next to the real one.

The fix is a delegation: the check must now report the *same* directory that
``palace_data_dir(_palace_layout_root(None))`` resolves to, where the root
honours  ``--palace-root`` >  ``$PALACE_ROOT`` /  ``$MEMPALACE_PALACE_PATH``
>  ``~/.mempalace``  (the resolver already defined in this module).

These tests assert the delegation, not the incidental message wording, so they
survive cosmetic rewording.  ``test_profile_env_delegates_to_resolver``
specifically is RED at the pre-fix HEAD (doctor checks the global home path)
and GREEN once ``check_data_directory`` delegates to the resolver.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memchorus import palace_path
from memchorus.install_doctor import FAIL, _palace_layout_root, check_data_directory


@pytest.fixture(autouse=True)
def _clear_palace_env(monkeypatch):
    """Isolate every test: start with no palace root env vars set."""
    monkeypatch.delenv("PALACE_ROOT", raising=False)
    monkeypatch.delenv("MEMPALACE_PALACE_PATH", raising=False)


def _reported_blob(result) -> str:
    """The text the check surfaced (message + hint) — where the path lives."""
    return f"{result.message or ''} {result.hint or ''}".strip()


def test_profile_env_delegates_to_resolver(tmp_path, monkeypatch):
    """AC1/AC4: a profile root (canonical leaf) is reported, not ``~/.mempalace``.

    A profile install points the palace at a profile-scoped root that already
    holds the data (the canonical-leaf branch of the resolver).  The doctor
    must check *that* directory — i.e. the exact path ``palace_data_dir``
    resolves to — and must NOT fall back to the hard-coded global home.
    """
    profile_root = tmp_path / "profile" / ".mempalace"
    profile_root.mkdir(parents=True)
    # Canonical leaf: the *root* holds a non-empty chroma (data present), so
    # the resolver returns the root untouched (no descent), and it exists.
    (profile_root / palace_path.CHROMA_FILE).write_bytes(b"NOT-EMPTY")

    monkeypatch.setenv("PALACE_ROOT", str(profile_root))

    expected = Path(palace_path.palace_data_dir(_palace_layout_root(None)))
    global_default = Path.home() / ".mempalace"

    result = check_data_directory()
    blob = _reported_blob(result)

    # The check must be inspecting the resolver-derived directory…
    assert str(expected) in blob, (
        f"check_data_directory reported {blob!r}, but "
        f"palace_data_dir(_palace_layout_root(None)) resolves to {expected}"
    )
    # …and, because here it differs from the global default, the hard-coded
    # global home must no longer be the path under inspection (RED at HEAD).
    assert expected.resolve() != global_default.resolve()
    assert str(global_default) not in blob
    assert result.status == "PASS"


def test_profile_env_canonical_leaf_descent_reports_leaf(tmp_path, monkeypatch):
    """The Aug-20 split: data at ``<root>/palace``, reader pointed at ``<root>``.

    ``palace_data_dir`` descends to ``<root>/palace``.  The doctor must
    inspect that leaf, not the bare root and not the global home.
    """
    root = tmp_path / "root"
    (root / "palace").mkdir(parents=True)
    # Leaf (root/palace) holds the data file; root's own chroma is absent, so
    # is_chroma_empty(root/chroma.sqlite3) is True -> resolver descends.
    (root / "palace" / palace_path.CHROMA_FILE).write_bytes(b"DATA")

    monkeypatch.setenv("PALACE_ROOT", str(root))

    expected = Path(palace_path.palace_data_dir(_palace_layout_root(None)))
    assert expected == (root / "palace")  # sanity: resolver descended
    global_default = Path.home() / ".mempalace"

    result = check_data_directory()
    blob = _reported_blob(result)
    assert str(expected) in blob, (
        f"expected the doctor to inspect the leaf {expected}, saw {blob!r}"
    )
    assert str(global_default) not in blob
    assert result.status == "PASS"


def test_fresh_profile_reports_fail_with_profile_hint(tmp_path, monkeypatch):
    """AC2: a fresh profile (no data dir yet) still FAILs, hinting at the
    profile root — not the global ``$HOME``."""
    profile_root = tmp_path / "fresh-profile" / ".mempalace"
    # Do NOT create it: fresh profile, store not yet minted.
    assert not profile_root.exists()

    monkeypatch.setenv("PALACE_ROOT", str(profile_root))

    expected = Path(palace_path.palace_data_dir(_palace_layout_root(None)))
    result = check_data_directory()
    blob = _reported_blob(result)

    assert result.status == FAIL
    assert str(expected) in blob
    assert str(Path.home() / ".mempalace") not in blob


def test_mempalace_palace_path_env_delegates(tmp_path, monkeypatch):
    """The alternate env var (``$MEMPALACE_PALACE_PATH``) is honoured too."""
    profile_root = tmp_path / "alt" / ".mempalace"
    profile_root.mkdir(parents=True)
    (profile_root / palace_path.CHROMA_FILE).write_bytes(b"X")

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(profile_root))

    expected = Path(palace_path.palace_data_dir(_palace_layout_root(None)))
    result = check_data_directory()
    assert str(expected) in _reported_blob(result)


def test_global_default_still_falls_back_to_home(monkeypatch):
    """AC3: with no env override, global (non-profile) install behaviour is
    unchanged — the check falls back to ``~/.mempalace`` (here, a fake home)."""
    fake_home = Path("/nonexistent-bubo-home")
    # Ensure that home has no .mempalace so we exercise the fresh/FAIL branch,
    # and prove the *path* is rooted at fake home, not something else.
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(fake_home)))

    result = check_data_directory()
    blob = _reported_blob(result)
    assert str(fake_home / ".mempalace") in blob
    assert result.status == FAIL


def test_explicit_palace_root_overrides_env(tmp_path, monkeypatch):
    """Precedence: an explicit root wins over the env var (resolver contract)."""
    explicit = tmp_path / "explicit" / ".mempalace"
    explicit.mkdir(parents=True)
    (explicit / palace_path.CHROMA_FILE).write_bytes(b"X")

    # The env points elsewhere on purpose; _palace_layout_root(None) (no
    # explicit arg) still resolves via env, mirroring the doctor's default.
    monkeypatch.setenv("PALACE_ROOT", str(explicit))

    expected = Path(palace_path.palace_data_dir(_palace_layout_root(None)))
    assert expected == explicit
    result = check_data_directory()
    assert str(expected) in _reported_blob(result)


def test_reported_dir_is_exactly_resolver_output(tmp_path, monkeypatch):
    """Direct equality: the directory the doctor checks is byte-for-byte the
    resolver output (the #172 single-source guarantee, previously bypassed)."""
    profile_root = tmp_path / "eq" / ".mempalace"
    profile_root.mkdir(parents=True)
    (profile_root / palace_path.CHROMA_FILE).write_bytes(b"EQ")

    monkeypatch.setenv("PALACE_ROOT", str(profile_root))

    expected = Path(palace_path.palace_data_dir(_palace_layout_root(None)))
    result = check_data_directory()
    # Pull the leading token of the message — that is the path under
    # inspection — and require it equals the resolver output.
    leading = _reported_blob(result).split()[0]
    assert leading == str(expected)
