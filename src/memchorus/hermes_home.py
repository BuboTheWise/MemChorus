"""Central Hermes home-directory + per-profile path resolution.

Most MemChorus modules historically hardcoded the POSIX layout
``~/.hermes/...``. Hermes Desktop on Windows installs under
``%LOCALAPPDATA%\\hermes`` instead, so the orchestrator was reading and
writing a *different* tree from the one the running agent actually uses.

This module is the single source of truth for locating the Hermes home
directory **and** for resolving the per-profile paths (``config.yaml``,
``memories/``, tuning data) that hang off it.

Home resolution order (see :func:`hermes_home`):

1. ``$HERMES_HOME`` — honoured when set and pointing at an existing directory.
2. ``%LOCALAPPDATA%/hermes`` — the Windows Desktop install location, when
   ``os.name == "nt"`` and the directory exists.
3. ``Path.home() / ".hermes"`` — the POSIX / general fallback.

Per-profile resolution (see :func:`active_profile_name`,
:func:`profile_home`, :func:`profile_config_path`) — added for issue #267:

``hermes -p <name>`` sets ``$HERMES_HOME`` to ``.../profiles/<name>/`` and
usually does **not** set ``$HERMES_PROFILE``. The legacy resolvers then built
``hermes_home() / "profiles" / <profile> / "config.yaml"`` and, because
``hermes_home()`` already pointed at the profile dir, produced a *double-nested*
path (``.../profiles/<name>/profiles/default/config.yaml``) and read settings
from the wrong file (or not at all). The canonical resolvers below detect that
``$HERMES_HOME`` already scopes to a profile and stop nesting.

Note on multi-profile gateways: these resolvers read ``os.environ`` on every
call, which is correct for one-profile-per-process (the Kanban dispatcher and
``hermes -p`` model). If a single long-lived gateway process ever serves *many*
profiles in-process, a process-global singleton / env-var switch is
insufficient — Hermes' ``ctx_bound()`` (ContextVar) is the intended mechanism
to bind the active profile per request. Track that as a follow-up if it becomes
a real deployment; it is not required for the current per-process model.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Mirrors ``memchorus._VALID_PROFILE_RE``. Kept local so this module stays a
# leaf (no import of the package ``__init__``). The two must agree: a profile
# name is 1-48 chars of [a-zA-Z0-9_-].
_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,48}")


def hermes_home() -> Path:
    """Resolve the Hermes home directory.

    Returns a :class:`~pathlib.Path`. The order is:

    1. ``$HERMES_HOME`` when set and the value is an existing directory.
    2. ``%LOCALAPPDATA%/hermes`` on Windows (``os.name == "nt"``) when it exists.
    3. ``Path.home() / ".hermes"`` as the last resort.

    The function never raises; at worst it returns the POSIX fallback so
    callers can still ``mkdir -p / read / write`` a predictable location.
    """
    # 1. Explicit override — a developer or the Desktop installer can pin the
    #    tree directly. Requires an existing directory so a stale/empty env var
    #    does not silently detach us from the real layout.
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_dir():
            return candidate

    # 2. Hermes Desktop on Windows installs under %LOCALAPPDATA%\hermes.
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            candidate = Path(local_appdata) / "hermes"
            if candidate.is_dir():
                return candidate

    # 3. Canonical POSIX layout (Linux/macOS, or Windows with no Desktop tree).
    return Path.home() / ".hermes"


def hermes_home_str() -> str:
    """Return :func:`hermes_home` rendered as a ``str``.

    Convenience for callers that use ``os.path`` rather than ``pathlib``.
    """
    return str(hermes_home())


# ---------------------------------------------------------------------------
# Per-profile resolution (issue #267)
# ---------------------------------------------------------------------------
# The raw ``$HERMES_HOME`` (before ``hermes_home()``'s is_dir()/Windows
# gating) is the authoritative signal for whether this process is scoped to a
# single profile. ``hermes -p <name>`` and the Kanban dispatcher both set it to
# ``.../profiles/<name>/``. We read the *raw* value here — not
# ``hermes_home()`` — so the profile name survives even if the directory check
# or the Windows branch in ``hermes_home()`` reshapes the returned path.


def _raw_hermes_home() -> Path | None:
    """Return the raw ``$HERMES_HOME`` as a path, or ``None`` when unset.

    Distinct from :func:`hermes_home`: this returns the literal env value
    (expanded, but with no ``is_dir()`` / platform gating) so callers can
    reason about *which profile* the process is bound to.
    """
    env = os.environ.get("HERMES_HOME", "").strip()
    if not env:
        return None
    return Path(env).expanduser()


def _profile_name_from_home(p: Path) -> str | None:
    """Derive a profile name when *p* is a per-profile home directory.

    Recognises the layouts produced by ``hermes -p`` / the Kanban dispatcher:

    * ``.../profiles/<name>/...``  -> ``<name>`` (any depth; the first
      component after a ``profiles`` segment wins)
    * ``.../profile/<name>/...``   -> ``<name>`` (Hermes Desktop legacy)

    Returns ``None`` when *p* does not carry a profile segment, or when the
    candidate fails :data:`_PROFILE_NAME_RE` (i.e. it is not a usable name).
    """
    parts = p.parts
    for i, part in enumerate(parts):
        if part in ("profiles", "profile"):
            if i + 1 < len(parts):
                candidate = parts[i + 1]
                if _PROFILE_NAME_RE.fullmatch(candidate):
                    return candidate
    return None


def active_profile_name() -> str:
    """Resolve the active Hermes profile name (issue #267).

    Precedence:

    1. ``$HERMES_HOME`` when it points at a per-profile directory — the name is
       taken straight from the path (``.../profiles/<name>/``). This is the
       ``hermes -p`` / Kanban-dispatcher case and takes priority over the env
       var so a stale ``HERMES_PROFILE`` cannot detach us from the profile the
       process actually runs in.
    2. ``$HERMES_PROFILE`` when set (sanitised to ``"default"`` on an invalid
       value).
    3. ``"default"``.

    Always returns a string safe for filesystem paths. Reads ``os.environ`` on
    every call — see the module docstring for the multi-profile-gateway note.
    """
    home = _raw_hermes_home()
    if home is not None:
        name = _profile_name_from_home(home)
        if name:
            return name
    raw = os.environ.get("HERMES_PROFILE", "").strip()
    if raw and _PROFILE_NAME_RE.fullmatch(raw):
        return raw
    return "default"


def _base_home() -> Path:
    """Return the true base Hermes home — the directory that contains ``profiles/``.

    Distinct from :func:`hermes_home`, which in the ``hermes -p <name>`` / Kanban
    case already returns the profile-scoped directory (``.../profiles/<name>``).
    Here we strip the ``profiles/`` (or legacy ``profile/``) segment so that
    named-profile paths are always built relative to the real root
    (``~/.hermes``) and never double-nested (issue #267).
    """
    home = _raw_hermes_home()
    if home is not None:
        parts = home.parts
        for i, part in enumerate(parts):
            if part in ("profiles", "profile"):
                if i > 0:
                    return Path(*parts[:i])
                break
    return hermes_home()


def profile_home(profile: str | None = None) -> Path:
    """Return the per-profile home directory (issue #267).

    * The **default** profile lives at the base tree (:func:`_base_home`) — its
      ``config.yaml`` and ``memories/`` sit directly under ``~/.hermes`` (or the
      ``$HERMES_HOME`` root), **not** under ``~/.hermes/profiles/default/``. This
      matches the on-disk layout and the historical special case in the memory
      sources (``default`` → ``<base>/memories``).
    * Every **named** profile lives at ``<base>/profiles/<name>/``.

    Building from :func:`_base_home` (not :func:`hermes_home`) means the
    ``hermes -p <name>`` / Kanban case — where ``$HERMES_HOME`` is already
    ``.../profiles/<name>`` — resolves to that same directory without
    double-nesting, whether the caller passes the name explicitly or relies on
    the active profile.
    """
    base = _base_home()
    name = active_profile_name() if profile is None else profile
    if name == "default":
        return base
    return base / "profiles" / name


def profile_config_path(profile: str | None = None) -> Path:
    """Return the canonical per-profile ``config.yaml`` path (issue #267).

    This is the single canonical resolver the acceptance criteria asked for.
    Every module that historically built its own
    ``hermes_home() / "profiles" / <profile> / "config.yaml"`` (or a
    double-nested variant) should call this instead so all resolvers agree.
    """
    return profile_home(profile) / "config.yaml"
