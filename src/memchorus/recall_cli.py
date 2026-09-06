"""memchorus-recall CLI (IMPL #167 / #163.4).

Usage::

    memchorus-recall kg <entity> [--hops N] [--limit M] [--relations comma,list]
                              [--profile NAME] [--json]
    memchorus-recall project <name> [--profile NAME] [--json]

The ``kg`` subcommand prints a bounded Knowledge-Graph recall for *entity* as
either a human-readable multi-line string or a JSON array, so it can be piped
to ``jq`` or fed into an agent's context window directly.

The ``project`` subcommand (IMPL #163.4, spec §4.7) prints the *keyed* project
record — the ``{location, standard, reconciled}`` contract from spec §3.3 —
resolved by the orchestrator's ``resolve_project_record`` API.  Both subcommands
follow the same graceful-degradation pattern: a missing pipeline degrades to a
clear status line on stderr and exit 1, never a traceback.

Exit-code semantics (both subcommands):
    0   success (zero or more relations returned / record resolved).
    1   pipeline unreachable, or ``resolve_project_record`` returned ``None``
        (orchestrator not registered, source has no API — the base-class
        no-op degrade is treated as an explicit "no data here" signal).
    2   usage error (missing argument, bad value).

OPSEC (spec §0 glossary): user-facing text uses ``<workspace>/<project>/…``
placeholders.  Stored ``canonical_root`` values are surfaced as-is from the
record — they are *data*, not our strings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional


def _resolve_source() -> Optional[Any]:
    """Obtain an object exposing ``recall_kg`` / ``resolve_project_record``.

    Resolution order:

    1. The globally registered ``MemoryOrchestrator`` (via
       ``memchorus.get_orchestrator``) — fans out to every source that
       backs a KG or a keyed project record.
    2. A fresh :class:`MemPalaceMemorySource` — used when
       ``get_orchestrator`` returns None (e.g. outside a running Hermes
       agent session).  The base-class no-op contract on both APIs means a
       direct ``MemPalaceMemorySource`` with no backing palace will
       surface as "no data here", not as a crash, which is the graceful
       path the operator expects.

    Returns None when neither path is available.
    """
    try:
        from memchorus import get_orchestrator

        orch = get_orchestrator()
        if orch is not None and (
            hasattr(orch, "recall_kg") or hasattr(orch, "resolve_project_record")
        ):
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


def _render_project_text(record: Dict[str, Any]) -> str:
    """Distinct-channel rendering of a resolved project record (§3.3 / §4.6).

    Three separate labeled blocks (location / standard / reconciled), ordered
    TOP-BOTTOM so the canonical pointer sits above any scratch-path report —
    mirroring the ``_format_context_block`` rendering the live agent injects.
    This is NOT a ``key=…`` / ``source_id=…`` DB dump (spec §3.3): each
    pointer line is a self-contained sentence, and ``reconciled`` entries are
    *labeled* ("a scratch alias, NOT the working copy"), never promoted.
    """
    loc = record.get("location") or {}
    std = record.get("standard") or {}
    rec = record.get("reconciled") or []

    lines: List[str] = []
    if loc:
        lines.append(
            "location: {root} (source: {src}, verified_at: {v})".format(
                root=loc.get("canonical_root", "?"),
                src=loc.get("source", "?"),
                v=loc.get("verified_at") or "<derived: unverified>",
            )
        )
    if std:
        topics = std.get("topics") or []
        topics_str = ", ".join(topics) if topics else "<none>"
        lines.append(
            "standard: skill={skill} doc={doc} gist={gist} topics=[{tr}]".format(
                skill=std.get("skill", "?"),
                doc=std.get("doc_path", "?"),
                gist=std.get("gist", ""),
                tr=topics_str,
            )
        )
    if rec:
        lines.append(f"reconciled: {len(rec)} scratch path(s) labeled:")
        for e in rec:
            lines.append(
                "  role={role} path={path} ({relation})".format(
                    role=e.get("role", "scratch"),
                    path=e.get("path", "?"),
                    relation=e.get("relation", ""),
                )
            )
    else:
        lines.append("reconciled: <none>")
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
            "unreachable: MemPalace KG not reachable (entity={entity!r}). "
            "Check that the MemPalace MCP server is running and that memchorus "
            'has the "mcp" extra installed.'.format(entity=entity),
            file=sys.stderr,
        )
        return 1

    if as_json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(_render_text(results))
    return 0


def _run_recall_project(source: Any, project_name: str, as_json: bool) -> int:
    """Resolve the keyed project record and print it (IMPL #163.4).

    Mirrors the ``kg`` pattern: zero is returned on a valid response (the
    record is structurally present even when a channel fell through to the
    §2.4 derived default), 1 when the source has no resolver, raised, or
    returned None for the API (the base-class no-op degrade is "no data
    here", which the operator acts on by registering a real orchestrator).
    """
    resolver = getattr(source, "resolve_project_record", None)
    if not callable(resolver):
        print(
            "unreachable: no resolve_project_record API on the current source "
            "(orchestrator not registered or a source without keyed records). "
            "Run inside a Hermes agent session, or register a source that "
            "backs the project:<name> namespace.",
            file=sys.stderr,
        )
        return 1
    try:
        record = resolver(project_name)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if record is None:
        print(
            "no data: no keyed project:<name> record was resolvable for "
            "'{name}'. The orchestrator fell back to the base-class no-op "
            "degrader (spec §4.5) — this usually means no source in this "
            "process backs the project record namespace.".format(
                name=project_name
            ),
            file=sys.stderr,
        )
        return 1

    # Spec §3.3: location + standard are *independent* — always present.
    # Tolerate either missing (defensive) but never let it crash the CLI.
    if not isinstance(record, dict):
        print(f"error: resolve_project_record returned {type(record).__name__}, expected dict",
              file=sys.stderr)
        return 1
    record.setdefault("location", {})
    record.setdefault("standard", {})
    record.setdefault("reconciled", [])

    if as_json:
        print(json.dumps(record, indent=2, default=str))
    else:
        print(_render_project_text(record))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memchorus-recall",
        description=(
            "Query the MemChorus / MemPalace Memory Graph: multi-hop KG "
            "relations touching a named entity (``kg``), or a keyed "
            "project-record resolution for a named project (``project``)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------------ #
    # kg <entity>                                                        #
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # project <name>  (IMPL #163.4, spec §4.7)                           #
    # ------------------------------------------------------------------ #
    proj_p = sub.add_parser(
        "project",
        help="Resolve the keyed project:<name> record (location / standard / reconciled)",
    )
    proj_p.add_argument("name", help="Project name or 'project:<name>' key")
    proj_p.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Output the raw record as JSON (pipeable to jq)",
    )
    proj_p.add_argument(
        "--profile", default=os.environ.get("HERMES_PROFILE") or "default",
        help="Hermes profile name (default: $HERMES_PROFILE or 'default')",
    )

    args = parser.parse_args(argv)
    command = getattr(args, "command", None)
    if command not in ("kg", "project"):
        parser.error(f"unknown command: {command!r}")

    source = _resolve_source()
    if source is None:
        print(
            "error: could not obtain a KG/project-capable source. "
            "Is memchorus installed? Is the MemPalace MCP server reachable?",
            file=sys.stderr,
        )
        return 1

    if command == "kg":
        entity = str(args.entity)
        hops = max(0, min(int(args.hops), 2))
        limit = max(1, int(args.limit))
        relations: Optional[List[str]] = None
        if args.relations:
            relations = [x.strip() for x in args.relations.split(",") if x.strip()]
        return _run_recall_kg(
            source=source,
            entity=entity,
            hops=hops,
            limit=limit,
            relations=relations,
            as_json=args.as_json,
        )

    # command == "project"
    return _run_recall_project(
        source=source,
        project_name=str(args.name),
        as_json=args.as_json,
    )


if __name__ == "__main__":
    sys.exit(main())
