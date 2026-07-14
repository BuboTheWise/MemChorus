# Phase 4: Per-Profile MemPalace Isolation

## Problem

ChromaDB isolation was correct (each profile got its own ChromaDB via `MEMPALACE_PALACE_PATH`), but the KnowledgeGraph SQLite DB was shared across all profiles at `~/.mempalace/knowledge_graph.sqlite3`. This caused knowledge-fact cross-contamination between Bubo, Cthugha, and Grok-Reasoner.

## Fix

Patch `mcp_server.py` (pipx-installed mempalace package at `/home/bubo/.local/share/pipx/venvs/mempalace/lib/python3.14/site-packages/mempalace/mcp_server.py`, line 31):

**Before:**
```python
from .knowledge_graph import KnowledgeGraph

_kg = KnowledgeGraph()
```

**After:**
```python
from .knowledge_graph import KnowledgeGraph

# Derive per-profile KG path from MEMPALACE_PALACE_PATH so each profile
# gets its own isolated knowledge graph (not shared across profiles).
import os as _os  # noqa: E402
_profile_kg_path = None
_mpc_palace = _os.environ.get("MEMPALACE_PALACE_PATH") or _os.environ.get("MEMPAL_PALACE_PATH")
if _mpc_palace:
    _profile_kg_path = _os.path.join(_mpc_palace, "knowledge_graph.sqlite3")

_kg = KnowledgeGraph(db_path=_profile_kg_path if _profile_kg_path else None)
```

Each profile's KG now lives at `~/.mempalace/<profile>/knowledge_graph.sqlite3` instead of the shared global file.

## Ad-Hoc Verification (3/3 passing)

| Check | Result |
|---|---|
| palace_path resolves from env var | `/home/bubo/.mempalace/cthugha` ✅ |
| _kg.db_path is profile-specific | `/home/bubo/.mempalace/cthugha/knowledge_graph.sqlite3` ✅ |
| KG is NOT global shared DB | Confirmed separate paths ✅ |

## Wrapper Scripts (Unchanged - Already Correct)

- `~/.hermes/profiles/cthugha/scripts/run-mempalace-mcp-server.sh` — sets `MEMPALACE_PALACE_PATH=~/.mempalace/cthugha`
- `~/.hermes/profiles/default/scripts/run-mempalace-mcp-server.sh` — sets `MEMPALACE_PALACE_PATH=~/.mempalace/bubo`
- `~/.hermes/profiles/grok-reasoner/scripts/run-mempalace-mcp-server.sh` — sets `MEMPALACE_PALACE_PATH=~/.mempalace/grok-reasoner`

## Notes

- The patch targets the installed pipx package directly. It will survive reboots but will be overwritten by `pipx upgrade mempalace`. A future upstream PR to mempalace should include this env-var support natively.
- Pyright type-hint warning on `_profile_kg_path if _profile_kg_path else None` is cosmetic — `KnowledgeGraph.__init__` accepts `db_path: str = None`, making the sentinel value valid.
