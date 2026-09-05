"""Bounded multi-hop KG subgraph extraction (IMPL #167).

Pure helpers that convert a flat list of MemPalace KG facts into a bounded
subgraph structure without any I/O.  All MCP/server interaction happens in
:meth:`_McpClient.kg_subgraph`; the callers of  :func:`build_subgraph` 
and :func:`render_subgraph` have no dependency on the transport layer at all.

Fact dict shape (from ``mempalace_kg_query``)::

    {
      "direction": "outgoing" | "incoming",
      "subject": "EntityName",
      "predicate": "relation_name",
      "object": "NeighbourEntityNameOrDescription",
      "valid_from": str | None,
      "valid_to":   str | None,
      "confidence": float,
      "source_closet": str | None,
      "current":    bool,
    }
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

# Neighbour strings longer than this are treated as free-text descriptions
# (not traversable named entities) rather than graph nodes.
_NEIGHBOR_MAX_LEN = 80


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_subgraph(
    entity: str,
    fetch_facts: Callable[[str], Optional[List[Dict[str, Any]]]],
    hops: int = 1,
    limit: int = 10,
    relations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Expand *entity* up to *hops* layers deep using *fetch_facts*.

    Args:
        entity: seed entity name.
        fetch_facts: callable that returns a flat list of fact dicts for a
            given entity, or ``None`` when the entity is unreachable / the
            connection is down.
        hops:  0 = seed only, 1 = one neighbour layer, 2 = two layers (capped at 2).
        limit: max total relations to include in the returned subgraph.
        relations: if non-empty, only facts whose ``predicate`` appears in this
            list are kept (useful for filtering by relation type).

    Returns:
        ``{"entity": ..., "hops": ..., "entities": [...],
           "relations": [...], "source_memories": [...], "count": int,
           "complete": bool}``
    """
    hops = max(0, min(hops, 2))
    limit = max(1, limit)
    rel_filter: Optional[Set[str]] = set(relations) if relations else None

    def _allowed(f: Dict[str, Any]) -> bool:
        if rel_filter is None:
            return True
        pred = str(f.get("predicate") or f.get("relation") or f.get("type") or "")
        return pred in rel_filter

    entities: Set[str] = {entity}
    all_rels: List[Dict[str, Any]] = []
    memories: List[Dict[str, Any]] = []

    # -- Seed entity's own facts (always fetched, regardless of hops) --------
    seed_facts_raw = fetch_facts(entity)
    seed_facts = [f for f in (seed_facts_raw or []) if _allowed(f)]
    _ingest(entity, seed_facts, entities, all_rels, memories)

    # -- Multi-hop expansion --------------------------------------------------
    visited_set: Set[str] = {entity}
    next_layer: Set[str] = set()
    complete: bool = True

    for _hop in range(1, hops + 1):
        # First iteration: derive neighbours from the seed facts.
        if not next_layer:
            for f in seed_facts:
                nb = _neighbour(entity, f)
                if nb and nb not in visited_set and len(nb) <= _NEIGHBOR_MAX_LEN:
                    next_layer.add(nb)
            if not next_layer:
                complete = True
                break

        # Cap per-hop expansion so a fan-out can't blow past ``limit``.
        if len(next_layer) > limit:
            next_layer = set(sorted(next_layer)[:limit])
            complete = False

        pending: Set[str] = set()
        for nb in sorted(next_layer):
            if len(all_rels) >= limit:
                complete = False
                break
            nb_facts = fetch_facts(nb)
            if not nb_facts:
                continue
            nb_facts = [f for f in nb_facts if _allowed(f)]
            _ingest(nb, nb_facts, entities, all_rels, memories)
            for f in nb_facts:
                nn = _neighbour(nb, f)
                if (
                    nn is not None
                    and nn not in visited_set
                    and nn not in next_layer
                    and len(nn) <= _NEIGHBOR_MAX_LEN
                ):
                    pending.add(nn)

        visited_set |= next_layer
        next_layer = pending

    relations_out = all_rels[:limit]
    if len(all_rels) > len(relations_out):
        complete = False

    return {
        "entity": entity,
        "hops": hops,
        "entities": sorted(entities),
        "relations": relations_out,
        "source_memories": memories,
        "count": len(relations_out),
        "complete": complete,
    }


def render_subgraph(subgraph: Dict[str, Any]) -> str:
    """Return a printable multi-line representation of a subgraph dict."""
    e = subgraph.get("entity", "?")
    hops = subgraph.get("hops", 0)
    n = subgraph.get("count", 0)
    lines = [
        f"KG Subgraph: {e}  (hops={hops}, {n} relations, "
        f"{'complete' if subgraph.get('complete') else 'truncated'})",
        "=" * 62,
    ]

    entities = subgraph.get("entities") or []
    if entities:
        lines.append("Entities:")
        for ne in entities:
            lines.append(f"  \u2022 {ne}")
        lines.append("")

    relations = subgraph.get("relations") or []
    if relations:
        lines.append("Relations:")
        for r in relations:
            conf = r.get("confidence")
            conf_str = (f"  conf={conf:.2f}" if conf is not None else "")
            d = r.get("direction", "outgoing")
            frm = r.get("from", "?")
            to = r.get("to", "?")
            pred = r.get("predicate", "related_to")
            lines.append(f"  {frm} --[{pred}]--> {to}  ({d}){conf_str}")
        lines.append("")
    else:
        lines.append("  (no relations found)")

    memories = subgraph.get("source_memories") or []
    if memories:
        lines.append("Source memories:")
        for m in memories:
            lines.append(f"  \u2022 {m.get('key', '?')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _neighbour(entity: str, fact: Dict[str, Any]) -> Optional[str]:
    """Return the traversable neighbour of *entity* implied by *fact*."""
    subj = str(fact.get("subject") or "")
    obj = str(fact.get("object") or "")
    direction = str(fact.get("direction") or "outgoing")

    candidate = obj if direction == "outgoing" else subj
    if candidate and candidate != entity and len(candidate) <= _NEIGHBOR_MAX_LEN:
        return candidate
    return None


def _ingest(
    from_entity: str,
    facts: List[Dict[str, Any]],
    entities: Set[str],
    relations: List[Dict[str, Any]],
    memories: List[Dict[str, Any]],
) -> None:
    for f in facts:
        subj = str(f.get("subject") or from_entity)
        obj = str(f.get("object") or "")
        pred = str(
            f.get("predicate")
            or f.get("relation")
            or f.get("type")
            or "related_to"
        )

        entities.add(subj)
        if obj and len(obj) <= _NEIGHBOR_MAX_LEN:
            entities.add(obj)

        rel_entry: Dict[str, Any] = {
            "from": subj,
            "to": obj,
            "predicate": pred,
            "confidence": f.get("confidence", 1.0),
            "direction": f.get("direction", "outgoing"),
        }
        if f.get("valid_from"):
            rel_entry["valid_from"] = f["valid_from"]
        if f.get("valid_to"):
            rel_entry["valid_to"] = f["valid_to"]
        relations.append(rel_entry)

        sk = f.get("source_closet") or f.get("source_memory") or f.get("drawer_id")
        if sk:
            memories.append({"key": sk})
