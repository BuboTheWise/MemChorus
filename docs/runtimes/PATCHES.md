# Runtime Patches

This directory documents modifications made to installed third-party packages that cannot be tracked directly by git because they live in `pipx` / `site-packages` rather than the repo. These are **runtime patches** — they survive reboots but will be **overwritten by `pipx upgrade <package>`**.

## Active Patches

| Patch | Target Package | File Modified | Purpose | Status |
|-------|---------------|---------------|---------|--------|
| Phase 4 KG Isolation | mempalace (pipx) | `mempalace/mcp_server.py` | Per-profile KnowledgeGraph SQLite DB isolation via `MEMPALACE_PALACE_PATH` env var | Active — verified 2026-07-13 |

### Phase 4: KnowledgeGraph Per-Profile Isolation

**Problem:** ChromaDB (doc/vector database) was correctly isolated per profile via `MEMPALACE_PALACE_PATH`, but the KnowledgeGraph SQLite DB always fell back to a shared global path (`~/.mempalace/knowledge_graph.sqlite3`), causing cross-profile knowledge-fact contamination between Bubo, Cthugha, and Grok-Reasoner.

**Fix:** Patch `mcp_server.py` (pipx-installed) to derive KG DB path from `MEMPALACE_PALACE_PATH`, placing each profile's graph at `~/.mempalace/<profile>/knowledge_graph.sqlite3`.

See [PATCHES-PHASE4-KG-ISOLATION.md](./PATCHES-PHASE4-KG-ISOLATION.md) for the full before/after diff and verification results.

**Upstream:** This env-var support should be contributed natively to mempalace so it persists across upgrades. Issue / PR pending.

## Reverting Patches

To revert any patch, replace the modified file with the original from a fresh `pipx install` or restore from backups stored in the task workspace that created each patch. When upgrading mempalace via pipx, re-apply all active patches after the upgrade:

```bash
pipx upgrade mempalace
# THEN re-apply runtime patches manually or via script
```

## Notes

- We intentionally never modify third-party source editable-local paths — packages are always installed cleanly from GitHub. Runtime patches are an exception for rapid isolation testing.
- The goal is to upstream these changes so the pipx-installed package respects profile-aware configuration natively, eliminating the need for any runtime patch whatsoever.
