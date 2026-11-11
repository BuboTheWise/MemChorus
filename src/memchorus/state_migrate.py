"""One-time migration of legacy MemChorus state into the profile home (issue #283, scope item 2).

New installs put all MemChorus-owned state inside the profile home so that
Hermes's native tools (``hermes backup``, ``hermes profile export/import``)
preserve it without any MemChorus-specific export step.

Existing installs often still carry state outside the profile home:

- ``~/.mempalace/<profile>`` — the old MemPalace data dir (``auto_init.DEFAULT_DATA_DIR``).
- ``~/.memchorus.yaml`` — the legacy global config.

This module enumerates those legacy sources and performs an idempotent
one-time move into the profile home.  After the move, the legacy paths are
**reported, not used** — callers should treat the profile home as the single
source of truth and never silently fall back to the legacy location.

Idempotence
-----------
A marker file (``<profile_home>/data/memchorus/state_migrated.json``) records
every source that has already been migrated.  Subsequent runs skip those
sources.  A source that is already empty or absent is also skipped (nothing
to move).  When both the source and the destination exist and are non-empty,
the destination wins (it is treated as authoritative) and the source is
renamed to ``<source>.bak`` so it is preserved, never silently destroyed.

Design rules (issue #283 scope item 2 + item 3):

- Never touches state **outside** ``$HOME`` — those paths must be reported
  by the ``--migration`` preflight, not silently moved or preserved here.
- Never writes to **credential** files (``.env``, ``auth.json``).
- Never raises from the public API; internal errors yield empty result.
- Read-safe: ``dry_run=True`` computes the full plan without touching
  the filesystem or any config file.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Path helpers (no imports of memchorus.hermes_home so the module is import-safe
# even when the resolver is not yet wired — mirrors state_inventory.py's
# fallback pattern).
# ---------------------------------------------------------------------------


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _hermes_home() -> Optional[Path]:
    try:
        from memchorus.hermes_home import hermes_home as _hh

        p = _hh()
        return Path(p) if p else None
    except Exception:
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env).expanduser()
        return None


def _profile_home(profile: str) -> Path:
    try:
        from memchorus.hermes_home import profile_home as _ph

        p = _ph(profile)
        if p:
            return Path(p)
    except Exception:
        pass
    hh = _hermes_home() or (_home() / ".hermes")
    return hh / "profiles" / profile


def _canonical_in_home(profile: str) -> Path:
    """Return the in-home canonical data dir for *profile*.

    Issue #283 item 2: state lives *inside the profile home* for every profile —
    ``profile_home(profile) / "mempalace"``. The default profile resolves to
    ``<hermes_home>/mempalace``; a named profile to ``<hermes_home>/profiles/<name>/mempalace``.
    This matches the writer (``auto_init.generate_config``) exactly, so
    one-time-moved state and freshly-written state always land in the same dir.
    """
    ph = _profile_home(profile)
    return ph / "mempalace"


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------


def marker_path(profile: str) -> Path:
    return _profile_home(profile) / "data" / "memchorus" / "state_migrated.json"


def read_marker(profile: str) -> Optional[dict]:
    """Return the marker dict if it exists and parses, else ``None``."""
    p = marker_path(profile)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def write_marker(profile: str, sources: List[str]) -> Path:
    """Write the migration marker.  Returns the marker path."""
    p = marker_path(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "migrated_from": sources,
                "migrated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )
    return p


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateMigrationResult:
    """Outcome of :func:`migrate_profile_state`.

    Attributes
    ----------
    moved : list[tuple[str, str]]
        ``(source, destination)`` pairs that were actually moved.
    preserved : list[tuple[str, str]]
        Pairs where the destination already existed; the source was renamed
        to a ``.bak`` sibling instead of being removed.
    config_rewritten : list[str]
        Absolute paths of config files that were rewritten to point at the
        in-home canonical dir.
    marker_written : Optional[str]
        Absolute path of the marker file, or ``None`` if no move was made
        (idempotent re-run, or ``dry_run``).
    already_migrated : bool
        ``True`` when the marker was found and every source listed in it is
        gone from disk (nothing to do on this run).
    """

    moved: List[tuple] = field(default_factory=list)
    preserved: List[tuple] = field(default_factory=list)
    config_rewritten: List[str] = field(default_factory=list)
    marker_written: Optional[str] = None
    already_migrated: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def legacy_sources(profile: Optional[str] = None) -> List[Path]:
    """Return legacy state paths that exist on disk for *profile*.

    Candidates (only those that **exist** and are **in ``$HOME``**):

    - ``~/.mempalace/<profile>`` — legacy MemPalace data dir.
    - ``~/.memchorus.yaml`` — legacy global config.
    - ``~/.memchorus-data/prohibitions.jsonl`` — legacy prohibitions (#268).

    Never raises — returns ``[]`` on any internal error.
    """
    try:
        profile = profile or "default"
        home = _home()

        candidates = [
            home / ".mempalace" / profile,
            home / ".memchorus.yaml",
            home / ".memchorus-data" / "prohibitions.jsonl",
        ]

        out: List[Path] = []
        for c in candidates:
            try:
                resolved = c.resolve()
            except (OSError, RuntimeError):
                continue
            if not resolved.exists():
                continue
            if not _in_home(resolved, home):
                continue
            out.append(resolved)
        return out
    except Exception:
        return []


def migrate_profile_state(
    profile: Optional[str] = None,
    *,
    dry_run: bool = False,
) -> StateMigrationResult:
    """Perform the one-time migration of legacy state into the profile home.

    Behaviour:

    1. Enumerate legacy sources (see :func:`legacy_sources`).
    2. For each source that exists and is not yet recorded in the marker:
       - Compute the in-home destination.
       - If the destination exists and is non-empty, **preserve** the source
         (rename to ``<source>.bak``) — the destination is authoritative.
       - Otherwise, move the source to the destination.
    3. Rewrite the profile's ``memchorus.yaml`` ``data_dir`` and
       ``runtime.mempalace_home`` to the in-home canonical dir (idempotent).
    4. Write the migration marker (unless ``dry_run``).

    Idempotent: calling twice (or after a crash mid-run) is safe.  A source
    already recorded in the marker is skipped; an empty source is skipped;
    a destination that already exists wins.

    ``dry_run=True`` computes the full plan but touches nothing — no file is
    moved, renamed, or rewritten, and the marker is not written.

    Never raises — on internal error returns an empty
    :class:`StateMigrationResult`.
    """
    try:
        profile = profile or "default"
        home = _home()
        p_home = _profile_home(profile)
        canonical = _canonical_in_home(profile)

        result_moved: List[tuple] = []
        result_preserved: List[tuple] = []
        result_config: List[str] = []
        marker_written: Optional[str] = None

        # -- Check whether we are re-running after a completed migration --
        existing_marker = read_marker(profile)
        if existing_marker and existing_marker.get("migrated_from"):
            # Every source in the marker is gone (or we're re-running)
            marker_sources = existing_marker["migrated_from"]
            all_gone = all(not Path(s).exists() for s in marker_sources)
            if all_gone:
                return StateMigrationResult(already_migrated=True)

        # -- Determine sources to move --
        sources = legacy_sources(profile)
        # Exclude sources already recorded in the marker
        if existing_marker and existing_marker.get("migrated_from"):
            done = set(existing_marker["migrated_from"])
            sources = [s for s in sources if str(s) not in done]

        if not sources:
            # Nothing to move — possibly all already migrated.
            if existing_marker:
                return StateMigrationResult(already_migrated=True)
            return StateMigrationResult()

        # -- Move each source --
        moved_sources: List[str] = []
        for src in sources:
            # Compute destination (issue #283 scope item 2: in-home canonical)
            if src.name == ".memchorus.yaml":
                # Legacy global config → the profile's canonical writer config
                # (``memchorus.yaml``, no leading dot — the name the reader uses).
                dst = p_home / "memchorus.yaml"
            elif src.name == "prohibitions.jsonl":
                # Legacy prohibitions (#268) → in-home data dir
                dst = p_home / "data" / "memchorus" / "prohibitions.jsonl"
            else:
                # ~/.mempalace/<profile> → <profile_home>/mempalace[/profile]
                dst = canonical

            if dry_run:
                result_moved.append((str(src), str(dst)))
                moved_sources.append(str(src))
                continue

            # Ensure destination parent exists
            dst.parent.mkdir(parents=True, exist_ok=True)

            # Decide whether the destination already holds authoritative data.
            if dst.is_dir():
                dst_nonempty = any(dst.iterdir())
            elif dst.exists():
                dst_nonempty = dst.stat().st_size > 0
            else:
                dst_nonempty = False

            if dst_nonempty:
                # Destination wins — preserve source as .bak
                bak = src.with_name(src.name + ".bak")
                if bak.exists():
                    # .bak already there — leave both, just record.
                    pass
                else:
                    os.rename(str(src), str(bak))
                result_preserved.append((str(src), str(dst)))
            else:
                # Move it
                if dst.exists():
                    dst.unlink()  # empty file — safe to remove
                src_parent = src.parent
                shutil.move(str(src), str(dst))
                result_moved.append((str(src), str(dst)))
                # If we moved a file and its legacy container dir is now empty,
                # remove the empty dir so the migration leaves no residue.
                # (e.g. ~/.memchorus-data/ after prohibitions.jsonl is moved.)
                if not src.is_dir():
                    try:
                        if src_parent.exists() and not any(src_parent.iterdir()):
                            src_parent.rmdir()
                    except OSError:
                        pass  # best-effort — never block the move

            moved_sources.append(str(src))

        # -- Rewrite profile config to point at canonical in-home dir --
        cfg_path = p_home / "memchorus.yaml"
        if cfg_path.is_file():
            try:
                import yaml  # type: ignore[import-not-found]

                cfg = yaml.safe_load(cfg_path.read_text()) or {}
                if isinstance(cfg, dict):
                    changed = False

                    # mempalace_config.data_dir
                    mp = cfg.get("mempalace_config")
                    if isinstance(mp, dict):
                        dd = mp.get("data_dir")
                        if dd and Path(str(dd)).expanduser() != canonical:
                            mp["data_dir"] = str(canonical)
                            changed = True

                    # mempalace_config.runtime.mempalace_home
                    rt = (mp or {}).get("runtime") if isinstance(mp, dict) else None
                    if isinstance(rt, dict):
                        mh = rt.get("mempalace_home")
                        if mh and Path(str(mh)).expanduser() != canonical:
                            rt["mempalace_home"] = str(canonical)
                            changed = True

                    if changed and not dry_run:
                        cfg_path.write_text(
                            yaml.dump(cfg, default_flow_style=False, sort_keys=False)
                        )
                        result_config.append(str(cfg_path))
            except Exception:
                pass  # never block the move on a config rewrite

        # -- Write marker --
        if not dry_run and moved_sources:
            p = write_marker(profile, moved_sources)
            marker_written = str(p)

        return StateMigrationResult(
            moved=result_moved,
            preserved=result_preserved,
            config_rewritten=result_config,
            marker_written=marker_written,
        )
    except Exception:
        return StateMigrationResult()


def _in_home(path: Path, home: Path) -> bool:
    try:
        p = path.resolve()
        h = home.resolve()
        p.relative_to(h)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


__all__ = [
    "StateMigrationResult",
    "legacy_sources",
    "migrate_profile_state",
    "marker_path",
    "read_marker",
    "write_marker",
]
