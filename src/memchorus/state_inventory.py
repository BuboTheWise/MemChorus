"""State inventory: single source of truth for all MemChorus state (issue #283).

Enumerates every file or directory that MemChorus reads or writes, per
profile.  This module is the authoritative reference for:

- ``MemoryProvider.backup_paths()`` — which state lives outside
  ``HERMES_HOME`` and must be declared to Hermes for backup.
- ``memchorus-doctor --migration`` preflight — which state is at risk
  of being lost during a host migration.
- The profile-export reality table in docs — what survives each
  backup / export mechanism.

Every path is resolved from the profile home Hermes is serving
(depends on #267 resolver).  The module is import-safe: it never
calls ``initialize()``, never spawns a subprocess, never touches
the network, and never raises from its public API.

Design rules (from issue #283 scope item 3):

- Return only paths that are **in ``$HOME``** and **outside
  ``HERMES_HOME``**.  Paths outside ``$HOME`` cannot be preserved
  by Hermes's native tools; they must be reported by the preflight,
  not silently returned.
- Return **nothing that contains credentials** (``.env``,
  ``auth.json``, MCP tokens, etc.).
- **Missing paths are skipped** — only existing state is declared.
- **Never raises.**  Any internal error yields an empty list.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "StateCategory",
    "StateEntry",
    "inventory_for_profile",
    "backup_paths_for_profile",
    "outside_home_entries",
    "expand_portable_path",
]


# ---------------------------------------------------------------------------
# Enumerations and data structures
# ---------------------------------------------------------------------------


class StateCategory(str, Enum):
    """Classification of a state entry."""

    CONFIG = "config"
    PROHIBITIONS = "prohibitions"
    MEMORY = "memory"
    HIT_RATE = "hit_rate"
    FEEDBACK = "feedback"
    PALACE = "palace"
    TUNING = "tuning"
    CHECKPOINT = "checkpoint"
    WORKING_STATE = "working_state"
    LEGACY = "legacy"


@dataclass(frozen=True)
class StateEntry:
    """A single tracked state file or directory.

    Attributes
    ----------
    name : str
        Human-readable identifier (e.g. ``"mempalace-palace"``).
    path : Path
        Absolute filesystem path.
    category : StateCategory
        What kind of state this is.
    is_dir : bool
        ``True`` if the entry is a directory (backup walks it).
    contains_secrets : bool
        ``True`` if the entry may contain credentials.  Entries with
        this flag are **excluded** from ``backup_paths()``.
    """

    name: str
    path: Path
    category: StateCategory
    is_dir: bool = False
    contains_secrets: bool = False

    def __str__(self) -> str:  # pragma: no cover
        kind = "dir" if self.is_dir else "file"
        secret = " [SECRETS]" if self.contains_secrets else ""
        return f"{self.name} ({kind}): {self.path}{secret}"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _home() -> Path:
    """Return ``$HOME`` as a ``Path``."""
    return Path(os.path.expanduser("~"))


def _hermes_home() -> Optional[Path]:
    """Return ``HERMES_HOME`` as a ``Path``, or ``None`` if unset.

    Tries the ``memchorus.hermes_home`` resolver first, then falls
    back to the ``HERMES_HOME`` env var, then to ``~/.hermes``.
    """
    try:
        from memchorus.hermes_home import hermes_home as _hh

        p = _hh()
        return Path(p) if p else None
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return None


def _is_under(parent: Path, child: Path) -> bool:
    """Return ``True`` if *child* is inside *parent* (or equal).

    Uses ``resolve()`` on both sides so symlinks don't fool us.
    Never raises.
    """
    try:
        cp = child.resolve()
        pp = parent.resolve()
        cp.relative_to(pp)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _active_profile() -> str:
    """Return the active profile name, defaulting to ``"default"``."""
    try:
        from memchorus.hermes_home import active_profile_name

        return active_profile_name() or "default"
    except Exception:
        return "default"


def _profile_home(profile: str) -> Path:
    """Return the profile home directory for *profile*.

    Tries the ``memchorus.hermes_home`` resolver, then falls back
    to ``<HERMES_HOME>/profiles/<profile>``.
    """
    try:
        from memchorus.hermes_home import profile_home as _ph

        p = _ph(profile)
        if p:
            return Path(p)
    except Exception:
        pass
    hh = _hermes_home() or (_home() / ".hermes")
    return hh / "profiles" / profile


def _resolve_data_dir(profile: str) -> Optional[Path]:
    """Resolve the MemPalace data directory from config files.

    Search order:
    1. Profile-local ``memchorus.yaml`` → ``data_dir`` or
       ``runtime.mempalace_home``.
    2. Global ``~/.memchorus.yaml`` → same keys.
    3. ``HERMES_HOME/memchorus.yaml`` → same keys.

    Returns ``None`` when no data dir is configured.
    """
    import yaml

    candidates: List[Path] = []
    p_home = _profile_home(profile)
    candidates.append(p_home / "memchorus.yaml")
    candidates.append(_home() / ".memchorus.yaml")
    hh = _hermes_home()
    if hh:
        candidates.append(hh / "memchorus.yaml")

    seen_cfg = set()
    for cfg_path in candidates:
        try:
            key = cfg_path.resolve()
        except (OSError, RuntimeError):
            continue
        if key in seen_cfg:
            continue
        seen_cfg.add(key)
        if not cfg_path.is_file():
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            if not isinstance(cfg, dict):
                continue
            dd = _data_dir_from_cfg(cfg)
            if dd and isinstance(dd, str):
                return Path(dd).expanduser()
        except Exception as exc:
            logger.debug(
                "state_inventory: failed to read %s: %s", cfg_path, exc
            )
    return None


def _data_dir_from_cfg(cfg: object) -> Optional[str]:
    """Extract the MemPalace data directory from a loaded config mapping.

    The canonical writer (``auto_init.generate_config``) nests the value
    under ``mempalace_config``:

    - ``mempalace_config.data_dir``
    - ``mempalace_config.runtime.mempalace_home``

    which is the shape the runtime reader (``state_migrate``) honours. The
    top-level ``data_dir`` / ``runtime.mempalace_home`` keys are accepted as
    a legacy fallback so hand-written configs (and the unit fixtures) that
    predate the nesting still resolve. Nested form wins when present.
    """
    if not isinstance(cfg, dict):
        return None

    # 1. Canonical nested form (what generate_config writes).
    mp = cfg.get("mempalace_config")
    if isinstance(mp, dict):
        dd = mp.get("data_dir")
        if dd and isinstance(dd, str):
            return dd
        rt = mp.get("runtime")
        if isinstance(rt, dict):
            mh = rt.get("mempalace_home")
            if mh and isinstance(mh, str):
                return mh

    # 2. Legacy top-level form (fixtures / older configs).
    dd = cfg.get("data_dir")
    if dd and isinstance(dd, str):
        return dd
    rt = cfg.get("runtime")
    if isinstance(rt, dict):
        mh = rt.get("mempalace_home")
        if mh and isinstance(mh, str):
            return mh
    return None


# ---------------------------------------------------------------------------
# Portable path expansion (item 4)
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{env:([^}]+)\}")


def expand_portable_path(value: str) -> str:
    """Expand a portable path the same way Hermes's config loader does.

    Handles (in order):

    1. ``${env:VAR}`` → ``${VAR}`` (Hermes's extended syntax).
    2. ``${VAR}`` / ``$VAR`` → ``os.path.expandvars`` (env lookup).
    3. ``~`` → ``os.path.expanduser``.

    This gives MemChorus's detector parity with Hermes's own
    ``_expand_env_vars`` / ``_interpolate_env_vars`` so a portable
    ``--palace ${HERMES_HOME}/.mempalace/palace`` resolves identically
    in both (acceptance criterion A7).

    Never raises: on any internal error the input is returned
    unchanged.
    """
    try:
        # Step 1: ${env:VAR} → ${VAR}
        value = _ENV_VAR_RE.sub(r"${\1}", value)
        # Step 2: ${VAR} / $VAR via expandvars
        value = os.path.expandvars(value)
        # Step 3: ~ via expanduser
        value = os.path.expanduser(value)
        return value
    except Exception:
        return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def inventory_for_profile(profile: Optional[str] = None) -> List[StateEntry]:
    """Enumerate all MemChorus state for a profile.

    Returns a list of :class:`StateEntry` covering every file or
    directory MemChorus reads or writes.  The list is ordered by
    category (config → prohibitions → memory → hit-rate → feedback →
    palace → legacy).

    Parameters
    ----------
    profile : str, optional
        Profile name.  Defaults to the active profile.

    Returns
    -------
    list[StateEntry]
        May be empty on internal error (never raises).
    """
    try:
        profile = profile or _active_profile()
        home = _home()
        p_home = _profile_home(profile)

        entries: List[StateEntry] = []

        # --- In-home (profile home) state ---

        # 1. Profile config
        entries.append(
            StateEntry(
                name="profile-config",
                path=p_home / "memchorus.yaml",
                category=StateCategory.CONFIG,
                is_dir=False,
            )
        )

        # 2. Prohibitions (issue #268)
        entries.append(
            StateEntry(
                name="prohibitions",
                path=p_home / "data" / "memchorus" / "prohibitions.jsonl",
                category=StateCategory.PROHIBITIONS,
                is_dir=False,
            )
        )

        # 3. Memory storage directory
        memories = p_home / "memories"
        entries.append(
            StateEntry(
                name="memory-storage",
                path=memories,
                category=StateCategory.MEMORY,
                is_dir=True,
            )
        )

        # 4. Hit-rate index (inside memories)
        entries.append(
            StateEntry(
                name="hit-rate-index",
                path=memories / "_hit_rate_index.json",
                category=StateCategory.HIT_RATE,
                is_dir=False,
            )
        )

        # 5. Tuning / calibration state (calibration_engine.DEFAULT_TUNING_DIR)
        # Real layout: <base>/data/memchorus/_tuning/<profile>.yaml — a directory,
        # not a single file. (A prior draft pointed at a non-existent tuning.json.)
        entries.append(
            StateEntry(
                name="tuning-state",
                path=p_home / "data" / "memchorus" / "_tuning",
                category=StateCategory.TUNING,
                is_dir=True,
            )
        )

        # --- Outside-home state (legacy / configurable) ---

        # 6. Feedback corrections (hardcoded default)
        entries.append(
            StateEntry(
                name="feedback-corrections",
                path=home / ".cache" / "memchorus_feedback_corrections.json",
                category=StateCategory.FEEDBACK,
                is_dir=False,
            )
        )

        # 7. Legacy global config
        entries.append(
            StateEntry(
                name="legacy-global-config",
                path=home / ".memchorus.yaml",
                category=StateCategory.CONFIG,
                is_dir=False,
            )
        )

        # 8. MemPalace palace (configurable data dir)
        data_dir = _resolve_data_dir(profile)
        if data_dir is None:
            # Default location (auto_init.DEFAULT_DATA_DIR)
            data_dir = home / ".mempalace" / profile
        entries.append(
            StateEntry(
                name="mempalace-palace",
                path=data_dir,
                category=StateCategory.PALACE,
                is_dir=True,
            )
        )

        # 9. Credentials (excluded from backup_paths)
        entries.append(
            StateEntry(
                name="credentials",
                path=p_home / ".env",
                category=StateCategory.CONFIG,
                is_dir=False,
                contains_secrets=True,
            )
        )
        entries.append(
            StateEntry(
                name="auth-json",
                path=p_home / "auth.json",
                category=StateCategory.CONFIG,
                is_dir=False,
                contains_secrets=True,
            )
        )

        return entries
    except Exception as exc:
        logger.debug("state_inventory.inventory_for_profile: %s", exc)
        return []


def backup_paths_for_profile(profile: Optional[str] = None) -> List[str]:
    """Return paths that satisfy the ``MemoryProvider.backup_paths()``
    contract.

    Filters (all must hold):

    - Path is **in ``$HOME``**.
    - Path is **outside ``HERMES_HOME``**.
    - Path **does not contain secrets**.
    - Path **exists** on disk.
    - No parent/child duplication (broadest path wins).

    Never raises — returns ``[]`` on any internal error.
    """
    try:
        entries = inventory_for_profile(profile)
        home = _home()
        hh = _hermes_home()

        results: List[str] = []
        seen: List[Path] = []

        for entry in entries:
            if entry.contains_secrets:
                continue
            try:
                resolved = entry.path.resolve()
            except (OSError, RuntimeError):
                continue
            if not resolved.exists():
                continue
            # Must be in $HOME
            if not _is_under(home, resolved):
                continue
            # Must be outside HERMES_HOME
            if hh is not None and _is_under(hh, resolved):
                continue
            # Deduplicate: skip if a parent already covers this path
            dominated = False
            for existing in seen:
                if _is_under(existing, resolved):
                    dominated = True
                    break
            if dominated:
                continue
            seen.append(resolved)
            results.append(str(resolved))

        return results
    except Exception as exc:
        logger.debug("state_inventory.backup_paths_for_profile: %s", exc)
        return []


def outside_home_entries(profile: Optional[str] = None) -> List[StateEntry]:
    """Return state entries that are **outside ``$HOME``** and exist.

    Used by the ``--migration`` preflight to report paths that Hermes
    cannot preserve (issue #283 scope item 3: "Paths outside ``$HOME``
    cannot be preserved by Hermes: they must be reported by the
    preflight").
    """
    try:
        entries = inventory_for_profile(profile)
        home = _home()
        out = []
        for e in entries:
            try:
                resolved = e.path.resolve()
            except (OSError, RuntimeError):
                continue
            if resolved.exists() and not _is_under(home, resolved):
                out.append(e)
        return out
    except Exception:
        return []


def in_home_outside_hermes_entries(
    profile: Optional[str] = None,
) -> List[StateEntry]:
    """Return state entries that are in ``$HOME`` but outside
    ``HERMES_HOME`` and exist — the set ``backup_paths()`` declares.

    Convenience for the preflight to cross-check that every such entry
    is covered.
    """
    try:
        entries = inventory_for_profile(profile)
        home = _home()
        hh = _hermes_home()
        out = []
        for e in entries:
            try:
                resolved = e.path.resolve()
            except (OSError, RuntimeError):
                continue
            if not resolved.exists():
                continue
            if not _is_under(home, resolved):
                continue
            if hh is not None and _is_under(hh, resolved):
                continue
            out.append(e)
        return out
    except Exception:
        return []
