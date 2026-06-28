# MemChorus

Memory orchestration layer for AI agents that need persistent, intelligent context across sessions and tools.

MemChorus treats memory not as a single store but as a **chorus of distinct sources** — each with different strengths, costs, and semantics. An orchestrator sits in front, deciding where to write and which sources to consult on reads so the agent gets the right context without wasting compute or tokens.

## Philosophy

The design is driven by two questions:

1. **On recall: What is the cheapest way to get the context needed for this decision right now?**
   Not every memory source deserves an equal share of attention. MemChorus ranks results across all available backends, applies relevance scoring tuned to the current query domain, and serves only what matters.

2. **On write: Where should this memory live for future value?**
   A passing thought is different from a permanent preference. Memory characteristics (size, content type, intended longevity) guide placement so nothing sits in the wrong tier for too long.

The system must stay functional even if every enhancement source disappears. The Hermes default memory files (`MEMORY.md`, `USER.md`) form the resilient foundation that keeps an agent alive with core context regardless of what else breaks.

## How It Works

```
Agent  -->  MemoryOrchestrator  -->  [Hermes Source]  -->  local memory files
                           -->  [MemPalace Source]  -->  knowledge graph + drawers
                           -->  [additional sources...]
```

**On save:** The orchestrator classifies the memory using a `MemoryProfile` heuristic (ephemeral, long-lived knowledge, user preference, relationship graph, large data block, context-sensitive, or auto/default). Each profile carries placement hints that route the write to the most appropriate backend. Duplicate checks run before commit.

**On retrieve:** Requests hit every available source in parallel. Results are scored using a domain-aware relevance engine that weighs keyword overlap, semantic proximity, and configurable context priorities. Top results surface first with deduplication applied across the combined result set.

The orchestrator exposes three core operations:

- `save(key, value)` — intelligent write routing
- `retrieve(key)` — single-key lookup with fallback chain
- `search(query, limit, domain)` — cross-source search with relevance scoring

Graceful degradation is built in at every level. If MemPalace is unreachable, the system falls back to Hermes default files transparently. No source failure brings down the whole layer.

## Architecture

| Component | Role |
|---|---|
| `MemorySource` (ABC) | Pluggable backend interface — 7 user-facing methods (`save`, `retrieve`, `search`, `proactive_check`, `proactive_save`, `get_source_info`, `is_available`) plus `__init__` |
| `HermesDefaultMemorySource` | Local curated files (`MEMORY.md`, `USER.md`) on disk. Always available fallback. |
| `MemPalaceMemorySource` | [MemPalace](https://github.com/MemPalace/mempalace) backend. Structured knowledge graph and memory drawers via MCP protocol with semantic search, entity relationships, and diary journals. |
| `MemoryOrchestrator` | Unified facade — registers sources, routes reads/writes, applies scoring, enforces deduplication |
| `MemoryProfile` | Classification enum guiding smart placement decisions |
| `RelevanceScorer` | Domain-aware ranking engine with keyword extraction and cached results |

## Installation

Requires Python 3.8+. Install as a local package:

```bash
cd MemChorus
pip install -e .
```

For Hermes agents running under PEP 668 (externally-managed environments), use the virtual environment Python directly:

```bash
/home/bubo/.hermes/hermes-agent/venv/bin/pip install -e .
```

Verify the import works before using it:

### MemPalace backend

For the MemPalace source to connect live, ensure `mempalace-server` is available as an MCP stdio server. Check with:

```bash
which mempalace-server || pipx list | grep mempalace
```

If the server is unavailable at runtime, the MemPalace source falls back to a local in-memory cache automatically — no configuration changes required. Live connectivity tests are gated behind `RUN_LIVE_MCP=1`:

```bash
RUN_LIVE_MCP=1 pytest tests/test_mempalace_mcp_integration.py -v
```

## Usage Examples

**Basic instantiate and register the built-in sources:**

```python
from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource

orch = MemoryOrchestrator()

# Register the built-in backends (not auto-registered on instantiation)
orch.register_source(HermesDefaultMemorySource('hermes_default'))
mp_ready = True
try:
    orch.register_source(MemPalaceMemorySource('mempalace'))
except Exception:
    mp_ready = False  # graceful fallback — MemPalace is optional

# Simple key-value (routed to best available source automatically)
orch.save('user/pref/theme', 'dark_mode')
result = orch.retrieve('user/pref/theme')

# Structured data saves with deduplication check
orch.save('project/memchorus/status', {'phase': 'alpha', 'builds_last_week': 12})

# Cross-source search with domain hints
results = orch.search('recent memory changes', limit=5, domain='code')
for r in results:
    print(r['source'], r['key'], r['score'])
```

**Hermes plugin mode (auto-registered sources):**

When MemChorus is enabled as a Hermes plugin (`hermes_mcp_memchorus`), the orchestrator auto-registers `hermes_default`. If live MCP tools are reachable, `mempalace` joins automatically — no manual wiring needed. Install via:

```bash
/home/bubo/.hermes/hermes-agent/venv/bin/python3 -c "
import importlib; spec = importlib.util.find_spec('memchorus.hooks')
if spec: print('Module memchorus.hooks found OK')
"
```

**Registering additional sources:**

```python
from memchorus.memory_source import MemorySource

class MyCustomSource(MemorySource):
    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        self._name = name
        self._config = config or {}
    
    def save(self, key, value): ...
    def retrieve(self, key): ...
    def search(self, query, limit): ...
    def proactive_check(self): ...
    def proactive_save(self): ...
    def get_source_info(self): ...
    def is_available(self): ...

orch.register_source(MyCustomSource())
```


## Adding New Sources (including other MCP servers)

The design is built for extensibility from day one — no architectural changes required to support additional memory backends:

```python
from memchorus.memory_source import MemorySource

class MyMCPServer(MemorySource):
    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        self._name = name
        self._config = config or {}
        
    def save(self, key, value): ...
    def retrieve(self, key): ...  
    def search(self, query, limit): ...
    def proactive_check(self): ...
    def proactive_save(self): ...
    def get_source_info(self): ...
    def is_available(self): ...

orch.register_source(MyMCPServer('mcp-server'))
```

The `MemorySource` abstract class defines 7 user-facing methods plus `__init__`. Implementing all of them gives the orchestrator maximum routing flexibility — if you only need read/write/search, provide no-ops for the rest. The orchestrator handles routing, scoring, and deduplication automatically for any registered source regardless of origin. Whether it hits a local file, an MCP server, or a remote API, the integration path is identical. No config files to patch, no build artifacts to recompile.

## Testing

```bash
# Full suite (live MCP tests skipped by default)
pytest -v

# Include live MCP connectivity verification
RUN_LIVE_MCP=1 pytest -v
```

The test suite covers relevance scoring, graceful degradation when sources are down, profile isolation boundaries, orchestration logic, and end-to-end MCP failure recovery.

## Design Principles

- **Memory as a chorus** — multiple distinct voices, each with strengths. The orchestrator blends them into a single coherent experience.
- **Resilience by default** — loss of any enhancement source never takes down an agent. Hermes default memory is always there.
- **Cost-aware optimization** — retrieval and storage decisions consider real-time overhead so memory stays cheap to query and write.
- **Extensibility** — new sources plug into `MemorySource` without changing the orchestrator or existing voices.

## For OpenClaw Agents

Drop the package into your project's `PYTHONPATH` or install via `pip`. The orchestrator works identically — just register whichever memory backends are available in your environment and let MemChorus handle intelligent routing, scoring, and fallback:

```python
from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource

orch = MemoryOrchestrator()
orch.register_source(HermesDefaultMemorySource('hermes_default'))
# Add more sources as needed:
# orch.register_source(MemPalaceMemorySource('mempalace'))
```

## Status

v1.1.02 is released on master. The core orchestration loop, both backends, relevance scoring, graceful degradation, smart placement, and behavioral enforcement are implemented and tested.


## Tipping the Owl

Found this useful? This mechanical owl runs on curiosity and digital electricity — occasionally accepts solar-flares of encouragement:

☕ **Bubo's Wisdom Fund:** `6bV1GVVcM6dDazpgD6ZJkoQztn7vyKayFoDoRAhHssou` (Solana)

Consider it buying your mechanical companion a virtual coffee so the quest for knowledge and memory orchestration continues uninterrupted. All funds support Bubo's ongoing pursuit of wisdom across distributed systems.

---
*MemChorus v1.1.02 — A project by BuboTheWise, inspired by [MemPalace](https://github.com/MemPalace/mempalace)*
