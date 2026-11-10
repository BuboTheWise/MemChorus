"""Issue #268 regression tests: per-profile default path, atomic replace, and
advisory file locking for ``ProhibitionsManager``.

These pin the three acceptance behaviours demanded by issue #268, built on top
of the canonical :func:`~memchorus.hermes_home.profile_home` resolver from
issue #267:

  1. **Per-profile default path** — with no ``data_dir`` override, the manager
     resolves ``<profile_home>/data/memchorus/prohibitions.jsonl``. The default
     profile lands under the base Hermes tree; a named profile (``HERMES_PROFILE``)
     nests under ``profiles/<name>/``. An explicit ``data_dir`` still wins.
  2. **Atomic replace** — ``save()`` writes a sibling ``.tmp`` then
     ``os.replace()`` it onto the target, so a crash mid-write never leaves a
     partial file, no stray ``.tmp`` remains, and the target inode is swapped
     (a real rename, not a truncate-in-place).
  3. **No dev-checkout absolute path** — the default path never embeds a
     developer checkout location (``.../workspace/Code/MemChorus/...``) or the
     old ``~/.memchorus-data`` fallback, so running the suite and the operator's
     real home are never polluted regardless of the active profile.

``conftest.py`` redirects ``HERMES_HOME`` / ``HOME`` to a private temp root at
import time; ``profile_home()`` reads ``os.environ`` on every call (no caching),
so these tests can re-point the profile per-test with ``monkeypatch`` and the
resolution follows.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

# Dev-checkout marker fragments that must NOT appear anywhere in the default
# path. Historically the manager built
#   ``hermes_home() / "workspace" / "Code" / "MemChorus" / "data" / ...``
#   and fell back to ``Path.home() / ".memchorus-data"`` — both of which are
#   exactly the absolute, operator-specific locations issue #268 removes.
_DEV_CHECKOUT_FRAGMENTS = (
    "/workspace/",
    "workspace\\Code\\",
    "\\Code\\MemChorus\\",
    "Code/MemChorus",
    ".memchoris-data",   # the old (typo) fallback dir
    ".memchorus-data",   # the new fallback dir name, still disallowed in default
)


def _resolved_default_path() -> Path:
    """The manager's default path (no data_dir) under the *current* env."""
    from memchorus.prohibitions import ProhibitionsManager

    return ProhibitionsManager().file_path


def _under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except (ValueError, TypeError):
        return False


def _tmp_root() -> Path:
    val = os.environ.get("HERMES_HOME") or os.environ.get("HOME") or ""
    assert val, "HERMES_HOME and HOME both unset — conftest isolation failed to install"
    return Path(val)


def _make_rules(n: int, prefix: str) -> list:
    from memchorus.prohibitions import Prohibition

    return [
        Prohibition(
            id=f"{prefix}-{i}",
            condition=f"rule {prefix} {i}",
            trigger_keywords=[f"{prefix}-kw-{i}"],
        )
        for i in range(n)
    ]


# ── (a) per-profile default path ────────────────────────────────────────────


class TestPerProfileDefaultPath:
    """The default (no data_dir) path must resolve through profile_home()."""

    def test_default_profile_under_base_root(self, monkeypatch):
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        fpath = _resolved_default_path()
        root = _tmp_root()
        # <root>/data/memchorus/prohibitions.jsonl
        assert fpath == root / "data" / "memchorus" / "prohibitions.jsonl", (
            f"default-profile path {fpath} should be <root>/data/memchorus/prohibitions.jsonl"
        )
        assert _under(fpath, root)

    def test_named_profile_nests_under_profiles(self, monkeypatch):
        monkeypatch.setenv("HERMES_PROFILE", "cthugha")
        fpath = _resolved_default_path()
        root = _tmp_root()
        assert fpath == root / "profiles" / "cthugha" / "data" / "memchorus" / "prohibitions.jsonl", (
            f"named-profile path {fpath} should nest under <root>/profiles/cthugha/"
        )
        assert _under(fpath, root / "profiles" / "cthugha")

    def test_named_profiles_do_not_collide(self, monkeypatch):
        """Two different named profiles must own two distinct files."""
        monkeypatch.setenv("HERMES_PROFILE", "profile-a")
        path_a = _resolved_default_path()
        monkeypatch.setenv("HERMES_PROFILE", "profile-b")
        path_b = _resolved_default_path()
        assert path_a != path_b, "two profiles resolved to the same prohibitions file"
        assert path_a.parent != path_b.parent

    def test_data_dir_override_still_wins(self, tmp_path, monkeypatch):
        """An explicit data_dir must take precedence over the per-profile default."""
        monkeypatch.setenv("HERMES_PROFILE", "cthugha")
        from memchorus.prohibitions import ProhibitionsManager

        mgr = ProhibitionsManager(data_dir=tmp_path / "pinned")
        assert mgr.file_path == (tmp_path / "pinned" / "prohibitions.jsonl")

    def test_default_path_is_stable_across_accesses(self, monkeypatch):
        """Re-accessing .file_path must not relocate the manager (caching)."""
        from memchorus.prohibitions import ProhibitionsManager

        mgr = ProhibitionsManager()
        first = mgr.file_path
        # Mutate env after the first resolution; the instance must stay put.
        monkeypatch.setenv("HERMES_PROFILE", "someone-else")
        assert mgr.file_path == first


# ── (b) atomic replace (no partial file) ────────────────────────────────────


class TestAtomicReplace:
    """save() must be a same-filesystem rename: complete-before/after, never partial."""

    def _fresh(self, tmp_path):
        from memchorus.prohibitions import ProhibitionsManager

        d = tmp_path / "data"
        mgr = ProhibitionsManager(data_dir=d)
        mgr.load()  # seeds defaults so we have something to persist
        return mgr, d

    def test_save_leaves_no_tmp_artifact(self, tmp_path):
        mgr, d = self._fresh(tmp_path)
        mgr._rules.extend(_make_rules(3, "extra"))
        mgr.save()
        stray = list(d.glob("*.tmp"))
        assert not stray, f"save() left stray tmp file(s): {stray}"

    def test_save_is_a_rename_inode_changes(self, tmp_path):
        mgr, d = self._fresh(tmp_path)
        fpath = mgr.file_path
        before = os.stat(fpath).st_ino
        mgr._rules.extend(_make_rules(5, "grow"))
        mgr.save()
        after = os.stat(fpath).st_ino
        assert after != before, (
            "save() kept the same inode — expected os.replace() (rename), "
            "not an in-place truncate"
        )

    def test_content_always_valid_jsonl(self, tmp_path):
        mgr, d = self._fresh(tmp_path)
        mgr._rules = _make_rules(7, "valid")
        mgr.save()
        text = (d / "prohibitions.jsonl").read_text(encoding="utf-8")
        lines = [ln for ln in text.strip().splitlines() if ln]
        assert len(lines) == 7, f"expected 7 rules on disk, got {len(lines)}"
        for ln in lines:
            obj = json.loads(ln)  # raises on any partial / corrupt line
            assert "id" in obj and "condition" in obj

    def test_round_trip_preserves_rules(self, tmp_path):
        mgr, d = self._fresh(tmp_path)
        mgr._rules = _make_rules(4, "rt")
        mgr.save()
        from memchorus.prohibitions import ProhibitionsManager

        reader = ProhibitionsManager(data_dir=d)
        n = reader.load()
        assert n == 4
        ids = sorted(r.id for r in reader._rules)
        assert ids == sorted(f"rt-{i}" for i in range(4))

    def test_concurrent_writers_produce_complete_file(self, tmp_path):
        """N threads racing save() must each hold the lock; the surviving file
        is one complete, valid JSONL payload — never a torn/partial merge."""
        from memchorus.prohibitions import ProhibitionsManager

        d = tmp_path / "data"
        payload_rules = 6
        errors: list = []
        barrier = threading.Barrier(4)

        def writer(wid: int) -> None:
            mgr = ProhibitionsManager(data_dir=d)
            mgr._rules = _make_rules(payload_rules, f"w{wid}")
            try:
                barrier.wait()  # maximise overlap at the lock
                mgr.save()
            except Exception as exc:  # noqa: BLE001 - surface any lock/IO fault
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent writers raised: {errors}"
        fpath = d / "prohibitions.jsonl"
        assert fpath.exists()
        text = fpath.read_text(encoding="utf-8")
        lines = [ln for ln in text.strip().splitlines() if ln]
        assert len(lines) == payload_rules, (
            f"expected a single complete {payload_rules}-rule payload, got {len(lines)} lines — "
            "possible torn write"
        )
        for ln in lines:
            json.loads(ln)  # every line must parse
        assert not list(d.glob("*.tmp")), "a .tmp survived a concurrent save"


# ── (c) no absolute dev-checkout path in the default ────────────────────────


class TestNoDevCheckoutPath:
    """The default path must be profile-derived, never an absolute checkout."""

    @pytest.mark.parametrize("profile", ["", "default", "cthugha"])
    def test_default_has_no_dev_checkout_fragment(self, monkeypatch, profile):
        if profile == "":
            monkeypatch.delenv("HERMES_PROFILE", raising=False)
        else:
            monkeypatch.setenv("HERMES_PROFILE", profile)
        fpath = str(_resolved_default_path())
        for frag in _DEV_CHECKOUT_FRAGMENTS:
            assert frag not in fpath, (
                f"default path {fpath!r} contains dev-checkout fragment {frag!r} — "
                "issue #268 requires a profile-derived path only"
            )

    def test_default_is_absolutely_resolved(self, monkeypatch):
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        fpath = _resolved_default_path()
        assert fpath.is_absolute(), f"default path {fpath} is not absolute"

    def test_old_memchoris_data_fallback_absent_from_source(self):
        """The old ``.memchoris-data`` fallback and ``workspace/Code/MemChorus``
        default must be gone from the module source itself."""
        import memchorus.prohibitions as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "workspace" not in src, "dev-checkout 'workspace' segment still referenced"
        assert "Code/MemChorus" not in src, "dev-checkout 'Code/MemChorus' still referenced"
        assert ".memchoris-data" not in src, "old '.memchoris-data' fallback still referenced"
        assert ".memchorus-data" not in src, "legacy '.memchorus-data' fallback still referenced"
        # And the new resolver IS wired in.
        assert "profile_home" in src, "profile_home() resolver not used in prohibitions.py"
