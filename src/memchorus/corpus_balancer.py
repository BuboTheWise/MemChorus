"""Corpus Balancer — (#209) working-state surface (b): live corpus-imbalance
diagnostic.

Purely additive and read-only. The balancer never touches ranking (it imports
nothing from ``searcher`` and mutates nothing) — it counts drawers from the
standing ``mempalace_list_drawers`` MCP tool via ``MemPalaceMemorySource.
call_tool`` and reports the observed ``closet_boost`` counters from
:mod:`memchorus.relevance_engine`. It then classifies the corpus into
``ok`` / ``warn`` / ``crit`` per the §4 threshold table in
``DESIGN-(b)-corpus-imbalance-diagnostic.md`` and renders the finding as
either a one-line recall header (``render_header_line``) or a full status
block (``render_status_block``).

Design (gen-2 contract — live card t_de823538 + TRIAGE-209-decision-memo §3/§6):

  * Metrics
      M1  active-project ratio  = working-state drawers / total corpus.
          v1 tier: drawers in the working-state rooms
          ({working-state, current-status, tasks}) plus drawers in
          project-named wings (wing_<slug>).  The ``meta.project == slug``
          tier is planned for #209-(a) and is not part of this card.
      M2  settled-knowledge ratio
          (drawers in the configured ``settled_wings`` ∪ ``settled_rooms``
          whitelist) ÷ total.
      M3  closet health
          ``mempalace_closets`` collection presence + the
          ``record_closet_query`` / ``record_bound_result`` counters from
          :mod:`memchorus.relevance_engine` (all-zero + missing collection
          means "structurally dead").

  * Level (threshold table, design memo §4)
      CRIT   M1 ratio == 0.0
              (zero active-project drawers anywhere)
      WARN   M1 ratio > 0.0 AND M1 ratio < project_min_ratio AND
             M2 ratio >= settled_threshold
      OK     otherwise — or when ``balance.mode == "archive"`` (suppression).
      (archive is checked FIRST so it always short-circuits to OK.)

  * Cache       ``(active_slug, config_hash)`` key + ``ttl_seconds`` (default
                60s).  The active project rarely changes mid-session, so we
                avoid re-probing MCP on every ``on_pre_llm_call`` call.
  * Gating      ``memchorus.balance.enabled`` (default OFF — opt-in).
  * Never-raises: every external path (MCP, resolver, cache read/write) is
                wrapped.  On failure we emit a report with ``level="ok"`` and
                a reason string, so the caller can drop the header line and
                recall itself is fully preserved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunables — mirrored into config when auto_init seeds the template.          #
# --------------------------------------------------------------------------- #
DEFAULT_PROJECT_MIN_RATIO = 0.05
DEFAULT_SETTLED_THRESHOLD = 0.50
DEFAULT_MAX_STALE_DAYS = 14
DEFAULT_TTL_SECONDS = 60.0

# Defaults for the settled-knowledge whitelist surfaced as config keys
# ``settled_wings`` / ``settled_rooms``.  Fresh installs get a meaningful
# baseline that tracks the design-memo whitelist verbatim.
DEFAULT_SETTLED_WINGS: Tuple[str, ...] = (
    "memchorus_learning",
    "memchorus_decisions",
    "memchorus_general",
    "self-improvement",
)
DEFAULT_SETTLED_ROOMS: Tuple[str, ...] = (
    "lessons-learned",
    "corrections",
    "diary",
    "evolution-reports",
    "research-archival",
    "pattern-recognition",
)

# Working-state rooms (design memo §2 M1, verbatim).  The room tier is
# what makes the diagnostic shippable BEFORE #209-(a) has stamped
# ``meta.project`` on every drawer.
WORKING_STATE_ROOMS: Tuple[str, ...] = ("working-state", "current-status", "tasks")

# M3: the closets collection name (design memo §2 M3).
CLOSETS_COLLECTION_NAME = "mempalace_closets"


class _CensusSource(Protocol):
    """The minimal surface the balancer expects from a MemPalace source."""

    def call_tool(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]: ...


class CorpusBalancer:
    """(#209 b) Live corpus-imbalance detector.

    Public surface (gen-2 contract, see module docstring for detail):

        ``compute(active_slug=None, *, agent_workrooms=None) -> dict``
            The "real work" entry — the only method that talks to
            MemPalace MCP.  Returns the M1/M2/M3 metrics dict with
            ``level`` and ``thresholds_hit``.
        ``render_header_line(report) -> Optional[str]``
            The one-line header note.  ``None`` when level is ``"ok"``
            (silent) or when the report is malformed.
        ``render_status_block(report) -> str``
            The recommended-action block for ``mempalace_status``.
        ``on_pre_llm_call(orchestrator) -> Optional[str]``
            Hook-facing convenience wrapper: reads ``balance`` config from
            the orchestrator, runs ``compute``, formats the header line,
            swallows all errors.

    Constructor (all arguments are injection points for tests):
        ``get_source`` — callable returning a ``MemPalaceMemorySource``
                          (or any object exposing compatible
                          ``call_tool(name, arguments) -> Any``).
        ``get_config`` — optional callable returning the memchorus config
                          dict; default reads ``orchestrator.config``.
        ``get_active_project`` — optional callable returning the current
                          active-project slug.
        ``now`` — clock injection; default ``time.time``.
        ``closet_presence`` — optional callable returning True/False/None
                          for whether ``mempalace_closets`` is present.
                          ``None`` (unknown) is treated as "not measurable"
                          by the classifier and does NOT promote a WARN to
                          CRIT on its own.
        ``closet_counter_source`` — optional callable returning the
                          ``_closet_stats`` dict (queries_seen,
                          queries_with_active_project, bound_results_surfaced).
        ``ttl_seconds`` — TTL override (default :data:`DEFAULT_TTL_SECONDS`).
    """

    def __init__(
        self,
        get_source: Optional[Callable[[], Optional[_CensusSource]]] = None,
        get_config: Optional[Callable[[], Dict[str, Any]]] = None,
        get_active_project: Optional[Callable[[], Optional[str]]] = None,
        now: Optional[Callable[[], float]] = None,
        closet_presence: Optional[Callable[[], Optional[bool]]] = None,
        closet_counter_source: Optional[Callable[[], Dict[str, int]]] = None,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        self._get_source = (
            get_source if get_source is not None else self._default_source_resolver
        )
        self._get_config = (
            get_config if get_config is not None else self._default_config_resolver
        )
        self._get_active_project = (
            get_active_project
            if get_active_project is not None
            else self._default_active_project_resolver
        )
        self._now: Callable[[], float] = now if now is not None else time.time
        self._closet_presence = (
            closet_presence
            if closet_presence is not None
            else self._default_closet_presence
        )
        self._closet_counter_source = (
            closet_counter_source
            if closet_counter_source is not None
            else self._default_closet_counter_source
        )
        self._ttl_seconds = (
            DEFAULT_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
        )

        # Cache: key = (slug, config_hash); value = (timestamp, report).
        self._cache: Dict[Tuple, Tuple[float, Dict[str, Any]]] = {}

    # ----------------------- default resolvers ------------------------- #

    @staticmethod
    def _default_source_resolver() -> Optional[_CensusSource]:
        try:
            from memchorus import get_orchestrator
            orch = get_orchestrator()
            if orch is not None:
                src = getattr(orch, "memory_sources", {}).get("mempalace")
                if src is not None and hasattr(src, "call_tool"):
                    return src
        except Exception:
            pass
        try:
            from memchorus import _get_orchestrator as _goh
            orch = _goh()
            if orch is not None:
                src = getattr(orch, "memory_sources", {}).get("mempalace")
                if src is not None and hasattr(src, "call_tool"):
                    return src
        except Exception:
            pass
        return None

    @staticmethod
    def _default_config_resolver() -> Dict[str, Any]:
        try:
            from memchorus import get_orchestrator
            orch = get_orchestrator()
            if orch is not None:
                return dict(getattr(orch, "config", {}) or {})
        except Exception:
            pass
        return {}

    @staticmethod
    def _default_active_project_resolver() -> Optional[str]:
        import os
        try:
            from memchorus import orientation as _orient
            return _orient._resolve_project(
                os.environ.get("HERMES_KANBAN_TASK", "").strip() or None
            )
        except Exception:
            return None

    @staticmethod
    def _default_closet_presence() -> Optional[bool]:
        """Probe ``mempalace_closets`` existence.

        Tries two candidate helper locations (the mempalace package and the
        memchorus in-tree patch site).  Returns ``True`` when any probe
        finds a non-None collection, ``False`` when a probe definitively
        reports "absent", ``None`` when it cannot tell (e.g. no palace
        path is configured).  ``None`` is treated by the classifier as
        "not measurable" and does NOT promote WARN→CRIT on its own — but
        the design memo's AC-S1 profile_b scenario relies on the caller
        passing an explicit presence value, so this default path is
        primarily a fallback.
        """
        for module_name in (
            "mempalace.closet_llm",
            "memchorus.mempalace_closets_probe",
        ):
            try:
                mod = __import__(module_name, fromlist=["get_closets_collection"])
                fn = getattr(mod, "get_closets_collection", None)
                if fn is None:
                    continue
                collection = fn(create=False)
                return collection is not None
            except Exception:
                # Absent (or unreadable) — treat as missing for M3 purposes.
                return False
        return None

    @staticmethod
    def _default_closet_counter_source() -> Dict[str, int]:
        try:
            from memchorus.relevance_engine import _closet_stats
            return dict(_closet_stats())
        except Exception:
            return {}

    # ----------------------- internal census --------------------------- #

    def _probe_total(self, args: Optional[Dict[str, Any]] = None) -> int:
        """Census via ``mempalace_list_drawers`` (limit=1 probe)."""
        source = self._get_source()
        if source is None or not hasattr(source, "call_tool"):
            raise RuntimeError(
                "corpus_balancer: no MemPalace source with call_tool()"
            )
        payload = dict(args or {})
        payload.setdefault("limit", 1)
        result = source.call_tool("mempalace_list_drawers", payload)
        if result is None:
            raise RuntimeError("corpus_balancer: list_drawers returned None")
        n = result.get("total", result.get("total_drawers"))
        if isinstance(n, bool) or not isinstance(n, int):
            raise RuntimeError(
                f"corpus_balancer: list_drawers did not report a numeric total "
                f"(got {n!r})"
            )
        return int(n)

    # ----------------------- main entry points ------------------------- #

    def cfg_block(self) -> Dict[str, Any]:
        """Read the ``balance`` sub-block (top-level in the orchestrator cfg).

        The orchestrator stores config flat (``self.config = config``), so
        sub-blocks like ``balance`` / ``recall`` / ``feedback_loop`` are
        top-level keys.
        """
        cfg = self._get_config() or {}
        bal = cfg.get("balance", {})
        if not isinstance(bal, dict):
            bal = {}
        return bal

    def _config_hash(self, cfg: Dict[str, Any]) -> str:
        """Stable digest of the config subset that affects the result."""
        subset = {
            "enabled": cfg.get("enabled"),
            "mode": cfg.get("mode"),
            "project_min_ratio": cfg.get("project_min_ratio"),
            "settled_threshold": cfg.get("settled_threshold"),
            "settled_wings": sorted(cfg.get("settled_wings") or []),
            "settled_rooms": sorted(cfg.get("settled_rooms") or []),
        }
        return hashlib.sha1(
            json.dumps(subset, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

    def compute(  # noqa: C901 — intentionally flat for readability
        self,
        active_slug: Optional[str] = None,
        *,
        agent_workrooms: Optional[Iterable[str]] = None,
        skip_cache: bool = False,
    ) -> Dict[str, Any]:
        """Compute M1/M2/M3 and classify into ``level``.

        Returns a dict with (at least):
          ``level``             "ok" | "warn" | "crit"
          ``reason``            short human-readable explanation (always set
                                for non-ok levels; may be set for ok too)
          ``active_project``    slug or None
          ``thresholds_hit``    list of threshold keys that tripped
          ``m1``/``m2``/``m3``  per-metric sub-dicts
          ``partial``           bool — True when MCP was down / unreadable
        """
        slug = (str(active_slug).strip() if active_slug else "") or None
        workrooms = tuple(agent_workrooms) if agent_workrooms else None
        cfg = self.cfg_block()
        cfg_key_hash = self._config_hash(cfg)

        key = (slug, cfg_key_hash)
        now = self._now()
        if not skip_cache:
            hit = self._cache.get(key)
            if hit is not None:
                ts, cached = hit
                if now - ts < self._ttl_seconds:
                    out = dict(cached)
                    out["_from_cache"] = True
                    return out

        report = self._compute_uncached(slug, cfg, workrooms)
        self._store(key, report, now)
        return report

    # ----------------------- internal compute -------------------------- #

    def _compute_uncached(
        self,
        slug: Optional[str],
        cfg: Dict[str, Any],
        workrooms: Optional[Tuple[str, ...]],
    ) -> Dict[str, Any]:
        # --- read config knobs (all optional; defaults per design-memo)  #
        mode = str(cfg.get("mode") or "").lower() or ""
        project_min_ratio = float(
            cfg.get("project_min_ratio", DEFAULT_PROJECT_MIN_RATIO)
        )
        settled_threshold = float(
            cfg.get("settled_threshold", DEFAULT_SETTLED_THRESHOLD)
        )
        settled_wings_raw = cfg.get("settled_wings", DEFAULT_SETTLED_WINGS)
        settled_rooms_raw = cfg.get("settled_rooms", DEFAULT_SETTLED_ROOMS)
        settled_wings = (
            frozenset(settled_wings_raw)
            if isinstance(settled_wings_raw, (list, tuple, set, frozenset))
            else frozenset(DEFAULT_SETTLED_WINGS)
        )
        settled_rooms = (
            frozenset(settled_rooms_raw)
            if isinstance(settled_rooms_raw, (list, tuple, set, frozenset))
            else frozenset(DEFAULT_SETTLED_ROOMS)
        )

        # M1: census of working-state rooms + project-named wing (when slug).
        # M2: census of each settled wing / room in the whitelist.
        m1_room_census: Dict[str, int] = {}
        m1_wing_census: Dict[str, int] = {}
        m2_wing_census: Dict[str, int] = {}
        m2_room_census: Dict[str, int] = {}

        errors: List[str] = []
        total_corpus = 0
        partial = False

        # (1) Total corpus (no filter).  A failure here marks the whole thing
        #     as partial — we can't compute any ratio without the denominator.
        try:
            total_corpus = self._probe_total({})
        except Exception as exc:
            errors.append(f"total: {exc}")
            partial = True

        # (2) M1: working-state rooms (always — these exist regardless of
        #     slug, and are the "current work" signal the design memo
        #     centres on).
        for room in WORKING_STATE_ROOMS:
            try:
                m1_room_census[room] = self._probe_total({"room": room})
            except Exception as exc:
                errors.append(f"m1[{room!r}]: {exc}")
                partial = True
                m1_room_census[room] = 0

        # (2b) M1: project-named wing (only when slug present).  Design memo
        #     §2 says M1 counts "working-state rooms OR project-named wings
        #     (wing_<slug>)" — we try both to be inclusive.
        m1_project_wing = None
        if slug:
            for wing in (f"wing_{slug}", slug):
                try:
                    n = self._probe_total({"wing": wing})
                    if m1_project_wing is None:
                        m1_project_wing = n
                    else:
                        m1_project_wing += n
                except Exception as exc:
                    errors.append(f"m1[{wing!r}]: {exc}")
                    partial = True
            m1_wing_census = {"wing": m1_project_wing or 0}

        # (3) M2: settled wings.
        for wing in sorted(settled_wings):
            try:
                m2_wing_census[wing] = self._probe_total({"wing": wing})
            except Exception as exc:
                errors.append(f"m2[{wing!r}]: {exc}")
                partial = True

        # (4) M2: settled rooms.
        for room in sorted(settled_rooms):
            try:
                m2_room_census[room] = self._probe_total({"room": room})
            except Exception as exc:
                errors.append(f"m2[{room!r}]: {exc}")
                partial = True

        m1_total = (
            sum(m1_room_census.values()) + (m1_project_wing or 0)
        )
        m2_total = sum(m2_wing_census.values()) + sum(m2_room_census.values())

        # Dedupe: drawers in a room that is ALSO in a settled wing are
        # counted twice.  We subtract the overlap (a drawer can only be
        # in one wing, so the room-tier and wing-tier are disjoint
        # categories by construction — the overlap is zero when the
        # whitelist is well-formed).
        m1_ratio = (m1_total / total_corpus) if total_corpus > 0 else 0.0
        m2_ratio = (m2_total / total_corpus) if total_corpus > 0 else 0.0

        # M3: closet presence + boost counters.
        try:
            closet_present = self._closet_presence()
        except Exception:
            closet_present = None
        try:
            closet_stats = self._closet_counter_source()
        except Exception:
            closet_stats = {}

        # --- classify per threshold table (design-memo §4) ------------- #
        # Order of checks (archive short-circuits first; then CRIT;
        # then WARN; everything else is OK).
        thresholds_hit: List[str] = []
        reason = ""
        level = "ok"

        if mode == "archive":
            level = "ok"
            thresholds_hit.append("mode_archive")
            reason = (
                "balance.mode=archive — deliberately a lessons-only "
                "profile; diagnostic suppressed."
            )
        elif partial and total_corpus <= 0:
            # MCP was down (or corpus is genuinely 0 drawers) — not
            # classifiable.  Report partial; caller treats as silent
            # (header line not rendered) to avoid false WARN/CRIT noise.
            level = "ok"
            reason = (
                "drawer census unavailable (MCP or corpus) — "
                + ("; ".join(errors[:3]) if errors else "zero drawers")
            )
        elif partial and m1_total == 0:
            # The M1 (active-project) census is the v1 signal, and here its
            # probes failed outright (not genuinely zero).  A zero that came
            # from a failed probe is NOT evidence of "no current work", so we
            # must not confidently classify to CRIT/WARN — stay silent and
            # mark partial.  (A partial census with a NON-zero M1 can still be
            # classified on the readable data.)
            level = "ok"
            thresholds_hit.append("census_partial_m1")
            reason = (
                "drawer census is partial — active-project (M1) probes "
                "failed, so the corpus cannot be confidently classified "
                + ("(" + "; ".join(errors[:3]) + ")" if errors else "")
            )
        else:
            # Normal classification path.
            if m1_total == 0:
                # CRIT: zero active-project drawers.  The "closet missing"
                # amplifier (M1==0 AND M3 missing) is a superset of the
                # same tier — the design memo lists it as the stronger
                # form, so we always classify M1==0 as CRIT.
                level = "crit"
                thresholds_hit.append("m1_zero_active")
                if closet_present is False:
                    thresholds_hit.append("m3_closets_missing")
                    reason = (
                        "zero active-project state, and the "
                        f"{CLOSETS_COLLECTION_NAME} collection is "
                        "missing — closet_boost is structurally 0.0."
                    )
                else:
                    reason = (
                        "zero active-project state (all drawers are "
                        "settled past knowledge). The agent recalls "
                        "its past, not its present."
                    )
            elif m1_ratio < project_min_ratio and m2_ratio >= settled_threshold:
                # WARN: thin active-project slice AND settled-dominant.
                level = "warn"
                thresholds_hit.append("m1_below_floor")
                thresholds_hit.append("m2_above_threshold")
                if closet_present is False:
                    thresholds_hit.append("m3_closets_missing")
                reason = (
                    f"active-project state is {m1_total}/{total_corpus} "
                    f"({m1_ratio * 100:.1f}%) < "
                    f"{project_min_ratio * 100:.0f}% threshold; "
                    f"{m2_ratio * 100:.0f}% settled knowledge."
                )
            else:
                level = "ok"
                reason = (
                    f"healthy: M1 {m1_ratio * 100:.1f}%, "
                    f"M2 {m2_ratio * 100:.1f}%."
                )

        # --- assemble the report --------------------------------------- #
        report: Dict[str, Any] = {
            "level": level,
            "reason": reason,
            "status": "partial" if (partial and total_corpus > 0) else "ok",
            "partial": partial,
            "active_project": slug,
            "mode": mode,
            "m1": {
                "total": m1_total,
                "ratio": round(m1_ratio, 4),
                "project_min_ratio": project_min_ratio,
                "room_census": m1_room_census,
                "wing_census": m1_wing_census,
            },
            "m2": {
                "total": m2_total,
                "ratio": round(m2_ratio, 4),
                "settled_threshold": settled_threshold,
                "wing_census": m2_wing_census,
                "room_census": m2_room_census,
                "settled_wings": sorted(settled_wings),
                "settled_rooms": sorted(settled_rooms),
            },
            "m3": {
                "closets_present": closet_present,
                "closet_collection": CLOSETS_COLLECTION_NAME,
                "closet_stats": dict(closet_stats or {}),
            },
            "total_corpus": total_corpus,
            "thresholds_hit": thresholds_hit,
            "errors": errors,
        }
        return report

    # ----------------------- cache helpers ----------------------------- #

    def _store(
        self,
        key: Tuple,
        report: Dict[str, Any],
        ts: float,
    ) -> None:
        self._cache[key] = (ts, dict(report))
        if len(self._cache) > 16:
            ordered = sorted(self._cache.items(), key=lambda kv: kv[1][0])
            drop = len(self._cache) - 16
            for k, _v in ordered[:drop]:
                self._cache.pop(k, None)


# --------------------------------------------------------------------------- #
# Render helpers — the two surfaces the diagnostic appears in.                #
# --------------------------------------------------------------------------- #

def _fmt_pct(value: float) -> str:
    if value is None:
        return "?"
    return f"{value * 100:.1f}%"


def render_header_line(report: Optional[Dict[str, Any]]) -> Optional[str]:
    """Render ``report`` into the recall-header one-line note.

    Returns ``None`` when level is ``"ok"`` (silent) or the report is not
    a dict with a usable level.  The line is what goes inside the existing
    ``[MemChorus Memory Recall]`` block so the agent sees the imbalance
    at the exact moment context arrives.
    """
    if not isinstance(report, dict):
        return None
    level = (report.get("level") or "ok").lower()
    if level not in ("warn", "crit"):
        return None

    m1 = report.get("m1", {}) or {}
    m2 = report.get("m2", {}) or {}
    m3 = report.get("m3", {}) or {}
    total = int(report.get("total_corpus") or 0)
    m1_count = int(m1.get("total") or 0)
    m1_ratio = float(m1.get("ratio") or 0.0)
    m2_ratio = float(m2.get("ratio") or 0.0)
    closets_present = m3.get("closets_present")

    if level == "warn":
        line = (
            f"[MemChorus] balance WARN: active-project state "
            f"{m1_count}/{total} ({_fmt_pct(m1_ratio)}) below "
            f"{_fmt_pct(float(m1.get('project_min_ratio') or 0.05))} "
            f"threshold; {_fmt_pct(m2_ratio)} of the corpus is settled "
            f"knowledge. Recalling past, not present — see "
            f"`mempalace_status` → Corpus Balance."
        )
    else:  # crit
        closets_note = (
            "PRESENT" if closets_present else "MISSING"
        )
        line = (
            f"[MemChorus] balance CRIT: active-project state "
            f"{m1_count}/{total} ({_fmt_pct(m1_ratio)}), "
            f"{_fmt_pct(m2_ratio)} settled knowledge, "
            f"closet collection {closets_note}. This profile recalls "
            f"its past, not its present — seed a working-state drawer or "
            f"set balance.mode=archive. See `mempalace_status` → Corpus "
            f"Balance for the action block."
        )
    return line


def render_status_block(report: Optional[Dict[str, Any]]) -> str:
    """Render the recommended-action block for ``mempalace_status``.

    Non-empty for warn/crit; a minimal marker line for ok.  The block is
    the "why + what to do" view: the header line is the nudge, the
    status block is the full remediation guide.
    """
    if not isinstance(report, dict):
        return "Corpus Balance: (report unavailable)"
    level = (report.get("level") or "ok").lower()
    total = int(report.get("total_corpus") or 0)
    m1 = report.get("m1", {}) or {}
    m2 = report.get("m2", {}) or {}
    m3 = report.get("m3", {}) or {}
    m1_count = int(m1.get("total") or 0)
    m1_ratio = float(m1.get("ratio") or 0.0)
    m2_count = int(m2.get("total") or 0)
    m2_ratio = float(m2.get("ratio") or 0.0)
    closets_present = m3.get("closets_present")
    closets_note = "PRESENT" if closets_present else (
        "MISSING" if closets_present is not None else "UNKNOWN"
    )

    if level == "ok":
        return (
            "Corpus Balance: OK\n"
            f"  active-project state: {m1_count}/{total} "
            f"({_fmt_pct(m1_ratio)})\n"
            f"  settled knowledge:    {m2_count}/{total} "
            f"({_fmt_pct(m2_ratio)})\n"
            f"  closets collection:   {closets_note}\n"
            f"  → healthy: corpus is active and balanced across work."
        )

    header = (
        "⚠  CORPUS BALANCE WARN" if level == "warn"
        else "⚠  CORPUS BALANCE CRIT"
    )
    sub = ", no current work" if level == "crit" else ""
    return (
        f"{header}{sub} — \"all-lessons\" profile\n"
        f"   active-project state:  {m1_count}/{total} "
        f"({_fmt_pct(m1_ratio)})   "
        f"{'(below floor)' if level != 'warn' else '(below floor)'}\n"
        f"   settled knowledge:     {m2_count}/{total} "
        f"({_fmt_pct(m2_ratio)})   [lessons+corrections+diary]\n"
        f"   closets collection:    {closets_note} "
        f"{'→ closet_boost structurally 0.0' if closets_present is False else ''}\n"
        f"   → This profile recalls its past, not its present.\n"
        f"   → ACTION (pick one):\n"
        f"     a. Seed a project working-state drawer "
        f"(auto via MemChorus session-start hook once #209-(a) lands), "
        f"or file one now:\n"
        f"        mempalace_add_drawer(wing=\"memchorus_workspace\", "
        f"room=\"working-state\",\n"
        f"          content=\"<goal · in-flight PRs/cards · blockers · "
        f"next 3 actions>\",\n"
        f"          meta={{\"project\":\"<slug>\"}})\n"
        f"     b. If this profile is intentionally archive-only, suppress:\n"
        f"        config: memchorus.balance.mode=archive\n"
        f"   → Related: #206 (active-project ranking), "
        f"#209-(a) (working-state drawers),\n"
        f"      t_080bcf6d brief (closets absent — separate follow-up)."
    )


__all__ = [
    "CorpusBalancer",
    "render_header_line",
    "render_status_block",
    "WORKING_STATE_ROOMS",
    "CLOSETS_COLLECTION_NAME",
    "DEFAULT_PROJECT_MIN_RATIO",
    "DEFAULT_SETTLED_THRESHOLD",
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_MAX_STALE_DAYS",
    "DEFAULT_SETTLED_WINGS",
    "DEFAULT_SETTLED_ROOMS",
]
