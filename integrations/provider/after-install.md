# MemChorus — after-install (Hermes Memory Provider)

Run this **once** after `hermes import` (or `hermes memory setup memchorus`)
on a host that does not already have the MemChorus SDK importable.

## 1. Install the SDK — the one native step

MemChorus ships as a buildable source tree (not on PyPI). The provider is a
thin adapter over the SDK, so the SDK itself must be installed for orientation
to work. On a clean host:

```bash
pip install "memchorus @ git+https://github.com/BuboTheWise/MemChorus.git@master"
```

Verify it resolves:

```bash
python -c 'import memchorus; print(memchorus.__version__)'
```

## 2. Optional — MCP tool surface (recall / remember)

The orientation block works without this. The on-demand `memchorus_recall` and
`memchorus_remember` MCP tools need the `mcp` extra:

```bash
pip install "memchorus[mcp] @ git+https://github.com/BuboTheWise/MemChorus.git@master"
```

## What Hermes already handled for you

`hermes memory setup memchorus`:

- auto-installed the index-resolvable hard dependencies (pydantic, PyYAML,
  OpenTelemetry, packaging) declared in `plugin.yaml` → `pip_dependencies`;
- ran the `import memchorus` probe and, if it was missing, printed the
  one-line install above (from `external_dependencies`);
- pinned `memory.provider = memchorus` in `config.yaml` via `post_setup`.

## First turn (recorded)

After step 1, the first `hermes -p <profile> chat` turn injects the MemChorus
orientation header (project + active task + recall status) even before the
model's first reasoning step — that is the re-provisioning path this issue
(#284 Issue B, Scope 1) restores.

**Recorded fixture (acceptance B1).** The first-turn contract — orientation
block is non-empty, state-labelled, and produced with exactly one background
prefetch worker — is asserted in
`tests/test_284b_scope4_orientation_states.py` (reusing the #262 / PR #281
direct-adapter orientation contract in `tests/test_orientation.py`).

**Hook parity (scope 5).** The imported provider implements the full
`MemoryProvider` ABC directly — including `on_session_end` — so it injects the
same session-start orientation, the same recall/remember tool surface, and the
same durable end-of-session write as the in-tree direct-adapter path. There is
no separate hook registration: `plugin.yaml` (see `first_turn.hooks: inline`)
is the single source of truth, so no second hook path can double-inject.

**No duplicate background activity (scope 6).** The provider holds a single
prefetch worker thread per instance. A second `queue_prefetch` while one is
in-flight is a no-op (`worker.is_alive()` check), so a repeated query never
stacks threads; the worker is a daemon and is joined with a bounded wait on
session end, so it never outlives the session and never blocks the caller.

## Active development (in-tree, not the imported copy)

For in-tree development you may use an editable build of the local source tree
(dev-only recipe; see ONBOARDING.md). In production, always install from the
pushed GitHub commit above — never an editable shim.
