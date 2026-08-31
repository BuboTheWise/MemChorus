"""Central Hermes home-directory resolution (issue #147).

Most MemChorus modules historically hardcoded the POSIX layout
``~/.hermes/...``. Hermes Desktop on Windows installs under
``%LOCALAPPDATA%\\hermes`` instead, so the orchestrator was reading and
writing a *different* tree from the one the running agent actually uses.

This module is the single source of truth for locating the Hermes home
directory. Resolution order:

1. ``$HERMES_HOME`` — honoured when set and pointing at an existing directory.
2. ``%LOCALAPPDATA%/hermes`` — the Windows Desktop install location, when
   ``os.name == "nt"`` and the directory exists.
3. ``Path.home() / ".hermes"`` — the POSIX / general fallback.

Downstream consumers build subpaths via ``hermes_home() / "<sub>"``.
See :func:`hermes_home` and :func:`hermes_home_str`.
"""

from __future__ import annotations

import os
from pathlib import Path


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
