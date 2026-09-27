"""
memchorus.surface_routing — write-time surface-routing classifier (spec #226).

This module is the *write-side* classifier that MemPalace's recall loop
(#224) consumes: it decides which of the three memory **surfaces** a new
fact lands on, and records that decision so a reader can see *why* the
fact is on its surface.

The authoritative shape of the three surfaces lives in the README
("The Three Memory Surfaces & Pointer Model") — this module does NOT
redefine what MEMORY / DRAWER / KG *is*. It only defines the *routing
decision* (the classifier contract, §4) and the ``routing_kind`` field
that the decision leaves on the persisted payload (spec §5.3).

Contract summary (spec §4, §5.3, §6):

* ``route(content, context) -> surface`` is a **pure, deterministic**
  function: given the same inputs it always returns the same one of
  ``"MEMORY"`` / ``"DRAWER"`` / ``"KG"``. It never writes, never pokes a
  live MCP backend, never reads the clock — the clock is passed in as
  ``context.as_of`` so every decision rule is assertable in a unit test.
* ``routing_kind`` and ``emission_kind`` are **two independent fields**
  (spec §5.3). ``routing_kind`` is *which surface*; ``emission_kind`` is
  *how the body is encoded* (``"json"`` or ``"str"``). They are set
  independently, never folded into one enum.

Decision rules (spec §4.1, first match wins):

==========================  ===========================================
Rule                        Condition                                 → Surface
==========================  ===========================================
R0  Explicit caller         ``context.caller_explicit`` is set (and   caller_explicit
                            not vetoed by R4)
R1  Standing preference     ``is_standing_preference`` is true        MEMORY
R2  Rule-of-state           relationship shape OR (validity window    KG
                            AND ``LONG_LIVED_KNOWLEDGE``)
R3  Default fallback        nothing above matched                     DRAWER
R4  Veto (KG → DRAWER)      R2 fired BUT the fact is expired /        DRAWER
                            not-yet-valid at ``context.as_of``
==========================  ===========================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("memchorus.surface_routing")

# --------------------------------------------------------------------------- #
# Surface (the decision OUTPUT, spec §2 / §4.2)
# --------------------------------------------------------------------------- #
# Kept as a plain ``str`` alias so the persisted ``routing_kind`` is a stable,
# JSON-serialisable, exactly-equal string (spec §4.2: "returns a Surface string
# exactly: 'MEMORY', 'DRAWER', 'KG'. No aliases, no None.").
SURFACE_MEMORY = "MEMORY"
SURFACE_DRAWER = "DRAWER"
SURFACE_KG = "KG"
SURFACES = (SURFACE_MEMORY, SURFACE_DRAWER, SURFACE_KG)
Surface = Any  # str; one of SURFACE_MEMORY / SURFACE_DRAWER / SURFACE_KG

# Emission kinds (spec §5.3) — orthogonal to routing.
EMISSION_JSON = "json"
EMISSION_STR = "str"
EMISSION_KINDS = (EMISSION_JSON, EMISSION_STR)


class FactKind(Enum):
    """Re-expression of ``MemoryProfile`` as a *shape of fact* (spec §4).

    The classifier does NOT invent new structural detection; it interprets
    the existing ``_infer_profile`` heuristic, re-stated as a shape:

    * ``STANDING_PREFERENCE``  ← USER_PREFERENCE, CONTEXT_SENSITIVE_PREF
    * ``LONG_LIVED_KNOWLEDGE`` ← LONG_LIVED_KNOWLEDGE (AUTO when dict+graph-kw)
    * ``RELATIONSHIP_SHAPE``   ← RELATIONSHIP_GRAPH
    * ``VERBATIM_ARTIFACT``    ← EPHEMERAL, LARGE_DATA_BLOCK
    """

    STANDING_PREFERENCE = "standing_preference"
    LONG_LIVED_KNOWLEDGE = "long_lived_knowledge"
    RELATIONSHIP_SHAPE = "relationship_shape"
    VERBATIM_ARTIFACT = "verbatim_artifact"


@dataclass
class RouteContext:
    """Inputs the classifier needs, injected at the call site (spec §4).

    Every field is available in tests, so each decision rule is assertable
    without a live MCP backend, a real clock, or network.
    """

    #: The caller's requested surface (if the orchestrator was given an
    #: explicit ``source_name`` / ``profile`` hint mapping to a surface).
    #: The classifier defers to it — *except* R4 can veto an explicit ``KG``.
    caller_explicit: Optional[str] = None

    #: ``_infer_profile(value)`` re-expressed as a :class:`FactKind`.
    fact_kind: FactKind = FactKind.VERBATIM_ARTIFACT

    #: True if *content* carries ``valid_from`` / ``valid_to`` /
    #: ``superseded_by``. The classifier also reads the actual dates from
    #: *content* for the R4 veto; this flag is the *signal* "there is a
    #: temporal window", which R2 requires for the temporal branch.
    has_validity_window: bool = False

    #: True if ``_infer_profile(value)`` returned USER_PREFERENCE or
    #: CONTEXT_SENSITIVE_PREF.
    is_standing_preference: bool = False

    #: True if the fact is *structurally* a relation (typed dict, list of
    #: 2-tuples) — from RELATIONSHIP_GRAPH or a subject/predicate/object shape.
    is_relationship_shape: bool = False

    #: Wall-clock used by the R4 expiry veto. ``None`` means "no expiry
    #: check" (the live write is treated as current).
    as_of: Optional[datetime] = None

    # -- observability (populated by the classifier, not part of the inputs) --
    #: Which rule fired ("R0".."R4") — set after routing (spec: "structured
    #: logging of the routing decision ... which rule fired, or 'fallback'").
    rule: Optional[str] = field(default=None, init=False, repr=False)
    #: Short human/structured reason for the decision.
    reason: Optional[str] = field(default=None, init=False, repr=False)
    #: The surface that was chosen (mirrors the returned value for the
    #: persisting caller).
    surface: Optional[str] = field(default=None, init=False, repr=False)


# --------------------------------------------------------------------------- #
# Structural detection helpers (spec §4.1 / §6.3)
# --------------------------------------------------------------------------- #

_VALIDITY_KEYS = ("valid_from", "valid_to", "superseded_by")


def _coerce_dict(value: Any) -> Optional[dict]:
    """Return *value* if it is a dict, else a nested ``text``/``content`` dict
    if one exists, else None.  Used to reach validity fields that may sit
    one level in."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (dict,)):  # pragma: no cover - defensive
        return value
    return None


def get_validity_fields(value: Any) -> dict:
    """Extract ``valid_from`` / ``valid_to`` / ``superseded_by`` from *value*.

    Handles the fact object being a plain dict (typical) or a dict that wraps
    the fact under ``text``/``content``/``fact``.  Missing fields are ``None``.
    """
    out = {"valid_from": None, "valid_to": None, "superseded_by": None}
    candidates = [value]
    if isinstance(value, dict):
        for wrap in ("fact", "record", "data"):
            inner = value.get(wrap)
            if isinstance(inner, dict):
                candidates.append(inner)
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        for k in _VALIDITY_KEYS:
            if out[k] is None and cand.get(k) is not None:
                out[k] = cand[k]
    return out


def _has_validity_window(value: Any) -> bool:
    """True if *value* carries any of valid_from / valid_to / superseded_by."""
    vf = get_validity_fields(value)
    return any(vf[k] is not None for k in _VALIDITY_KEYS)


def _parse_dt(value: Any) -> Optional[datetime]:
    """Best-effort parse of a date/datetime field; None on failure.

    The classifier NEVER raises on a malformed boundary — it treats an
    unparseable ``valid_from`` / ``valid_to`` as "no boundary" so a writer that
    forgot a valid date still gets a sane routing decision.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _naive(value)
    if isinstance(value, date):
        return _naive(datetime(value.year, value.month, value.day))
    s = str(value).strip()
    if not s:
        return None
    for candidate in (s, s.replace("Z", "+00:00"), s[:19], s[:10]):
        try:
            return _naive(datetime.fromisoformat(candidate))
        except (ValueError, TypeError):
            continue
    return None


def _naive(dt: datetime) -> datetime:
    """Normalise to a naive UTC datetime for stable comparisons in R4."""
    try:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:  # pragma: no cover - defensive
        pass
    return dt


def _r4_vetoed(value: Any, as_of: Optional[datetime]) -> bool:
    """R4: an already-expired (or not-yet-valid) rule-of-state at write time.

    Returns True when the fact is a *historical record* → should sit on DRAWER,
    not KG (spec §4.1 R4, AC-6).  Only meaningful once R2 has fired.
    """
    if as_of is None:
        return False
    boundary = _parse_dt(as_of)
    if boundary is None:
        return False  # unparseable as_of → no expiry check, not a veto
    vf = _parse_dt(get_validity_fields(value).get("valid_from"))
    vt = _parse_dt(get_validity_fields(value).get("valid_to"))
    # Not-yet-valid: write happened before the fact's validity window opened.
    if vf is not None and boundary < vf:
        return True
    # Already-expired: the validity window closed before the write happened.
    if vt is not None and vt < boundary:
        return True
    return False


def _is_structural_relationship(value: Any) -> bool:
    """Structural (NOT lexical) relationship shape (spec §6.3).

    A prose body that mentions two entities is NOT a relationship — the shape
    must be a typed ``subject → predicate → object`` dict or a list of 2-tuples.
    """
    if isinstance(value, dict):
        # A typed triple is the canonical KG shape.
        if all(k in value for k in ("subject", "predicate", "object")):
            return True
        # A dict that explicitly declares a relation edge.
        if value.get("relation") or value.get("predicate"):
            return True
        return False
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                return True
            if isinstance(item, dict) and all(
                k in item for k in ("subject", "predicate", "object")
            ):
                return True
        return False
    return False


# --------------------------------------------------------------------------- #
# Emission kind (spec §5.3) — orthogonal to routing
# --------------------------------------------------------------------------- #

_TEXT_WRAPPER_KEYS = ("text", "_content", "body", "content", "value", "str")


def emission_kind(value: Any) -> str:
    """How the body is *encoded* (spec §5.3): ``"json"`` or ``"str"``.

    Determined from the shape of the body itself:

    * a plain string, or a dict that *wraps* a string body under a textual key
      (``text`` / ``_content`` / ``body`` / ``content`` / ``value``) → ``"str"``;
    * a genuine structured payload — a real typed triple (``subject`` +
      ``predicate`` + ``object``) or a list of edges → ``"json"``.

    This is intentionally *independent* of :func:`routing_decision`: a fact can
    be ``routing_kind="KG"`` AND ``emission_kind="str"`` (a rule-of-state
    written as prose) or ``routing_kind="DRAWER"`` AND ``emission_kind="json"``.
    """
    if isinstance(value, str):
        return EMISSION_STR
    if isinstance(value, (list, tuple)):
        return EMISSION_JSON
    if isinstance(value, dict):
        for k in _TEXT_WRAPPER_KEYS:
            if k in value and isinstance(value[k], str):
                return EMISSION_STR
        if all(k in value for k in ("subject", "predicate", "object")):
            return EMISSION_JSON
        # A structured dict that is not a string-wrapper → json-encoded body.
        return EMISSION_JSON
    # int / float / bool / None — encode as a simple value.
    return EMISSION_STR


# --------------------------------------------------------------------------- #
# The classifier (spec §4)
# --------------------------------------------------------------------------- #


def routing_decision(
    content: Any,
    context: Optional[RouteContext] = None,
) -> RouteContext:
    """Evaluate the decision rules and return the populated :class:`RouteContext`.

    This is the *observable* form of the classifier: after the call,
    ``context.surface`` is the chosen surface, ``context.rule`` is the rule that
    fired (``"R0"``..``"R4"``), and ``context.reason`` explains why.  :func:`route`
    is a thin wrapper around this that returns just the surface string.

    The function is pure and deterministic: the same ``(content, context)``
    always yields the same decision.  It does not raise on unusual inputs — a
    malformed fact falls through to the conservative DRAWER default (R3/R4).
    """
    ctx = context if context is not None else RouteContext()

    is_standing = ctx.is_standing_preference or ctx.fact_kind == FactKind.STANDING_PREFERENCE
    is_relationship = ctx.is_relationship_shape or _is_structural_relationship(content)
    has_window = ctx.has_validity_window or _has_validity_window(content)

    def _finish(surface: str, rule: str, reason: str) -> RouteContext:
        ctx.surface = surface
        ctx.rule = rule
        ctx.reason = reason
        if ctx.has_validity_window is False and has_window:
            ctx.has_validity_window = True
        # Structured observability (spec: "which rule fired, or 'fallback'").
        logger.info(
            "surface_route rule=%s surface=%s standing=%s relationship=%s "
            "window=%s explicit=%s reason=%s",
            rule,
            surface,
            is_standing,
            is_relationship,
            has_window,
            ctx.caller_explicit,
            reason,
        )
        return ctx

    # --- R0: explicit caller ----------------------------------------------
    if ctx.caller_explicit is not None and ctx.caller_explicit in SURFACES:
        if (
            ctx.caller_explicit == SURFACE_KG
            and _r4_vetoed(content, ctx.as_of)
        ):
            return _finish(
                SURFACE_DRAWER,
                "R4",
                "explicit KG caller vetoed: fact expired/not-yet-valid at as_of",
            )
        return _finish(ctx.caller_explicit, "R0", "caller named the surface explicitly")

    # --- R1: standing preference → MEMORY ---------------------------------
    if is_standing:
        return _finish(
            SURFACE_MEMORY,
            "R1",
            "standing preference: timeless rule-of-conduct, injected every turn",
        )

    # --- R2: rule-of-state → KG (subject to R4 veto) -----------------------
    temporal_branch = has_window and ctx.fact_kind == FactKind.LONG_LIVED_KNOWLEDGE
    if is_relationship or temporal_branch:
        if _r4_vetoed(content, ctx.as_of):
            return _finish(
                SURFACE_DRAWER,
                "R4",
                "rule-of-state expired/not-yet-valid at as_of → historical record",
            )
        reason = (
            "typed relationship shape" if is_relationship else "temporal window on long-lived knowledge"
        )
        return _finish(SURFACE_KG, "R2", f"rule-of-state: {reason}")

    # --- R3: conservative default → DRAWER --------------------------------
    return _finish(
        SURFACE_DRAWER,
        "R3",
        "no rule matched: conservative verbatim-artefact sink",
    )


def route(content: Any, context: Optional[RouteContext] = None) -> str:
    """Return the chosen surface exactly: ``"MEMORY"`` / ``"DRAWER"`` / ``"KG"``.

    Thin, contract-compliant wrapper over :func:`routing_decision` (spec §4.2).
    Deterministic; never returns ``None``.
    """
    result = routing_decision(content, context)
    surface = result.surface
    # routing_decision always sets `.surface` via _finish(); this guard is a
    # belt-and-braces invariant so the public contract stays "exactly a string".
    if surface not in SURFACES:  # pragma: no cover - invariant
        surface = SURFACE_DRAWER
    return surface


__all__ = [
    "Surface",
    "SURFACES",
    "SURFACE_MEMORY",
    "SURFACE_DRAWER",
    "SURFACE_KG",
    "EMISSION_JSON",
    "EMISSION_STR",
    "EMISSION_KINDS",
    "FactKind",
    "RouteContext",
    "route",
    "routing_decision",
    "emission_kind",
    "_r4_vetoed",
    "_is_structural_relationship",
    "_has_validity_window",
    "get_validity_fields",
    "_parse_dt",
]
