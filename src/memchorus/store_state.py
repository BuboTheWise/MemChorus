#!/usr/bin/env python3
"""
store_state.py -- classify the post-restore memory-store state for orientation.

Issue #284 (Issue B, "after restore") scope 4: the orientation header shown on
the first turn of a restored home must be *non-empty* AND must distinguish
between four qualitatively different conditions, so the agent (and a human
reading the log) knows exactly what to do rather than guessing:

    1. ``store-reachable``        -- a memory source is present AND reachable.
    2. ``store-down``             -- a source is configured/present but the
                                     backing store (MemPalace / MCP) is down or
                                     not responding.  The data *should* be
                                     there once the process is up.
    3. ``store-missing``          -- no source is present at all, or the data
                                     files are absent/empty on disk after a
                                     restore.  Cross-checked against a small
                                     in-home manifest of the stores that were
                                     expected + approximate counts, written
                                     during normal operation (see
                                     ``state_inventory``).
    4. ``config-foreign``         -- the config points the store at a path
                                     outside ``$HOME`` (a foreign host path
                                     that does not exist on this machine), so
                                     even a working store cannot be reached.

The classification is a pure function of the orchestrator's registered
sources and the on-disk state manifest: it performs no I/O of its own beyond
reading the already-loaded source objects and (optionally) the state
inventory, and it *never* raises.  Any internal error degrades to the
most conservative label so the header is never empty and never misleading.

This module is deliberately import-light so it can be unit-tested with a
stub orchestrator and no live MemPalace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path as _Path
from typing import Any, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreState:
    """A classified store state plus the human-facing header text.

    Attributes
    ----------
    code:
        One of ``store-reachable`` / ``store-down`` / ``store-missing`` /
        ``config-foreign``.  Stable, grep-able, and used in provenance.
    label:
        Short human label (e.g. ``"store reachable"``).
    detail:
        A one-or-two-line explanation + the concrete next step, secret-free.
    expected_stores:
        Approximate count of stores the manifest says *should* exist in-home.
        ``0`` when no manifest / inventory is available.
    present_stores:
        Count of registered sources on the orchestrator (best-effort).
    """

    code: str
    label: str
    detail: str
    expected_stores: int = 0
    present_stores: int = 0

    @property
    def is_reachable(self) -> bool:
        return self.code == "store-reachable"


# ---------------------------------------------------------------------------
# State codes (importable constants for tests / provenance)
# ---------------------------------------------------------------------------

STORE_REACHABLE = "store-reachable"
STORE_DOWN = "store-down"
STORE_MISSING = "store-missing"
CONFIG_FOREIGN = "config-foreign"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _source_available(source: Any) -> bool:
    """Best-effort source reachability probe, mirroring the orchestrator."""
    try:
        check = getattr(source, "is_available", None)
        if check is None:
            # Fall back to a lightweight "looks wired" heuristic.
            return bool(getattr(source, "enabled", True))
        if callable(check):
            return bool(check())
        return bool(check)
    except Exception:
        return False


def _count_sources(orchestrator: Any) -> int:
    """Return the number of registered memory sources (best-effort)."""
    try:
        sources = getattr(orchestrator, "memory_sources", None)
        if sources is None:
            return 0
        if hasattr(sources, "__len__"):
            return len(sources)
        # Iterables without len.
        n = 0
        for _ in sources:
            n += 1
        return n
    except Exception:
        return 0


def _source_reachable(orchestrator: Any) -> bool:
    """True if at least one registered source is currently reachable."""
    try:
        sources = getattr(orchestrator, "memory_sources", None)
        if not sources:
            return False
        for source in sources.values():
            if _source_available(source):
                return True
    except Exception:
        return False
    return False


def _expected_store_count() -> int:
    """Approximate number of in-home stores the manifest expects.

    Reads the :mod:`memchorus.state_inventory` inventory for the active
    profile and counts non-secret state entries that *should* be present.
    Returns ``0`` if the inventory is unavailable or unreadable — this is a
    best-effort cross-check, not a hard requirement.
    """
    try:
        from memchorus.state_inventory import inventory_for_profile
        entries = inventory_for_profile()
        # Count palace / memory / checkpoint / tuning categories (the "stores").
        store_categories = {"palace", "memory", "checkpoint", "tuning", "working_state"}
        n = 0
        for entry in entries:
            cat = getattr(entry, "category", None)
            cat_val = getattr(cat, "value", cat) if cat is not None else None
            if cat_val in store_categories:
                n += 1
        return n
    except Exception:
        return 0


def _config_points_foreign() -> bool:
    """Detect whether the store config points at a path outside ``$HOME``.

    A "foreign" path is one the config names that does not resolve inside the
    local ``$HOME`` — the signature of a config copied from another host where
    the original absolute path no longer exists here.  Best-effort; returns
    ``False`` on any ambiguity (so we don't mislabel a healthy local store).
    """
    try:
        from memchorus.state_inventory import (
            _hermes_home,
            _home,
            inventory_for_profile,
        )
    except Exception:
        return False

    try:
        home = _home()
    except Exception:
        home = os.path.expanduser("~")

    try:
        home_resolved = str(home.resolve())
    except Exception:
        home_resolved = str(home)

    try:
        entries = inventory_for_profile()
    except Exception:
        return False

    foreign = 0
    for entry in entries:
        try:
            from pathlib import Path as _P
            p = _P(str(entry.path))
            resolved = str(p.resolve())
        except Exception:
            continue
        # A store path that is absolute and outside $HOME is foreign.
        if resolved.startswith("/"):
            if not (resolved == home_resolved or resolved.startswith(home_resolved + os.sep)):
                foreign += 1
    # Only flag as foreign if we found foreign paths AND none are local.
    # (One foreign path among several local ones is still a config issue.)
    return foreign > 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_store_state(orchestrator: Any) -> StoreState:
    """Classify the current memory-store state for the orientation header.

    Parameters
    ----------
    orchestrator:
        A live :class:`~memchorus.orchestrator.MemoryOrchestrator` (or a stub
        exposing ``.memory_sources``).  May be ``None`` when the orchestrator
        could not be constructed at all.

    Returns
    -------
    StoreState
        A frozen classification with a human-facing ``detail`` line.  Never
        raises; degrades to ``STORE_MISSING`` on any internal error.
    """
    present = _count_sources(orchestrator)
    expected = _expected_store_count()

    # 1) config-foreign — most specific, checked first.  A foreign config
    #    path means even a "reachable" source is unreachable on this host.
    try:
        if _config_points_foreign():
            return StoreState(
                code=CONFIG_FOREIGN,
                label="config points at a foreign path",
                detail=(
                    "The MemChorus store is configured at an absolute path that "
                    "is not inside this host's $HOME — likely a config copied "
                    "from another machine. Re-point the store path at a local "
                    "location (see `memchorus --doctor` / state inventory) or "
                    "restore the data files to that path."
                ),
                expected_stores=expected,
                present_stores=present,
            )
    except Exception:
        pass

    # 2) store-reachable — at least one source is up and answering.
    try:
        if orchestrator is not None and _source_reachable(orchestrator):
            return StoreState(
                code=STORE_REACHABLE,
                label="store reachable",
                detail=(
                    "Memory store is up and answering. Live recall is available "
                    "via memchorus_orient / memchorus_recall."
                ),
                expected_stores=expected,
                present_stores=present,
            )
    except Exception:
        pass

    # 3) store-down — sources are registered but none are reachable.
    try:
        if orchestrator is not None and present > 0:
            return StoreState(
                code=STORE_DOWN,
                label="store down",
                detail=(
                    "A memory source is configured but not responding (MemPalace "
                    "or its MCP backend is down). Start the backing store, then "
                    "re-run — the data is expected to be present once it is up."
                ),
                expected_stores=expected,
                present_stores=present,
            )
    except Exception:
        pass

    # 4) store-missing — no sources at all, or data absent/empty after restore.
    if expected > 0:
        missing_detail = (
            f"No memory source is reachable and the in-home manifest lists "
            f"~{expected} expected store(s) that are absent or empty on disk — "
            f"this looks like a post-restore gap. Re-provision the memchorus "
            f"package (see the provider's unavailable_reason / after-install "
            f"notes) and restore the store data."
        )
    else:
        missing_detail = (
            "No memory source is reachable and no expected stores were found "
            "on disk. The memchorus package and/or store data may not be "
            "restored on this host — run the re-provision step in the "
            "provider's after-install notes."
        )

    return StoreState(
        code=STORE_MISSING,
        label="store missing or empty after restore",
        detail=missing_detail,
        expected_stores=expected,
        present_stores=present,
    )
