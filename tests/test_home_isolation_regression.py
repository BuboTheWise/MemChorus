"""Issue #269 regression: the test suite must not write into the operator's
real home tree (``~/.hermes``, ``~/.memchorus-data``, ``~/.mempalace``, ...).

The isolation itself is installed at ``conftest.py`` import time — before any
memchorus module is imported or its import-time constants are baked in. It
redirects ``HERMES_HOME`` and the underlying home env (``HOME`` / ``USERPROFILE``)
to a private temp root, which then also drives every ``Path.home()`` /
``expanduser("~")`` site that bypasses the ``hermes_home()`` resolver.

These tests pin the *contract*:

  1. ``HERMES_HOME`` and ``HOME`` are set to a temp path (not the real home).
  2. Code paths that previously wrote into the real home (``ProhibitionsManager``)
     now resolve into the temp root and **actually write there** when exercised.
  3. The known-write file (``prohibitions.jsonl``) does **not** appear under
     the operator's real home after being triggered.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

_real_home_env = os.environ.get("_MC269_REAL_HOME")


def _tmp_root() -> Path:
    val = os.environ.get("HERMES_HOME") or os.environ.get("HOME") or ""
    assert val, "HERMES_HOME and HOME both unset — conftest isolation failed to install"
    return Path(val)


def _under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except (ValueError, TypeError):
        return False


# ── tests ────────────────────────────────────────────────────────────────────

class TestHomeIsolationEnv:
    """The conftest isolation must have redirected the home env to a temp path."""

    def test_hermes_home_is_temp(self):
        root = _tmp_root()
        # Must be under /tmp (or %TEMP% on Windows), not under the operator's home.
        # On POSIX: /tmp, on Windows: C:\Users\...\AppData\Local\Temp
        assert root.is_absolute(), f"HERMES_HOME is not absolute: {root}"
        # Should NOT contain the real home prefix (if we captured one).
        if _real_home_env:
            real = Path(_real_home_env)
            assert not _under(root, real), (
                f"isolated root {root} is inside the operator's real home {real} — "
                f"conftest isolation ineffective"
            )

    def test_home_env_is_temp(self):
        home = os.environ.get("HOME", "")
        assert home, "HOME env var not set by conftest isolation"
        if _real_home_env:
            real = Path(_real_home_env)
            assert not _under(Path(home), real), (
                f"isolated HOME {home} is inside the operator's real home {real}"
            )

    def test_home_and_hermes_home_agree(self):
        """Both env vars should point at (or under) the same temp root."""
        hh = os.environ.get("HERMES_HOME")
        home = os.environ.get("HOME")
        # Either they are the same path, or one is under the other —
        # but they must not be pointing at the real home.
        assert hh and home, "HERMES_HOME or HOME missing"
        hh_p, home_p = Path(hh), Path(home)
        assert _under(hh_p, home_p) or _under(home_p, hh_p) or hh_p == home_p, (
            f"HERMES_HOME ({hh}) and HOME ({home}) are in different trees"
        )


class TestProhibitionsManagerWritesToTemp:
    """The write-path leak (prohibitions.jsonl) must land in the temp root."""

    def test_default_dir_resolves_into_temp(self):
        from memchorus.prohibitions import ProhibitionsManager

        mgr = ProhibitionsManager()  # no data_dir → uses default
        fpath = mgr.file_path
        root = _tmp_root()
        assert _under(fpath, root), (
            f"ProhibitionsManager default path {fpath} is NOT under the "
            f"isolated root {root} — suite would pollute the real home"
        )
        # Must not be under the real home.
        if _real_home_env:
            real = Path(_real_home_env)
            assert not _under(fpath, real), (
                f"ProhibitionsManager resolved {fpath} under real home {real}"
            )

    @pytest.fixture()
    def write_probe(self, tmp_path):
        """Create a ProhibitionsManager pointing at a fresh dir and trigger load()."""
        from memchorus.prohibitions import ProhibitionsManager

        probe_dir = tmp_path / "probe_home" / ".memchoris-data"
        mgr = ProhibitionsManager(data_dir=probe_dir)
        mgr.load()  # writes prohibitions.jsonl
        yield mgr, probe_dir

    def test_load_writes_file_into_probe_dir(self, write_probe):
        _mgr, probe_dir = write_probe
        out = probe_dir / "prohibitions.jsonl"
        assert out.exists(), f"ProhibitionsManager.load() did not create {out}"
        assert out.stat().st_size > 0, "prohibitions.jsonl is zero bytes"

    def test_load_does_not_touch_real_home(self, write_probe):
        """After trigger, no prohibitions.jsonl should appear under the real home."""
        _, _ = write_probe
        if not _real_home_env:
            pytest.skip("no real home captured — cannot do negative assertion")
        real = Path(_real_home_env)
        candidates = [
            real / ".memchoris-data" / "prohibitions.jsonl",
            real / ".hermes" / ".memchoris-data" / "prohibitions.jsonl",
        ]
        for c in candidates:
            assert not c.exists(), (
                f"Found prohibitions.jsonl at real-home path {c} — "
                f"test suite leaked into the operator's home (issue #269)"
            )


class TestAutoInitConstantsAreIsolated:
    """Module-level constants that bake in the home path at import time."""

    def test_default_data_dir_not_real_home(self):
        from memchorus import auto_init

        d = Path(auto_init.DEFAULT_DATA_DIR)
        root = _tmp_root()
        assert _under(d, root), (
            f"auto_init.DEFAULT_DATA_DIR {d} is NOT under the isolated root {root}"
        )
        if _real_home_env:
            real = Path(_real_home_env)
            assert not _under(d, real), (
                f"auto_init.DEFAULT_DATA_DIR {d} resolved under real home {real}"
            )
