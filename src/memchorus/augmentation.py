"""
Pillar 3 — Tunnels / Diary / Events reserved augmentation slots (issue #225).

Implements the North-star spec §6: a deterministic, additive augmentation
channel that reserves *slots inside the K budget* for three MemPalace sources
with exact trigger conditions.  It is explicitly NOT a competing rater:
reserved items never enter the pillar-1 weighted score and never displace
ranked items by out-scoring them.  They only reduce the number of slots
available to main ranked items (§6.4 — additive, §6.2 — budget).

Trigger table (spec §6.1 — deterministic, no LLM, no "sometimes"):
    Tunnels:        T.domain set AND a tunnel to a different domain exists AND
                    the task text references the bridge domain.
    Diary:          T.kind in {synthesis, project_start, post_action}.
    Events+Artif.:  T.kind in {review, handoff, integration} OR the task text
                    references a correlation_id / patch.ready / artifact id.

Section layout (spec §6.3):
    [BRIDGE/<domain>]    tunnel rows
    [SESSION LOG]        diary rows
    [COORDINATION]       event / artifact rows

Budget (spec §6.2, config root ``RECALL_CONFIG``):
    implied recall  →  at most ``reserved_slots_implied``  (1) reserved item
    explicit recall →  at most ``reserved_slots_explicit`` (2) reserved items
The cap applies to the UNION of all three sources — a tunnel never consumes
two slots to the exclusion of a coordination event; the budget is shared.

Determinism guarantees (spec §6.6):
    - Same ``(task, fetcher-data)`` pair → identical output, including order.
    - No clock, no randomness, no LLM anywhere in this module.

Graceful degradation (spec §6.7):
    - ``fetcher=None`` or a ``fetch(source)`` raising → empty contribution
      from that source, never propagates.  Other sources are unaffected.

No cross-task carryover (spec §6.1 note):
    - A tunnel fires only when *this* task's domain + terms reference the
      bridge.  A tunnel fetched for one task must not leak into another.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from memchorus.recall_config import RECALL_CONFIG, RecallLoopConfig

logger = logging.getLogger(__name__)

__all__ = [
    "AugmentationSource",
    "AugItem",
    "augment",
    "combine",
    "reserved_budget",
    "render_augmentation_block",
    "triggers_for",
]

# ---------------------------------------------------------------------------
# Trigger vocabulary (spec §6.1)
# ---------------------------------------------------------------------------

#: T.kind values that trigger the diary (session log) source.
_DIARY_KINDS: frozenset = frozenset({"synthesis", "project_start", "post_action"})

#: T.kind values that trigger the events/artifacts (coordination) source.
_EVENT_KINDS: frozenset = frozenset({"review", "handoff", "integration"})

#: Text references that independently trigger the events/artifacts source
#: (spec §6.1: "task references correlation_id / patch.ready / artifact id").
_EVENT_TEXT_RE = re.compile(
    r"correlation_id"  # explicit id token
    r"|patch\.ready"  # event type literal
    r"|\bartifact\s*#\d+",  # "artifact#97" or "artifact #97"
    re.IGNORECASE,
)

#: Bridge-domain reference: task text must mention the *bridge* domain, not
#: just its own.  We match any token of the domain name (lowercased,
#: word-boundary) — the spec says "task references bridge domain", which
#: we interpret as a literal substring mention of a domain string that is
#: NOT the task's own domain (no cross-task carryover, §6.1 note).
_BRIDGE_RE = re.compile(
    r"(?:to|towards|bridg\w*|tunnels?|connect|link)s?\s+(?:the\s+)?([a-z][a-z0-9-]+)"
)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: A fetcher callable: ``fetch(source: str) -> list[dict]``.
#: ``source`` is one of ``"tunnel"``, ``"diary"``, ``"event"``.  The fetcher
#: is responsible for any MCP transport (``mempalace_find_tunnels``,
#: ``mempalace_diary_read``, ``mempalace_artifact_get``/``mempalace_event_list``).
#: It must be deterministic: same input → same list, same order.
AugmentationSource = Callable[[str], List[Dict[str, Any]]]


# ---------------------------------------------------------------------------
# Data class for a single reserved item
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AugItem:
    """One reserved augmentation item.

    Attributes:
        source:      which of {tunnel, diary, event} produced this item.
        provenance:  human-readable traceability string (e.g. "tunnel#1",
                     "diary:entry-2026-09-24", "artifact#97").  Required.
        section:     the layout label from spec §6.3 (e.g. "BRIDGE/mempalace").
        content:     the row data fetched from the source (opaque dict).
        reserved:    always ``True`` — marker for the combine/render layer to
                     distinguish these from pillar-1 ranked items.
    """

    source: str
    provenance: str
    section: str
    content: Dict[str, Any] = field(compare=False, default_factory=dict)
    reserved: bool = True

    def __getitem__(self, key: str) -> Any:
        """Dict-like access so callers can use either ``item["source"]`` or
        ``item.source`` interchangeably (both resolve to the same fields)."""
        if key == "content":
            return self.content
        try:
            return getattr(self, key)
        except AttributeError:
            return self.content.get(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like ``.get()`` for compatibility with the ``dict`` protocol
        used by downstream engine callers that expect ``it.get("source")``."""
        if key == "content":
            return self.content
        if hasattr(self, key):
            return getattr(self, key)
        return (
            self.content.get(key, default)
            if isinstance(self.content, dict)
            else default
        )

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict form for downstream engine / orchestrator use."""
        return {
            "source": self.source,
            "provenance": self.provenance,
            "section": self.section,
            "content": dict(self.content),
            "reserved": self.reserved,
        }


# ---------------------------------------------------------------------------
# Trigger logic (spec §6.1)
# ---------------------------------------------------------------------------


def _task_field(task: Dict[str, Any], key: str) -> Any:
    """Read a field from the task dict (tolerates missing keys)."""
    return task.get(key) if isinstance(task, dict) else getattr(task, key, None)


def triggers_for(task: Dict[str, Any]) -> Set[str]:
    """Return the set of pillar-3 sources this task triggers.

    Deterministic.  No side effects.  Returns at most ``{"tunnel","diary","event"}``.

    Args:
        task: task-signal dict with keys ``kind``, ``domain``, ``text``,
              ``explicit`` (any subset).  Missing keys → that dimension
              contributes nothing.
    """
    fired: Set[str] = set()
    kind = str(_task_field(task, "kind") or "").strip().lower()
    domain = (_task_field(task, "domain") or "").strip().lower()
    text = str(_task_field(task, "text") or "").lower()

    # --- Diary (spec §6.1 row 2) -------------------------------------------
    if kind in _DIARY_KINDS:
        fired.add("diary")

    # --- Events / artifacts (spec §6.1 row 3) -------------------------------
    #   fires on T.kind ∈ {review, handoff, integration}  OR
    #   task text references correlation_id / patch.ready / artifact id
    if kind in _EVENT_KINDS or _EVENT_TEXT_RE.search(text):
        fired.add("event")

    # --- Tunnel (spec §6.1 row 1) -------------------------------------------
    #   fires when:
    #     a) task has a domain set, AND
    #     b) task text references a bridge domain (a domain OTHER than its own)
    #   No cross-task carryover: own domain alone is NOT a bridge reference.
    if domain:
        # A bridge reference is a domain token in the text that is not the
        # task's own domain.  We scan free-text words (lowercased) and check
        # whether any of them match a known bridge-domain keyword that is
        # different from task.domain.
        words = re.findall(r"[a-z][a-z0-9-]{1,}", text)
        # Also check the structured bridge field if present
        bridge = str(_task_field(task, "bridge_domain") or "").strip().lower()
        if bridge and bridge != domain:
            fired.add("tunnel")
        else:
            # Fallback: the text itself references a different domain-like token.
            # We use a heuristic: a word that is at least 4 chars, not a common
            # stopword, and != domain, appearing in a tunnel/bridge context.
            _STOP = frozenset(
                {
                    "the",
                    "and",
                    "for",
                    "with",
                    "this",
                    "that",
                    "from",
                    "into",
                    "over",
                    "under",
                    "upon",
                    "task",
                    "text",
                    "kind",
                    "none",
                    "true",
                    "false",
                    "null",
                    "review",
                    "handoff",
                    "integration",
                    "recall",
                    "bridge",
                }
            )
            for w in words:
                if (
                    w != domain
                    and w not in _STOP
                    and len(w) >= 4
                    and not w.startswith("correlation")
                    and not w.startswith("patch")
                    and not w.startswith("artifact")
                ):
                    # Additional heuristic gate: the word must appear alongside
                    # a bridge-related keyword OR be a standalone domain token.
                    ctx = re.search(
                        rf"(?:tunnel|bridge|connect|link|towards|towords)\s+(?:the\s+)?{re.escape(w)}\b",
                        text,
                    ) or re.search(
                        rf"{re.escape(w)}\s+(?:bridge|tunnel|connect|link)\b",
                        text,
                    )
                    if ctx:
                        fired.add("tunnel")
                        break

    return fired


# ---------------------------------------------------------------------------
# Budget (spec §6.2)
# ---------------------------------------------------------------------------


def reserved_budget(
    explicit: bool,
    override: Optional[int] = None,
    config: Optional[RecallLoopConfig] = None,
) -> int:
    """Return the max number of reserved slots for this recall call.

    Args:
        explicit: True for explicit (user/agent-requested) recall,
                  False for implied (decision-point / auto) recall.
        override:  If not None, use this as the cap (still ≤ K).
        config:    RecallLoopConfig (defaults to RECALL_CONFIG singleton).
    """
    cfg = config or RECALL_CONFIG
    if override is not None and override >= 0:
        cap = override
    elif explicit:
        cap = cfg.reserved_slots_explicit
    else:
        cap = cfg.reserved_slots_implied
    # Never exceed K (pillar-3 slots are drawn FROM the K budget)
    k = cfg.k_explicit if explicit else cfg.k_implied
    return min(cap, k)


# ---------------------------------------------------------------------------
# Augment pipeline
# ---------------------------------------------------------------------------

#: Canonical source order (tunnel → diary → event) — spec §6.2: "at most 1
#: slot per source" is NOT the spec rule; it's "at most N RESERVATIONS TOTAL
#: across all sources".  The canonical order ensures deterministic
#: assignment when the budget exceeds the number of fired sources.
_SOURCE_ORDER: tuple = ("tunnel", "diary", "event")

#: Section labels per source (spec §6.3)
_SECTION_LABEL: Dict[str, str] = {
    "tunnel": "BRIDGE",
    "diary": "SESSION LOG",
    "event": "COORDINATION",
}


def _label_section(source: str, task: Dict[str, Any]) -> str:
    """Build the full section header for a source (spec §6.3).

    tunnel → ``[BRIDGE/<domain>]``  (uses bridge_domain or task.domain)
    diary  → ``[SESSION LOG]``
    event  → ``[COORDINATION]``
    """
    base = _SECTION_LABEL.get(source, source.upper())
    if source == "tunnel":
        domain = (
            _task_field(task, "bridge_domain")
            or _task_field(task, "domain")
            or "bridge"
        )
        return f"BRIDGE/{domain}"
    return base


def _fetch_rows(
    fetcher: Optional[AugmentationSource],
    source: str,
) -> List[Dict[str, Any]]:
    """Call the fetcher for ``source``; swallow any exception → []."""
    if fetcher is None:
        return []
    try:
        rows = fetcher(source)
        if rows is None:
            return []
        return list(rows)
    except Exception:
        logger.debug(
            "pillar-3: fetch(%s) raised — contributing 0 rows (degraded)",
            source,
            exc_info=True,
        )
        return []


def _make_item(
    source: str,
    task: Dict[str, Any],
    row: Dict[str, Any],
    index: int,
) -> AugItem:
    section = _label_section(source, task)
    prov = row.get("provenance") or row.get("source_key") or f"{source}#{index + 1}"
    return AugItem(
        source=source,
        provenance=str(prov),
        section=section,
        content=dict(row),
        reserved=True,
    )


def augment(
    *,
    task: Dict[str, Any],
    fetcher: Optional[AugmentationSource],
    explicit: bool = False,
    config: Optional[RecallLoopConfig] = None,
) -> List[AugItem]:
    """Execute the pillar-3 augmentation pipeline.

    1. Determine which sources this task triggers (spec §6.1).
    2. If the budget is 0 → return [] immediately (no fetch needed).
    3. For each triggered source (canonical order), fetch rows.
    4. Apply the shared budget cap across ALL sources (spec §6.2).
    5. Stamp each row with provenance + ``reserved=True``.
    6. Return the capped, ordered list of ``AugItem``.

    Args:
        task:     task-signal dict (see ``triggers_for``).
        fetcher:  callable or None (None → no sources fetched → []).
        explicit: True → use ``reserved_slots_explicit`` budget.
        config:   RecallLoopConfig override.

    Returns:
        list[AugItem] — empty list if nothing fires or fetcher fails.
        Never raises.
    """
    budget = reserved_budget(explicit=explicit, config=config)
    if budget <= 0:
        return []

    fired = triggers_for(task)
    if not fired:
        return []

    items: List[AugItem] = []
    for source in _SOURCE_ORDER:
        if source not in fired:
            continue
        rows = _fetch_rows(fetcher, source)
        for i, row in enumerate(rows):
            if len(items) >= budget:
                break
            items.append(_make_item(source, task, row, i))
        if len(items) >= budget:
            break
    return items


# ---------------------------------------------------------------------------
# Combine with main ranked list (spec §6.4 — additive, never displaces)
# ---------------------------------------------------------------------------


def combine(
    main: List[Any],
    reserved: List[AugItem],
) -> List[Any]:
    """Combine a pillar-1 ranked list with pillar-3 reserved items.

    Reserved items are appended AFTER all main items (additive, spec §6.4).
    They never reorder the main list and never replace one.

    Args:
        main:     the K-1 main ranked items from select().
        reserved: pillar-3 AugItem list (already budget-capped).

    Returns:
        main + reserved (as plain dicts in the reserved entries).
    """
    return list(main) + [it.to_dict() for it in reserved]


# ---------------------------------------------------------------------------
# Render for injection (spec §6.3 layout)
# ---------------------------------------------------------------------------


def _row_summary(row: Dict[str, Any]) -> str:
    """Compact one-line summary of a fetched row (no LLM)."""
    # Priority: summary / entry / description / content
    for key in ("summary", "entry", "description", "content", "note"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    # Fallback: dump a few key fields
    parts = []
    for key in (
        "event_id",
        "type",
        "filename",
        "id",
        "wing_a",
        "room_a",
        "wing_b",
        "room_b",
        "correlation_id",
        "writer",
        "timestamp",
    ):
        v = row.get(key)
        if v is not None:
            parts.append(f"{key}={v}")
    return " ".join(parts) if parts else "(empty row)"


def render_augmentation_block(
    *,
    task: Dict[str, Any],
    fetcher: Optional[AugmentationSource],
    explicit: bool = False,
    config: Optional[RecallLoopConfig] = None,
) -> str:
    """Render the pillar-3 reserved items as a section-labelled block.

    Layout (spec §6.3 — ordered, only non-empty sections emitted):
        [BRIDGE/<domain>]
        - provenance: <text>

        [SESSION LOG]
        - provenance: <text>

        [COORDINATION]
        - provenance: <text>

    If no sources fired or all fetches failed → returns "" (empty block,
    caller should not inject an empty augmentation section).

    Deterministic: same task + same fetched data → same block.
    """
    items = augment(task=task, fetcher=fetcher, explicit=explicit, config=config)
    if not items:
        return ""

    # Group by section (preserving first-seen order = _SOURCE_ORDER)
    groups: Dict[str, List[AugItem]] = {}
    for it in items:
        groups.setdefault(it.section, []).append(it)

    lines: List[str] = []
    for section, rows in groups.items():
        lines.append(f"[{section}]")
        for it in rows:
            lines.append(f"- {it.provenance}: {_row_summary(it.content)}")
        lines.append("")  # blank line separator
    # Remove trailing blank line
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
