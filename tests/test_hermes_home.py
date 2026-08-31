"""Tests for the central Hermes home-directory resolver (issue #147).

Covers:
  * the 3-tier resolution order of ``hermes_home()``
    (HERMES_HOME env → %LOCALAPPDATA%\\hermes on Windows → POSIX fallback)
  * ``hermes_home_str()`` string rendering
  * the cross-platform ``_looks_like_data_dir()`` heuristic (issue #147
    specifically fixes the Windows path recognition here)
  * ``_resolve_hermes_memory_dir()`` per-profile isolation

All filesystem access is monkeypatched so the tests are hermetic — they never
read the operator's real ``~/.hermes`` tree.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from memchorus.hermes_home import hermes_home, hermes_home_str
import memchorus.hermes_home as _hh_mod
from memchorus.hermes_memory_source import (
    _looks_like_data_dir,
    _resolve_hermes_memory_dir,
)


@pytest.fixture(autouse=True)
def _clean_hermes_env(monkeypatch):
    """Start each test with a neutral environment (no stale overrides)."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    yield


@pytest.fixture
def windows(monkeypatch):
    """Emulate a Windows host for ``memchorus.hermes_home``.

    ``hermes_home()`` branches on ``os.name == "nt"``. Setting the *global*
    ``os.name`` would make ``pathlib.Path`` dispatch to ``WindowsPath``
    (which raises ``NotImplementedError`` on Linux) and break pytest's own
    report machinery, which imported ``Path`` at load time. Instead hand the
    module under test a dedicated ``os`` view whose ``name`` is ``"nt"`` while
    its ``environ`` is the real environment. The global ``os`` stays posix, so
    ``Path`` remains ``PosixPath`` everywhere.
    """
    import types
    fake_os = types.SimpleNamespace(name="nt", environ=os.environ)
    monkeypatch.setattr(_hh_mod, "os", fake_os)
    yield


# ---------------------------------------------------------------------------
# hermes_home() — three resolution tiers
# ---------------------------------------------------------------------------

def test_returns_path_type(tmp_path):
    result = hermes_home()
    assert isinstance(result, Path)


def test_posix_fallback(monkeypatch, tmp_path):
    """No env, POSIX host → Path.home()/.hermes."""
    monkeypatch.setattr(os, "name", "posix")
    expected = Path.home() / ".hermes"
    assert hermes_home() == expected


def test_hermes_home_env_honoured_when_dir_exists(monkeypatch, tmp_path):
    """Tier 1: $HERMES_HOME pointing at an existing dir wins."""
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hermes_home() == tmp_path


def test_hermes_home_env_ignored_when_dir_missing(monkeypatch, tmp_path):
    """A stale/empty $HERMES_HOME (nonexistent path) must fall through to tier 3."""
    monkeypatch.setattr(os, "name", "posix")
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("HERMES_HOME", str(missing))
    assert hermes_home() == Path.home() / ".hermes"


def test_windows_localappdata_tier(windows, tmp_path):
    """Tier 2: on Windows, %LOCALAPPDATA%/hermes wins when it exists.

    HERMES_HOME is deliberately unset so we exercise the LOCALAPPDATA branch
    rather than tier 1.
    """
    appdata_dir = tmp_path / "AppData" / "Local"
    appdata_dir.mkdir(parents=True)
    (appdata_dir / "hermes").mkdir(parents=True)
    os.environ["LOCALAPPDATA"] = str(appdata_dir)
    assert hermes_home() == appdata_dir / "hermes"


def test_windows_localappdata_skipped_when_absent(windows, tmp_path):
    """Tier 2 falls through to POSIX if %LOCALAPPDATA%/hermes does not exist."""
    appdata_dir = tmp_path / "AppData" / "Local"
    appdata_dir.mkdir(parents=True)  # LOCALAPPDATA exists but no "hermes" child
    os.environ["LOCALAPPDATA"] = str(appdata_dir)
    monkey_home = Path.home() / ".hermes"
    assert hermes_home() == monkey_home


def test_hermes_home_beats_localappdata(windows, tmp_path):
    """Tier 1 outranks tier 2 even when both are present and valid."""
    appdata_dir = tmp_path / "AppData" / "Local"
    (appdata_dir / "hermes").mkdir(parents=True)
    env_dir = tmp_path / "env-pinned"
    env_dir.mkdir(parents=True)
    os.environ["LOCALAPPDATA"] = str(appdata_dir)
    os.environ["HERMES_HOME"] = str(env_dir)
    assert hermes_home() == env_dir


# ---------------------------------------------------------------------------
# hermes_home_str()
# ---------------------------------------------------------------------------

def test_str_helper_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "posix")
    s = hermes_home_str()
    assert isinstance(s, str)
    assert Path(s) == Path.home() / ".hermes"


def test_str_helper_env_override(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hermes_home_str() == str(tmp_path)


# ---------------------------------------------------------------------------
# _looks_like_data_dir() — cross-platform path recognition (issue #147 core)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "/tmp/memdir",                       # POSIX absolute
    "~/some/dir",                        # tilde + separator
    "C:\\Users\\me\\memdir",             # Windows absolute (the #147 gap)
    "C:/Users/me/memdir",                # Windows forward-slash form
    ".\\localdir",                       # relative with backslash marker
    "./localdir",                        # relative with slash marker
])
def test_positive_path_like(value):
    assert _looks_like_data_dir(value) is True


@pytest.mark.parametrize("value", [
    "hermes_default",       # canonical source id
    "my_source",            # plain identifier
    "mempalace",            # single word
])
def test_positive_simple_names_are_not_paths(value):
    assert _looks_like_data_dir(value) is False


def test_empty_string_not_path():
    assert _looks_like_data_dir("") is False


def test_bare_relative_word_not_path():
    # A single relative word with no separator is NOT a data dir.
    assert _looks_like_data_dir("localdir") is False


# ---------------------------------------------------------------------------
# _resolve_hermes_memory_dir() — per-profile isolation
# ---------------------------------------------------------------------------

def test_default_profile_memory_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    os_environ_profile_default = "default"
    monkeypatch.setenv("HERMES_PROFILE", os_environ_profile_default)
    result = _resolve_hermes_memory_dir()
    assert Path(result) == tmp_path / "memories"


def test_named_profile_memory_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "cthugha")
    result = _resolve_hermes_memory_dir()
    assert Path(result) == tmp_path / "profiles" / "cthugha" / "memories"


def test_profile_sanitized_on_resolution(monkeypatch, tmp_path):
    """A corrupt profile (e.g. with slashes) collapses to 'default'."""
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "a/b/c")  # fails [A-Za-z0-9_-]
    result = _resolve_hermes_memory_dir()
    assert Path(result) == tmp_path / "memories"
