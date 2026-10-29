# Decision Spec — MemChorus #253: exposed resolver for injected pointer keys

Status: DECIDED (2026-10-28) · Decided by: orchestrator profile · Repo: BuboTheWise/MemChorus
Live base: master at ae9476a (2.0.70). Live hint code: src/memchorus/hooks.py L1926–L1944.

## Problem (from #253)
The recall block injects pointers as `read it: retrieve(key='…')`, but `retrieve()` is in-process
only: no MCP tool, no CLI subcommand, no drawer mapping. Every agent-tool surface 404s or
"invalid choice". Cross-profile pointers (`source=<other-profile>`) make it worse — even an
in-corpus resolver must route to the issuing profile's store.

## Options

- **D1 — MemChorus CLI subcommand `memchorus-recall retrieve <key>` [CHOSEN].**
  Self-contained in our repo: `recall_cli.py` already has `kg` + `project` subcommands with
  established exit-code semantics (0 = hit, 1 = miss/unreachable). `orchestrator.retrieve(key)`
  (orchestrator.py L1816) already implements source-ranked lookup with LRU cache — the resolver
  exists; it just has no surface. Add one subcommand + update the hint text to name the
  subcommand as the reachable handle. No upstream dependency, no MCP server work, one PR.

- **D2 — MemPalace upstream: shared `memchorus_retrieve` MCP tool.**
  Cleanest long-term shape (all profiles, one tool), but requires an upstream PR we cannot
  merge and a new MCP server registration per profile. Correct for the future; too heavy for
  this cycle.

- **D3 — Hint rewrite only (no new surface).**
  Cheapest, but still leaves the key unresolvable from the agent's tool box — the #253 core
  complaint stands.

## Decision: D1

## Implementation outline

1. `src/memchorus/recall_cli.py`:
   - Add `retrieve` subcommand: `memchorus-recall retrieve <key> [--profile NAME] [--json]`.
   - Route through the orchestrator's existing `retrieve(key)` (or
     `retrieve_with_source(key)` for `--json`); use the `--profile` flag for cross-store
     routing the same way `project --profile` already resolves records per profile.
   - Exit codes match the existing convention: 0 = hit (print body to stdout),
     1 = miss (cold/pruned/never-saved — same class, no distinction needed at this layer;
     the miss message should say "cold cache or never saved — re-fetch the live source"),
     2 = pipeline unreachable.
2. `src/memchorus/hooks.py` L1926–L1944: update the C13b hint to replace
   "read it: retrieve(key='…')" with "read it: `memchorus-recall retrieve <key>`" as the
   reachable handle. Keep the ID-space disambiguation sentence (mempalace_search scope) —
   that part is still correct and useful for the search-first path.
3. Version: no bump in this PR (RELEASE card bumps 2.0.70 → 2.0.71 at merge time).

## Acceptance criteria (for IMPL)

- `memchorus-recall retrieve <existing-key>` exits 0 and prints the body to stdout.
- `memchorus-recall retrieve <missing-key>` exits 1 and prints the miss message to stderr.
- New test suite in `tests/`: seed a key in the orchestrator cache, run the CLI subcommand
  via subprocess, assert exit code + stdout for hit case; assert exit code + stderr for miss
  case. The miss case must be distinguishable from the hit case (not exit 0 with empty stdout).
- The injected recall block, when rendered for a pointer key, names
  `memchorus-recall retrieve <key>` as the reachable handle (not `retrieve(key='…')`).
- OPSEC: no agent names, paths, or email domains in any diff or commit message/trailer.

## Out of scope (this cycle)

- Cross-profile store routing via MCP (D2) — follow-up spec when upstream is ready.
- MCP `memchorus_retrieve` tool registration.
