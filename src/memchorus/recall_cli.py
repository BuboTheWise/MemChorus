"""memchorus-recall CLI (IMPL #167).

Usage:
    memchorus-recall kg <entity> [--hops N] [--limit M] [--relations comma,list]
                              [--profile NAME] [--json]

Prints a bounded Knowledge-Graph recall for *entity* as either a human-
readable multi-line string or a JSON array, so it can be piped to ``jq`` or
fed into an agent's context window directly.

Exit-code semantics:
    0   success (zero or more relations returned).
    1   KG unreachable (MCP server down) or an error occurred.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional


def _resolve_source() -> Optional[Any]:
    """Obtain an object exposing ``recall_kg``.

    Resolution order:
    1. The globally registered ``MemoryOrchestrator`` (via
       ``memchorus.get_orchestrator``) — fans out to every source that
       backs a KG.
    2. A fresh :class:`MemPalaceMemorySource` — used when
       ``get_orchestrator`` returns None (e.g. outside a running Hermes
       agent session).

    Returns None when neither path is available.
    """
    try:
        from memchorus import get_orchestrator

        orch = get_orchestrator()
        if orch is not None and hasattr(orch, "recall_kg"):
            return orch
    except Exception:
        pass

    try:
        from memchorus.mempalace_memory_source import MemPalaceMemorySource

        return MemPalaceMemorySource(name="mempalace", config={})
    except Exception:
        return None


def _render_text(results: List[Dict[str, Any]]) -> str:
    """Human-friendly rendering of orchestrator.recall_kg() output."""
    if not results:
        return "(no relations found)"
    lines: List[str] = [f"{len(results)} relations:"]
    for r in results:
        content = r.get("content")
        if isinstance(content, dict) and content:
            frm = content.get("from", "???")
            to = content.get("to", "???")
            pred = content.get("predicate", "related_to")
            direction = content.get("direction", "outgoing")
            score = r.get("score")
            score_str = f" conf={float(score):.2f}" if score is not None else ""
            lines.append(f"  [{direction}] {frm} --[{pred}]--> {to}{score_str}")
        else:
            lines.append(f"  {r.get('key', '?')}")
    return "\n".join(lines)


def _run_recall_kg(
    source: Any,
    entity: str,
    hops: int,
    limit: int,
    relations: Optional[List[str]],
    as_json: bool,
) -> int:
    """Call ``source.recall_kg`` and print the result.

    Returns 0 on a valid response (even an empty list), 1 when the KG is
    unreachable or an exception was raised.
    """
    try:
        results = source.recall_kg(
            entity=entity,
            hops=hops,
            limit=limit,
            relations=relations,
        )
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if results is None:
        print(
            f"unreachable: MemPalace KG not reachable (entity={entity!r}). "
            "Check that the MemPalace MCP server is running and that memchorus "
            'has the "mcp" extra installed.',
            file=sys.stderr,
        )
        return 1

    if as_json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(_render_text(results))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memchorus-recall",
        description=(
            "Query the MemChorus / MemPalace Knowledge Graph for relations "
            "touching a named entity."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    kg_p = sub.add_parser("kg", help="KG recall for a named entity")
    kg_p.add_argument("entity", help="Seed entity name (e.g. 'MemChorus')")
    kg_p.add_argument(
        "--hops", type=int, default=1,
        help="Traversal depth: 0 (seed only), 1 (default), 2 (max)",
    )
    kg_p.add_argument(
        "--limit", type=int, default=10,
        help="Max relations to return. Default: 10",
    )
    kg_p.add_argument(
        "--relations", default=None,
        help="Comma-separated predicate filter (e.g. 'child_of,loves')",
    )
    kg_p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Output JSON instead of human-readable text",
    )
    kg_p.add_argument(
        "--profile", default=os.environ.get("HERMES_PROFILE") or "default",
        help="Hermes profile name (default: $HERMES_PROFILE or 'default')",
    )

    args = parser.parse_args(argv)
    if getattr(args, "command", None) != "kg":
        parser.error(f"unknown command: {getattr(args, 'command', None)!r}")

    entity = str(args.entity)
    hops = max(0, min(int(args.hops), 2))
    limit = max(1, int(args.limit))
    relations: Optional[List[str]] = None
    if args.relations:
        relations = [x.strip() for x in args.relations.split(",") if x.strip()]

    source = _resolve_source()
    if source is None:
        print(
            "error: could not obtain a KG-capable source. "
            "Is memchorus installed? Is the MemPalace MCP server reachable?",
            file=sys.stderr,
        )
        return 1

    return _run_recall_kg(
        source=source,
        entity=entity,
        hops=hops,
        limit=limit,
        relations=relations,
        as_json=args.as_json,
    )


if __name__ == "__main__":
    sys.exit(main())
