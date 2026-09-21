"""WorkingStateSeeder — IMPL #209 (a1): deterministic per-project working-state drawers.

Direction (a1) of the #209 "corpus-working-state" plan: populate the MemPalace
palace with exactly ONE working-state drawer per active project (in the
``memchorus_workspace / working-state`` room) so the (a2) ranking surface has
something to rank.  The (b) diagnostic half (:mod:`memchorus.corpus_balancer`)
already reads these rooms; this module provides the population side.

Design constraints (from the card body and D2 amendment):

* **One drawer per project, always upserted.**  There must be no unbounded
  growth — a repeated ``on_session_start`` in the SAME session does NOT create
  a second drawer.  Idempotency is enforced two ways:

    1. **In-process session cache** (primary): once ``ensure()`` has written a
       drawer for a given ``(slug, state-hash)`` pair in this process, further
       calls against the same pair short-circuit — a single
       ``memory_source.save()`` per slug per session state.
    2. **MemPalace content-hash dedup** (secondary): the drawer's
       ``drawer_id`` is derived from ``sha256(wing|room|content)``
       (:func:`mempalace.ids.make_drawer_id_from_content`), so byte-identical
       content re-saved later (e.g. across processes) also lands as an
       ``already_exists`` no-op rather than a duplicate row.

  The combination guarantees AC-S3: a fresh session in a real project
  produces exactly ONE drawer, and a second ``on_session_start`` in that
  session (state unchanged) triggers no second write.

* **Write goes through ``MemorySource.save()`` (NOT ``add_drawer``).**  The D2
  amendment requires the A3 project-stamp on the write path so that
  :func:`memchorus.relevance_engine.closet_bound_result` can classify the
  seeded drawer as *bound* once t_1d3d1f72 (A2–A5) merges.  The MemPalace
  source's ``save()`` calls ``_active_slug()`` (which resolves the active
  project via :func:`memchorus.orientation._resolve_project`) and forwards
  it to the MCP server as the ``project`` drawer metadata — the primary
  binding signal for the #206 predicate.  Routing to the correct wing/room
  comes from the source's ``_resolve_wing`` / ``_categorize_room`` maps,
  which now admit the ``WORKING_STATE`` category (see
  :mod:`memchorus.mempalace_memory_source`).

* **Slug source-of-truth is :func:`memchorus.orientation._resolve_project`.**
  We re-use it rather than re-implement: the same slug the read path binds on
  is stamped on the write path, so the (b) census and (a2) ranking see
  consistent identity.  Raw hex Kanban task IDs are rejected by
  :func:`memchorus.orientation._is_hermez_project_name` (which
  :func:`_resolve_project` already consults), so they never become drawers.

* **Snapshot body is bounded, grep-friendly, and deterministically composed.**
  Fields: ``goal``, ``in_flight`` (diffs / PRs), ``open_kanban``
  (cards), ``blockers``, ``next_actions`` (bounded to 3).  No volatile
  timestamps inside the body — the ``state-hash`` (computed from exactly
  these fields) rides in the ``source_file`` and NOT in the content, so the
  content itself is stable across repeated seeds of unchanged state and
  MemPalace's content-hash dedup stays a true no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "WorkingStateSeeder",
    "compose_snapshot",
    "resolve_slug",
    "state_hash",
    "WORKING_STATE_CATEGORY",
    "WORKING_STATE_WING",
    "WORKING_STATE_ROOM",
    "NEXT_ACTIONS_MAX",
    "SESSION_SEED_CACHE",
    "clear_session_cache",
]

# --- constants -------------------------------------------------------------

WORKING_STATE_CATEGORY = "WORKING_STATE"
WORKING_STATE_WING = "memchorus_workspace"
WORKING_STATE_ROOM = "working-state"
NEXT_ACTIONS_MAX = 3  # bounded "next 3 actions" (spec §3)

# In-process idempotency cache: key = (slug, shash) -> True once a successful
# write of that exact state has occurred in this process.  Multiple states for
# the same slug can coexist (a changed state upserts rather than being
# collapsed onto the previous one), which is what lets the seeder both
#   * dedup repeated writes of the *same* state (AC-S3), and
#   * upsert writes of a *changed* state (new shash ⇒ new cache key ⇒ save runs)
# even though the MemorySource's own key is slug-scoped.
#
# Cross-instance arrive-from-another-process dedup still uses a one-shot probe
# of ``source.retrieve(slug_key)`` on the very first in-process write for that
# slug — that's :func:`WorkingStateSeeder._slug_seen_in_process` below.  Once a
# slug has been written in-process (any state), the probe is skipped so the
# source's slug-scoped store can't mask a genuine state change.
SESSION_SEED_CACHE: Dict[Any, bool] = {}
# slug -> True (or the shash we last wrote for that slug).
_SEEN_IN_PROCESS: Dict[str, Any] = {}


def clear_session_cache() -> None:
    """Reset the in-session idempotency caches (test / process-reset helper)."""
    SESSION_SEED_CACHE.clear()
    _SEEN_IN_PROCESS.clear()


def _slug_cached_hash(slug: str) -> Optional[str]:
    """Return the last written state-hash for *slug* in this process, or None.

    Used by :meth:`WorkingStateSeeder.ensure` to decide whether a repeated
    write of the same state should short-circuit.  A fresh process (or a slug
    never written in-process) returns ``None``, in which case the seeder
    falls through to the :meth:`MemorySource.retrieve` cross-instance probe
    before writing.
    """
    entry = _SEEN_IN_PROCESS.get(slug)
    return entry if isinstance(entry, str) else None


def _canonical_slug(raw: Any) -> Optional[str]:
    """Normalise the project slug exactly as the source's ``_active_slug()`` does.

    Mirrors :meth:`memchorus.mempalace_memory_source.MempalaceMemorySource._active_slug`
    (``strip`` → ``lower`` → ``None``-if-empty) so the A3 project-stamp on the
    write path and ``source_file`` / ``key`` on the seeder always agree.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    return s or None


def resolve_slug(provenance: Optional[str] = None) -> Optional[str]:
    """Resolve the active project slug for this call, or ``None`` (silent skip).

    Reuses :func:`memchorus.orientation._resolve_project` — the canonical
    priority chain (HERMES_KANBAN_TASK → HERMES_WORKSPACE → CWD basename) with
    hex Kanban task IDs rejected by :func:`memchorus.orientation._is_hermez_project_name`.

    ``provenance`` is accepted for API symmetry with the card's build spec;
    the resolver's own env-driven chain is authoritative.
    """
    try:
        from memchorus import orientation

        slug = orientation._resolve_project(os.environ.get("HERMES_KANBAN_TASK"))
    except Exception as exc:  # pragma: no cover - defensive: never fail a seed
        logger.debug("working_state_seeder: resolve_slug failed: %s", exc)
        return None
    return _canonical_slug(slug) if slug else None


def _bounded(items: Optional[Sequence[Any]], cap: int) -> List[str]:
    """Coerce to a bounded list of stripped strings, dropping empties."""
    if not items:
        return []
    out: List[str] = []
    for item in items:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            out.append(s)
        if len(out) >= cap:
            break
    return out


def compose_snapshot(
    slug: str,
    *,
    goal: Optional[str] = None,
    in_flight: Optional[Sequence[Any]] = None,
    open_kanban: Optional[Sequence[Any]] = None,
    blockers: Optional[Sequence[Any]] = None,
    next_actions: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Compose the bounded, deterministic working-state snapshot body.

    Parameters mirror the card's build spec (goal / in-flight diffs-PRs /
    open kanban cards / blockers / next 3 actions).  ``next_actions`` is
    hard-capped to :data:`NEXT_ACTIONS_MAX` (3) entries; other fields are
    bounded to their natural length so a pathologically large ``in_flight``
    does not bloat the drawer beyond MemPalace's ``chunk_size``.

    The body carries exactly the routing fields the MemPalace source needs to
    resolve wing / room / provenance / project via its ``_resolve_wing`` /
    ``_categorize_room`` / ``add_drawer`` seams — plus a human- and search-
    friendly ``snapshot`` string.

    Determinism: every field is coerced to a stable string form (list order
    preserved, blanks dropped, ``next_actions`` capped).  This is what makes
    :func:`state_hash` stable and MemPalace's content-hash dedup a true
    no-op for unchanged state.
    """
    goal_s = str(goal).strip() if goal else ""
    in_flight_s = _bounded(in_flight, 16)
    open_kanban_s = _bounded(open_kanban, 16)
    blockers_s = _bounded(blockers, 16)
    next_actions_s = _bounded(next_actions, NEXT_ACTIONS_MAX)

    # Grep-friendly AAAK-style snapshot string for the drawer content.  Kept
    # compact (≤200 chars per field, 5 fields total) so it stays under the
    # MemPalace default ``chunk_size`` and renders cleanly in search results.
    def _fmt(label: str, items: List[str]) -> str:
        if not items:
            return f"{label}:"
        return f"{label}: " + " | ".join(items)

    # Canonicalise the slug once (strip/lower) — the snapshot's ``project``
    # field, the ``project=<slug>`` line in the body, and MemPalace's A3
    # project-stamp (which the source's ``_active_slug()`` applies) must all
    # agree.  Using the same normalisation here makes round-trip assertions on
    # the stored payload exact.
    slug_c = slug.strip().lower() if slug else ""

    lines = [f"project={slug_c}"]
    if goal_s:
        lines.append(f"goal: {goal_s[:200]}")
    lines.append(_fmt("in_flight", in_flight_s))
    lines.append(_fmt("open_kanban", open_kanban_s))
    lines.append(_fmt("blockers", blockers_s))
    lines.append(_fmt("next_actions", next_actions_s))
    snapshot_txt = "\n".join(lines)

    return {
        # --- routing fields (read by MempalaceMemorySource.save()) ---------
        "category": WORKING_STATE_CATEGORY,
        # --- body (read by _to_str() → content → drawer_id hash) -----------
        "snapshot": snapshot_txt,
        # --- structured (read by corpus_balancer / tests) ------------------
        "project": slug_c,
        "goal": goal_s,
        "in_flight": in_flight_s,
        "open_kanban": open_kanban_s,
        "blockers": blockers_s,
        "next_actions": next_actions_s,
    }


def state_hash(snapshot: Dict[str, Any]) -> str:
    """Deterministic short hash of the snapshot's state fields.

    Hashes ONLY the state-bearing fields (goal / in_flight / open_kanban /
    blockers / next_actions), NOT routing or identity fields.  Used for two
    purposes:
      1. The in-process :data:`SESSION_SEED_CACHE` key.
      2. The ``source_file`` suffix (``PROJECT_<slug>_<hash>``), which is also
         the provenance identifier forwarded to the MCP server.
    """
    state_fields = (
        snapshot.get("goal") or "",
        tuple(snapshot.get("in_flight") or ()),
        tuple(snapshot.get("open_kanban") or ()),
        tuple(snapshot.get("blockers") or ()),
        tuple(snapshot.get("next_actions") or ()),
    )
    # Canonical form: stable JSON of the tuple, then SHA-256 first 12 hex.
    raw = json.dumps(
        [str(s) if isinstance(s, str) else [str(x) for x in s]
        for s in state_fields
    ], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]


class WorkingStateSeeder:
    """Populate exactly one working-state drawer per active project.

    Call sites:
      * ``hooks.on_session_start`` → ``ensure(seed=True)``  (idempotent)
      * ``hooks.on_session_end``    → ``ensure(seed=False)`` (upsert)

    The seeder is a thin coordinator — it knows which key to use, how to gate
    repeated saves in this process, and how to surface ``source_file`` /
    provenance correctly.  All storage mechanics (wing/room resolution, A3
    project-stamp, MCP call, local cache mirror) are delegated to the
    MemPalace :class:`MemorySource` passed in (typically
    ``orchestrator.memory_sources["mempalace"]``); see D2 amendment.
    """

    def __init__(self, memory_source: Any) -> None:
        """
        Args:
            memory_source: A ``MemorySource``-like object exposing at least
                ``save(key, value) -> bool`` and ``retrieve(key) -> Any | None``.
                Tests pass a lightweight stub; hooks pass the live MemPalace
                source from the orchestrator.
        """
        self._source = memory_source

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @property
    def source(self) -> Any:
        """The :class:`MemorySource` the seeder writes through."""
        return self._source

    def ensure(
        self,
        slug: Optional[str] = None,
        *,
        seed: bool = True,
        goal: Optional[str] = None,
        in_flight: Optional[Sequence[Any]] = None,
        open_kanban: Optional[Sequence[Any]] = None,
        blockers: Optional[Sequence[Any]] = None,
        next_actions: Optional[Sequence[Any]] = None,
    ) -> bool:
        """Ensure a working-state drawer exists for *slug* (idempotent).

        Args:
            slug: Explicit slug to seed for.  When ``None`` (default), the
                slug is resolved via :func:`resolve_slug` (priority chain
                HERMES_KANBAN_TASK → HERMES_WORKSPACE → CWD).
            seed:
              * ``True`` (session start): idempotent — a hit in the in-process
                :data:`SESSION_SEED_CACHE` for the same ``(slug, state-hash)``
                short-circuits the write.
              * ``False`` (session end): upsert — the cache is still consulted
                for the exact state (no redundant re-write), but a changed
                state always upserts.
            goal / in_flight / open_kanban / blockers / next_actions:
                snapshot fields, see :func:`compose_snapshot`.

        Returns:
            ``True`` when a save() call was issued OR a hit was detected in
            source/cache; ``False`` when no save was attempted (no slug,
            no source, or the :class:`MemorySource` returned a falsy save
            result).  Callers should log the result, not raise, so a seed
            failure never breaks the rest of the hook.
        """
        # Explicit "" (empty string) means "caller looked and there is no
        # project" → no-op.  None means "you resolve it from the environment".
        # This keeps the test contract for "explicit empty slug is a noop"
        # intact while still letting ensure(None) do env-resolution.
        if slug is None:
            effective_slug = resolve_slug()
        else:
            effective_slug = _canonical_slug(slug)
        if not effective_slug:
            logger.debug("working_state_seeder: no slug — skipping seed (seed=%s)", seed)
            return False

        source = self._source
        if source is None:
            logger.debug("working_state_seeder: no memory_source — skipping seed")
            return False

        snapshot = compose_snapshot(
            effective_slug,
            goal=goal,
            in_flight=in_flight,
            open_kanban=open_kanban,
            blockers=blockers,
            next_actions=next_actions,
        )
        shash = state_hash(snapshot)
        key = f"PROJECT_{effective_slug}"
        source_file = f"PROJECT_{effective_slug}_{shash}"

        # Attach provenance / key context the MemPalace source reads
        # explicitly (see MempalaceMemorySource.save()).
        snapshot = dict(snapshot)  # shallow copy; don't mutate the composed result
        snapshot["source_file"] = source_file

        # ---- Idempotency gate (two layers) --------------------------------
        #
        # Layer 1: in-process exact-state dedup.  If we already wrote this
        # (slug, shash) pair in this process, short-circuit regardless of seed
        # mode.  This makes "repeated seed of unchanged state" a true no-op.
        cache_key = (effective_slug, shash)
        if cache_key in SESSION_SEED_CACHE:
            logger.debug(
                "working_state_seeder: cached seed for slug=%s hash=%s — skipping write",
                effective_slug, shash)
            return True

        # Layer 2: cross-instance arrival.  Only on the *first* in-process
        # write for a slug do we probe the source's own store.  Once we've
        # written at least one state for this slug in-process, the source's
        # slug-scoped key will always match the old state and the retrieve
        # probe cannot tell "changed state" from "same state" — so we skip
        # it and let the save proceed (upsert).
        if effective_slug not in _SEEN_IN_PROCESS:
            try:
                already = source.retrieve(key)
                if already is not None:
                    logger.debug(
                        "working_state_seeder: source already has key=%s — skipping write",
                        key)
                    SESSION_SEED_CACHE[cache_key] = True
                    _SEEN_IN_PROCESS[effective_slug] = shash
                    return True
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "working_state_seeder: retrieve() failed, continuing to save: %s",
                    exc)

        # The actual write.  Goes through the source's save() so the A3
        # project-stamp fires inside MempalaceMemorySource (D2 amendment) and
        # routing (wing / room) resolves via the category → map lookup.
        try:
            ok = bool(source.save(key, snapshot))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "working_state_seeder: save() raised for slug=%s: %s",
                effective_slug, exc)
            return False

        if ok:
            SESSION_SEED_CACHE[cache_key] = True
            _SEEN_IN_PROCESS[effective_slug] = shash
            logger.info(
                "working_state_seeder: seeded working-state for slug=%s (hash=%s)",
                effective_slug, shash)
        else:
            logger.debug(
                "working_state_seeder: source.save() returned falsy for slug=%s",
                effective_slug)
        return ok
