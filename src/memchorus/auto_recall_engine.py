"""
AutoRecallEngine: Automatic context injection at decision points.

Wires behavioral enforcement into the orchestrator search pipeline by detecting
decision points from BehavioralTrigger and automatically querying MemoryOrchestrator
search to inject retrieved context inline — no manual recall needed.

Decision point -> query mapping (deterministic):

  PLANNING_START       -> "past planning patterns architecture decisions strategy"
  TOOL_CALL_INTENT     -> "tool usage history command conventions domain guidance"
  POST_ACTION_COMPLETE -> "post-action learnings outcomes results"
  ERROR_STATE          -> "errors recovery patterns failure modes known issues"

Acceptance criteria:

  AC-1: Constructor accepts MemoryOrchestrator + BehavioralTrigger.
  AC-2: on_decision_point returns deterministic queries per DP type.
  AC-3: Early termination caching prevents redundant queries within window.
  AC-4: Graceful degradation returns [] when orchestrator unavailable.
  AC-5: Result count hard-limited to 3.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional

from memchorus.behavioral_trigger import DecisionPoint, DetectedPoint  # type: ignore[import-not-found]
from memchorus.recall_config import (  # type: ignore[import-not-found]
    BaseDimensionWeights,
    RECALL_CONFIG,
    estimate_tokens,
)

# Canonical import surface for the select() contract (spec §4, card #223).
__all__ = [
    "AutoRecallEngine",
    "BaseDimensionWeights",
    "DecisionPoint",
    "DetectedPoint",
    "SelectionResult",
    "SelectedCandidate",
    "TaskContext",
    "estimate_tokens",
    "select",
]


logger = logging.getLogger(__name__)

# Item budget K for *implied* (decision-point / auto) recall — read from the
# single named config root (spec §4.3).  Decision-point recall is by
# definition "implied", so its hard cap is ``k_implied`` (default 3).  This is
# the ONE place the implied item budget is named in this module; the literal
# ``3`` that used to be scattered through the limit logic below now derives
# from here, so drift is caught at a single site (see
# ``tests/test_recall_config_root.py``).
_IMPLIED_ITEM_BUDGET: int = RECALL_CONFIG.k_implied


# ---------------------------------------------------------------------------
# MC-001 recursion guard sentinel (module-level)
# ---------------------------------------------------------------------------

_REC_GUARD: bool = False  # flipped during enforce() to catch re-entry


# ---------------------------------------------------------------------------
# Query templates per decision point type
# ---------------------------------------------------------------------------

# GAP P0-3 FIX (2026-07-19): Expanded query templates to cover real-world recall needs.
# The original templates were engineering-focused, missing key terms from actual stored
# memories like user preferences, project conventions, debug notes, etc.
_QUERY_MAP: Dict[DecisionPoint, Optional[str]] = {
    DecisionPoint.PLANNING_START: (
        "past planning patterns architecture decisions strategy notes "
        "project organization conventions documentation standards workflow"
    ),
    DecisionPoint.TOOL_CALL_INTENT: (
        "tool usage history command conventions domain-specific guidance "
        "preferences user context setup configuration environment "
        "debug findings verification testing procedures scripts"
    ),
    DecisionPoint.POST_ACTION_COMPLETE: (
        "post-action learnings outcomes results decisions made changes "
        "completed tasks progress milestones reviews improvements"
    ),
    DecisionPoint.ERROR_STATE: (
        "errors recovery patterns failure modes known issues bugs fixes "
        "troubleshooting diagnostic root cause debugging steps workarounds"
    ),
    DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION: (
        "synthesis analysis findings key insight understanding learned important "
        "patterns review summary conclusions takeaways documentation research"
    ),
    # IMPL #163.2 — sentinel: PROJECT_START is a *keyed* record lookup, not a
    # semantic query. ``None`` means "resolve the project:<name> record via
    # orchestrator.resolve_project_record(); do not run orchestrator.search()".
    # The dispatch branch in ``on_decision_point`` catches this DP **before**
    # ``_extract_query`` is consulted, so the sentinel is defensive only
    # (spec §4.2).
    DecisionPoint.PROJECT_START: None,
}


# ---------------------------------------------------------------------------
# Cache entry for early-termination caching
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    result: List[Dict[str, Any]]
    timestamp: float  # time.time() when the cache was populated


# ---------------------------------------------------------------------------
# AutoRecallEngine
# ---------------------------------------------------------------------------


class AutoRecallEngine:
    """Automatically queries memory sources at detected decision points.

    Constructor arguments:

      orchestrator (MemoryOrchestrator): source for ``search(query, limit)``
      trigger (BehavioralTrigger): used only by the public ``fire_for_text``
        convenience method — it delegates to ``trigger.fire(text)`` internally.
    """

    def __init__(
        self,
        orchestrator: Any,  # MemoryOrchestrator (no type-signal needed)
        trigger: Any,  # BehavioralTrigger
        cache_ttl: float = 5.0,  # seconds before cache expires per DP type
    ) -> None:
        self._orchestrator = orchestrator
        self._trigger = trigger
        self._cache_ttl = cache_ttl

        # Per-type cache: maps DecisionPoint value (int) -> _CacheEntry
        self._cache: Dict[int, _CacheEntry] = {}

        # MC-001 re-entry guard — flipped during enforce() to block recursive calls
        self._in_enforcement_recall = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_decision_point(self, decision_point: DetectedPoint) -> List[Dict[str, Any]]:
        """Retrieve context for a single *decision_point* from the orchestrator.

        Args:
            decision_point: A DetectedPoint emitted by BehavioralTrigger.

        Returns:
            Up to the implied item budget (K = ``RECALL_CONFIG.k_implied``,
            default 3) highest-relevance search results, or ``[]`` on degradation.
        """
        global _REC_GUARD

        # MC-001 guard: block recursive entry during enforcement cycle
        if _REC_GUARD or self._in_enforcement_recall:
            logger.debug(
                "AutoRecallEngine: recursion guard blocked re-entry for %s",
                decision_point.type.name,
            )
            return []

        dp_type = decision_point.type

        # Early-termination cache check
        cached = self._get_cached(dp_type)
        if cached is not None:
            return cached

        # Activate recursion guard before doing work
        _REC_GUARD = True
        self._in_enforcement_recall = True
        try:
            # IMPL #163.2 (spec §4.1) — PROJECT_START is a *keyed* record
            # resolution, NOT a free-text semantic query.  Dispatch it to the
            # orchestrator's ``resolve_project_record(name)`` API so the ranked
            # search path is never consulted with the ``None`` sentinel (the
            # bug this branch prevents: ``_do_search(None)`` falling through
            # to ``orchestrator.search(None, limit=_IMPLIED_ITEM_BUDGET)``).
            if dp_type is DecisionPoint.PROJECT_START:
                results = self._do_resolve_project_record(decision_point)
            else:
                query = self._extract_query(dp_type)
                results = self._do_search(query)

            # Harden: enforce the implied item budget (K) regardless of
            # orchestrator output.  K for implied recall is the named config
            # key (spec §4.3), not a scattered literal.
            results = results[:_IMPLIED_ITEM_BUDGET]

            # Stash in cache
            self._cache[dp_type.value] = _CacheEntry(
                result=list(results), timestamp=time.time()
            )

            return results
        finally:
            # Always deactivate guard, even on exception
            _REC_GUARD = False
            self._in_enforcement_recall = False

    def fire_for_text(self, text: str) -> Dict[str, List[Dict[str, Any]]]:
        """Convenience wrapper: call BehavioralTrigger on *text*, then retrieve
        context for every detected decision point.

        Returns a dict keyed by decision-point type *name* (str, via
        ``point.type.name``) -> the result list for that point.
        """
        if self._trigger is None:
            logger.warning("AutoRecallEngine has no trigger; cannot fire_for_text")
            return {}

        points = self._trigger.fire(text)
        # Group by type so rapid-fire returns consistent cache hits
        output: Dict[str, List[Dict[str, Any]]] = {}
        for point in points:
            key = point.type.name
            output[key] = self.on_decision_point(point)
        return output

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_query(self, dp_type: DecisionPoint) -> str:
        """Return the deterministic search query for *dp_type*."""
        return _QUERY_MAP.get(dp_type, "")

    def _do_search(self, query: str) -> List[Dict[str, Any]]:
        """Call orchestrator.search() with graceful degradation."""
        if query == "":
            logger.warning(
                "AutoRecallEngine: no query defined for this decision point type"
            )
            return []

        try:
            results = self._orchestrator.search(query, limit=_IMPLIED_ITEM_BUDGET)
        except Exception as exc:
            logger.warning(
                "AutoRecallEngine: orchestrator search failed — returning empty list. %s",
                exc,
            )
            return []

        if not results:
            logger.warning(
                "AutoRecallEngine: search returned no results for query '%s'", query
            )
            return []

        return results

    def _do_resolve_project_record(self, point: DetectedPoint) -> List[Dict[str, Any]]:
        """Resolve the *keyed* project record for a PROJECT_START decision point.

        Returns the §3.3 record contract as a **1-element list** so the caller's
        ``List[Dict]`` shape and the ``[:_IMPLIED_ITEM_BUDGET]`` hard-limit are
        preserved.  Graceful degradation: a missing/blank project name, a missing
        orchestrator API, or any exception all return ``[]`` — never an escape
        (spec §4.2/§4.5).
        """
        name = getattr(point, "project_name", None)
        if not name:
            # Fall back to the matched keyword (e.g. a project name surfaced in
            # a session-start banner), stripping any ``project:`` prefix.
            name = getattr(point, "matched_keyword", None)
        if not name or not str(name).strip():
            logger.warning(
                "AutoRecallEngine: PROJECT_START has no project_name to resolve — "
                "returning empty list (no keyed lookup possible)"
            )
            return []
        name = str(name).strip()
        # resolve_project_record strips its own ``project:`` prefix, but it can
        # also accept a bare slug (spec §3.1).  Ensure a bare slug is keyed so
        # normalize_project_key always receives the canonical ``project:<slug>``
        # form.  A name that is *already* keyed is left untouched.
        if name and not name.lower().startswith("project:"):
            name = "project:" + name

        resolver = getattr(self._orchestrator, "resolve_project_record", None)
        if not callable(resolver):
            logger.warning(
                "AutoRecallEngine: orchestrator has no resolve_project_record API — "
                "degrading PROJECT_START to empty list"
            )
            return []
        try:
            record = resolver(name)
        except Exception as exc:
            logger.warning(
                "AutoRecallEngine: resolve_project_record failed for '%s' — "
                "returning empty list. %s",
                name,
                exc,
            )
            return []
        if not isinstance(record, dict):
            logger.warning(
                "AutoRecallEngine: resolve_project_record returned %s (expected dict) — "
                "degrading to empty list",
                type(record).__name__,
            )
            return []
        return [record]

    def _get_cached(self, dp_type: DecisionPoint) -> Optional[List[Dict[str, Any]]]:
        """Return cached context if the same DP fired within cache_ttl; else None."""
        entry = self._cache.get(dp_type.value)
        if entry is None:
            return None
        if time.time() - entry.timestamp < self._cache_ttl:
            return list(entry.result)  # defensive copy
        # Expired — remove stale entry
        del self._cache[dp_type.value]
        return None

    def clear_cache(self) -> None:
        """Remove all cached entries."""
        self._cache.clear()


# ===========================================================================
# select() — task-aware selection  (spec §4, card #223, pillar 1)
# ===========================================================================
#
# A pure, deterministic, LLM-free scorer that chooses the top-K candidates for
# the current turn.  It is deliberately *separate* from the legacy
# ``RelevanceScorer`` (quality/recency/source) because that model ranks by
# stored metadata + recency, not by the current task.  ``select()`` composes
# the five spec §4.2 base dimensions (each in [0,1], weights from
# ``RECALL_CONFIG.base_weights``, Σ = 1.0), then applies two additive boosts
# (bound-project closet + working-state sub-signal) and enforces the
# token budget.  No LLM, no MCP, no orchestrator is required — the input is a
# static ``candidates`` list and a static ``TaskContext``.
#
# Boosts are guaranteed non-negative, so a boost can only *raise* a candidate
# (final >= base) and never reorder an item below one with a higher base score.
# Out of scope here (deferred to pillar 2/3 cards): the temporal validity gate
# (KG ``as_of``/supersession) and the augmentation slot (tunnel/diary/event).

# Neutral fallbacks (in [0,1]) when a dimension cannot be computed or the task
# gives no guidance for it.
_RECENCY_NEUTRAL = 0.5
_DOMAIN_NEUTRAL = 0.5
_SOURCE_UNKNOWN = 0.5

# Source-channel prior — mirrored from ``RelevanceScorer.__init__`` default so
# the two never disagree.  Normalised by the max prior (0.7) to land in [0,1].
_DEFAULT_SOURCE_PRIORS: Dict[str, float] = {"hermes_default": 0.7, "mempalace": 0.3}

# Strength (calibration) normalisation: ``boost_factor_for_key`` returns
# ``[0.5, 3.0]`` (1.0 when no history).  We map that linearly onto ``[0, 1]``
# so the *neutral* factor (1.0) sits at 0.2 and high-utility memories (3.0) max
# out at 1.0 — a monotonic, bounded transform that keeps the [0,1] [0,1]
# contract.
_STRENGTH_MIN = 0.5
_STRENGTH_MAX = 3.0


@dataclass
class TaskContext:
    """The current-turn context ``select()`` scores *against*.

    ``query`` is the free-text task signal (the decision being made right now).
    ``active_project`` drives the closet (bound-project) boost.  ``as_of`` is
    the reference timestamp for the recency dimension.  ``domain`` is optional —
    when set it drives the domain_relevance dimension (1.0 on match, 0.0
    otherwise); when ``None`` that dimension is neutral (0.5) for every
    candidate.  ``base_weights`` optionally overrides the canonical weights
    (defaults to ``RECALL_CONFIG.base_weights``) so callers can test channel
    variants without touching the config root.
    """

    query: str
    active_project: Optional[str] = None
    as_of: Optional[float] = None
    domain: Optional[str] = None
    recency_tau_days: float = RECALL_CONFIG.recency_tau_days
    base_weights: Optional[BaseDimensionWeights] = None


@dataclass
class SelectedCandidate:
    """One chosen candidate with its explainable score components.

    ``breakdown`` holds the five *raw* base dimensions (each in [0,1]).
    ``base_score`` is the weighted sum of those (Σweights = 1.0).  ``boosts``
    is the labelled set of additive boosts that were applied.  ``final_score``
    is ``base_score + sum(boosts.values())``.  ``tokens`` is the injection cost
    of this candidate's content.
    """

    result: Dict[str, Any]
    base_score: float
    boosts: Dict[str, float]
    final_score: float
    breakdown: Dict[str, float]
    tokens: int


@dataclass
class SelectionResult:
    """The ranked, budget-bounded output of ``select()``."""

    chosen: List[SelectedCandidate]
    total_tokens_used: int
    total_tokens_budget: int


# ---------------------------------------------------------------------------
# Per-dimension scorers  (each returns a value in [0,1] or neutral)
# ---------------------------------------------------------------------------


def _dim_query_match(query: str, content: Any) -> float:
    """query_match — lexical F1 overlap between the task query and content."""
    from memchorus.relevance_engine import RelevanceScorer  # local: avoid cycle

    try:
        return float(RelevanceScorer._score_quality(query, content))
    except Exception:  # graceful: neutral rather than an escape
        return _DOMAIN_NEUTRAL


def _dim_recency(timestamp: Any, as_of: Optional[float], tau_days: float) -> float:
    """recency — exp(-age_days / tau), brand-new -> 1.0, older -> decays.

    Missing, non-numeric, or pre-epoch timestamps degrade to neutral (0.5)
    rather than 0.0, so a candidate with no reliable timestamp is neither
    rewarded nor destroyed.
    """
    import time as _t

    if as_of is None or as_of <= 0:
        as_of = _t.time()
    age_days: Optional[float] = None
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        age_days = (as_of - float(timestamp)) / 86400.0
    if age_days is None:
        return _RECENCY_NEUTRAL
    if age_days < 0:
        age_days = 0.0
    if tau_days <= 0:
        return _RECENCY_NEUTRAL
    # Clamp the exponent range to avoid subnormal underflow at very large age.
    return float(math.exp(-min(age_days / tau_days, 40.0)))


def _dim_strength(key: Optional[str]) -> float:
    """strength — calibrated utility of the key, normalised to [0,1].

    Reads the in-memory ``HitRateTracker`` (no network).  1.0 (no history)
    maps to 0.2; high-utility keys map toward 1.0.
    """
    boost = 1.0
    try:
        from memchorus.calibration_engine import CalibrationEngine  # local import

        boost = float(CalibrationEngine().boost_factor_for_key(key or ""))
    except Exception:
        boost = 1.0
    span = _STRENGTH_MAX - _STRENGTH_MIN
    if span <= 0:
        return 0.5
    norm = (boost - _STRENGTH_MIN) / span
    return float(min(max(norm, 0.0), 1.0))


def _dim_domain(candidate: Dict[str, Any], domain: Optional[str]) -> float:
    """domain_relevance — 1.0 on an exact _domain match, 0.0 otherwise,
    neutral (0.5) when the task has no domain guidance."""
    if domain is None:
        return _DOMAIN_NEUTRAL
    cand_domain = candidate.get("_domain")
    if cand_domain is None:
        return 0.0
    return (
        1.0 if str(cand_domain).strip().lower() == str(domain).strip().lower() else 0.0
    )


def _dim_source_channel(source: Any) -> float:
    """source_channel — normalised source prior, matched to the legacy scorer."""
    if not source:
        return _SOURCE_UNKNOWN
    max_prior = max(_DEFAULT_SOURCE_PRIORS.values()) if _DEFAULT_SOURCE_PRIORS else 1.0
    return float(
        _DEFAULT_SOURCE_PRIORS.get(source, _SOURCE_UNKNOWN) / max(max_prior, 1e-9)
    )


def _closet_boost(candidate: Dict[str, Any], active_project: Optional[str]) -> float:
    """Bound-project (closet) additive boost, or 0.0 when not bound."""
    if not active_project:
        return 0.0
    try:
        from memchorus.relevance_engine import (
            ContextWeight,
            closet_bound_result,
        )  # local

        if closet_bound_result(candidate, active_project):
            return float(getattr(ContextWeight(), "closet_boost_factor", 0.35) or 0.35)
    except Exception:
        return 0.0
    return 0.0


def _sub_signal_boosts(
    candidate: Dict[str, Any], active_project: Optional[str]
) -> Dict[str, float]:
    """Working-state sub-signal boosts (bounded tier), applied to bound results.

    Returns ``{f"sub_signal:<signal>": weight}`` for each fired tier.  Only
    bound (closet) candidates carry sub-signal — consistent with R4 in
    ``relevance_engine`` (unbound drawers stay pure context and do not borrow a
    working-state nudge).
    """
    if not active_project:
        return {}
    try:
        from memchorus.relevance_engine import _detect_fired_signals  # local

        fired = _detect_fired_signals(
            candidate.get("content"), candidate.get("key"), candidate
        )
    except Exception:
        return {}
    if not fired:
        return {}
    from memchorus.relevance_engine import _SUB_SIGNAL_WEIGHTS  # canonical tiers

    out: Dict[str, float] = {}
    for sig in fired:
        w = _SUB_SIGNAL_WEIGHTS.get(sig)
        if w is not None:
            out[f"sub_signal:{sig}"] = float(w)
    return out


def _score_breakdown(
    candidate: Dict[str, Any],
    task: TaskContext,
    weights: BaseDimensionWeights,
) -> Dict[str, float]:
    """Compute the five base dimensions (each in [0,1]) for one candidate."""
    return {
        "query_match": _dim_query_match(task.query, candidate.get("content")),
        "recency": _dim_recency(
            candidate.get("timestamp"), task.as_of, task.recency_tau_days
        ),
        "strength": _dim_strength(candidate.get("key")),
        "domain_relevance": _dim_domain(candidate, task.domain),
        "source_channel": _dim_source_channel(candidate.get("source")),
    }


def select(
    task: TaskContext,
    candidates: List[Dict[str, Any]],
    K: int,
    B_tokens: int,
    reserved_slots: Optional[int] = None,
) -> SelectionResult:
    """Task-aware selection: pick the top-K candidates for *task* within budget.

    Contract (spec §4, card #223):
      - Each candidate gets a *base score* = Σ (base_weight_i × dim_i), where
        the weights are ``task.base_weights or RECALL_CONFIG.base_weights``
        and Σweights = 1.0.  Every dim is in [0,1], so base ∈ [0,1].
      - Two additive boosts are then applied (both non-negative):
        the bound-project closet boost and the working-state sub-signal tiers.
        ``final = base + sum(boosts.values())`` — boosts only ever *raise* a
        candidate (never reorder below a higher-base item).
      - Candidates are ranked by ``(-final, -base, key)`` — deterministic.
      - Output is the highest-ranked prefix that (a) contains at most
        ``K - reserved_slots`` items and (b) fits within ``B_tokens``
        (estimated via ``len(content)//4``), in strictly-rank order.

    Reserved slots (spec §6.2, pillar 3):
      ``reserved_slots`` sets aside the top-N item positions for pillar-3
      augmentation (tunnel, diary, event provenance), so the base selection
      never occupies the slots pillar-3 will fill.  Defaults to ``0`` (no
      reserves) so this primitive stays neutral; the recall *path* passes
      ``RECALL_CONFIG.reserved_slots_implied`` (1) or
      ````reserved_slots_explicit`` (2) to hold that headroom.  ``None`` is
      treated as 0.  The value is clamped to ``[0, K]`` — if it is >= K then
      the chosen list is empty (pillar-3 owns the entire budget).
    The budget is still strictly enforced (``cumulative tokens <= B_tokens``),
    skipping no-fit items in rank order.

    Pure: no LLM, no MCP round-trip, no orchestrator.  Accepts an empty
    ``candidates`` list and returns an empty ``SelectionResult``.
    """
    weights = task.base_weights or RECALL_CONFIG.base_weights
    # Σweights = 1.0 is a spec invariant (1-AC2); guard against a mis-wired
    # override so the weighted sum stays a proper probability mix.
    total_w = (
        weights.query_match
        + weights.recency
        + weights.strength
        + weights.domain_relevance
        + weights.source_channel
    )
    if abs(total_w - 1.0) > 1e-6:
        raise ValueError(
            f"select() base weights must sum to 1.0, got {total_w}. "
            "Pass base_weights matching RECALL_CONFIG.base_weights."
        )

    if K <= 0 or B_tokens < 0:
        return SelectionResult(
            chosen=[], total_tokens_used=0, total_tokens_budget=B_tokens
        )
    if not candidates:
        return SelectionResult(
            chosen=[], total_tokens_used=0, total_tokens_budget=B_tokens
        )

    scored: List[SelectedCandidate] = []
    for cand in candidates:
        breakdown = _score_breakdown(cand, task, weights)
        base_score = (
            weights.query_match * breakdown["query_match"]
            + weights.recency * breakdown["recency"]
            + weights.strength * breakdown["strength"]
            + weights.domain_relevance * breakdown["domain_relevance"]
            + weights.source_channel * breakdown["source_channel"]
        )
        closet = _closet_boost(cand, task.active_project)
        boosts: Dict[str, float] = {"closet": closet} if closet > 0.0 else {}
        sub = _sub_signal_boosts(cand, task.active_project)
        if sub:
            boosts.update(sub)
        final_score = base_score + sum(boosts.values())
        tokens = estimate_tokens(cand.get("content"))
        scored.append(
            SelectedCandidate(
                result=cand,
                base_score=round(base_score, 6),
                boosts=boosts,
                final_score=round(final_score, 6),
                breakdown={k: round(v, 6) for k, v in breakdown.items()},
                tokens=tokens,
            )
        )

    # Deterministic rank: highest final, then highest base, then stable key.
    scored.sort(
        key=lambda sc: (-sc.final_score, -sc.base_score, str(sc.result.get("key", "")))
    )

    # Reserved-slot headroom (spec §6.2, pillar 3): the recall path holds
    # these item positions open for augmentation; select caps chosen to K-n.
    reserve = 0 if reserved_slots is None else max(0, min(int(reserved_slots), K))
    cap = K - reserve

    # Budget-bounded prefix (highest first): take up to `cap` whose cumulative
    # cost fits B_tokens; stop at the first that would overflow.
    chosen: List[SelectedCandidate] = []
    used = 0
    for sc in scored:
        if len(chosen) >= cap:
            break
        if used + sc.tokens > B_tokens:
            break
        chosen.append(sc)
        used += sc.tokens

    return SelectionResult(
        chosen=chosen,
        total_tokens_used=used,
        total_tokens_budget=B_tokens,
    )


# NOTE: No stub/try-except fallback around the top-level import on line 30.
# The unguarded ``from memchorus.behavioral_trigger import DecisionPoint, DetectedPoint``
# either succeeds (putting DecisionPoint into globals, so this block is dead code) or raises
# ImportError immediately and aborts module loading — the if-statement below never executes
# because Python never reaches it when the import fails.  A stub here would give false
# confidence: the module would appear to load but all decision-point logic would silently use
# locally-defined enums with no real BehavioralTrigger wiring, defeating enforcement.
# The correct fix for missing behavior_trigger is: ensure behavioral_trigger.py ships
# correctly alongside this module; don't silently degrade enforcement to stub classes in-use.
# If a packaging scenario ever requires graceful degradation (e.g., wheels that omit optional
# dependencies), replace the top-level import with ``try … except ImportError`` and gate the
# entire class behind an availability check, not a stub enum.
