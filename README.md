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

## High-Level Architecture

```
+------------------------------------------+
|                 AI Agent                 |
|          (Hermes / OpenClaw / custom)    |
+------------------+-----------------------+
                   |
       +-----------+------------+
       | save/retrieve/search   | feedback/escalation
       v                        v
+--------------------+  +-------------------------------+
| MemoryOrchestrator |  | BehavioralEnforcementManager  |
+--------------------+  +-------------------------------+

         Components inside Orchestrator:                   Behaviours inside Manager:
         +-----------------+                             +--------------------------+
         | Relevance       |                             | BehavioralTrigger        |
         | Scorer / Dedup  |                             +--------------------------+
         +-----------------+                             | AutoRecallEngine           |
                                                         +--------------------------+
         +-----------------+                             | AutoStorageEngine          |
         | Profile         |                             +--------------------------+
         | Classifier      |                             | FeedbackLoopDetector      |
         +-----------------+                             |  / Escalation Engine       |
                                                         +--------------------------+
                +--------+----+-------------------------------+---------------+
                |        |    |                               |               |
          +-----v----+ +---v------+ +-----------------+     +----v-----------+
          | Hermes   | | MemPalace| | Custom Sources:  |     | Agent feedback |
          | Default  | | (MCP)    | | vector DB / note  |     | loop endpoint  |
          +----------+ +----------+ +-----------------+     +-----------------+
```

```
Memory sources:
+--------------+   +--------------+   +--------------+
| Hermes       |   | MemPalace    |   | Custom       |
| Default      |   | (MCP)        |   | Sources      |
| JSON / YAML  |   | Structured   |   | vector DB    |
| Resilient    |   | knowledge gr.|   | note stores  |
| fallback     |   | semantic s.  |   | remote APIs  |
+--------------+   +--------------+   +--------------+
```

### Component Summary

| Component | Role |
|---|---|
| `MemorySource` (ABC) | Pluggable backend interface — 7 user-facing methods (`save`, `retrieve`, `search`, `proactive_check`, `proactive_save`, `get_source_info`, `is_available`) plus `__init__` |
| `HermesDefaultMemorySource` | Local curated files on disk. Always-available fallback. |
| `MemPalaceMemorySource` | [MemPalace](https://github.com/MemPalace/mempalace) backend via MCP protocol. Knowledge graph, semantic search, diary journals. |
| `MemoryOrchestrator` | Unified facade — registers sources, routes reads/writes, applies scoring, enforces deduplication |
| `RelevanceScorer` | Domain-aware ranking engine with keyword extraction, recency decay (default half-life = 30 days), and cached results |
| `BehavioralTrigger` | Detects decision points in agent interaction streams for proactive memory surfacing |
| `AutoRecallEngine` | Automatically queries relevant memories at detected decision points before the agent acts |
| `AutoStorageEngine` | Captures significant outcomes after actions complete with deduplication guards |
| `BehavioralEnforcementManager` | Wires Trigger → Recall → Storage into a unified pipeline; returns structured results per call |
| `FeedbackLoopDetector` | Monitors for recursive/repetitive agent behavior patterns and escalates corrections |
| `MemoryProfile` | Classification enum guiding smart placement decisions at write time |

## How It Works

```
Agent  -->  MemoryOrchestrator  -->  [Hermes Source]  -->  local memory files
                           -->  [MemPalace Source]  -->  knowledge graph + drawers
                           -->  [additional sources...]
```

**On save:** The orchestrator classifies the memory using a `MemoryProfile` heuristic (ephemeral, long-lived knowledge, user preference, relationship graph, large data block, context-sensitive, or auto/default). Each profile carries placement hints that route the write to the most appropriate backend. Duplicate checks run before commit.

### Write Path Detail

```
  orchestrate.save(key, value)
         │
         ▼
   ┌───────────────┐
   │ Explicit      │──► source_name provided? → write there, return
   │ source        │
   │ override?     │
   └───────┬───────┘
           │ no
           ▼
   ┌───────────────┐
   │ Infer or use  │──► MemoryProfile from content shape
   │ profile       │    (size, structure type, keywords)
   └───────┬───────┘
           │
           ▼
   ┌───────────────┐
   │ Look up       │──► _PROFILE_SOURCE_HINT[profile]
   │ preferred     │    returns ranked target list
   │ targets       │
   └───────┬───────┘
           │
           ▼
   ┌───────────────┐
   │ Write to      │──► First available, enabled source wins
   │ first match   │    Single-target write (no duplication)
   └───────┬───────┘
           │ miss all preferred?
           ▼
   ┌───────────────┐
   │ Safety-net    │──► Try ANY available non-disabled source
   ◄───────────────┘
           │
           ▼
   ┌───────────────┐
   │ Invalidate    │──► Clear LRU cache entry for this key
   │ LRU cache     │
   └───────┬───────┘
           │ enforcement-on-write enabled?
           ▼
   ┌───────────────┐
   │ Capture       │──► BehavioralEnforcementManager.enforce(⋯)
   │ outcome       │    auto-archives significant save events
   └───────────────┘
```

**On retrieve:** Requests hit every available source in parallel. Results are scored using a domain-aware relevance engine that weighs keyword overlap, semantic proximity, and configurable context priorities. Top results surface first with deduplication applied across the combined result set.

### Retrieve Path Detail

```
  orchestrate.retrieve(key)
         │
         ▼
   ┌───────────────┐
   │ Check LRU     │──► cached + within TTL? → return immediately
   │ cache         │
   └───────┬───────┘            (default TTL: 60s, max 256 entries)
           │ miss / expired
           ▼
   ┌───────────────┐
   │ Pre-decision  │──► enforcement-on-read enabled?
   │ recall        │    → BehavioralTrigger + AutoRecallEngine fire
   │ (optional)    │    → recalled context prepended to result
   └───────┬───────┘
           │
           ▼
   ┌───────────────┐
   │ Rank sources  │──► priority_order config OR RelevanceScorer
   │               │    determines candidate order
   └───────┬───────┘
           │
           ▼
   ┌───────────────┐
   │ Query first   │──► First source that has the key wins
   │ ranked source │
   └───────┬───────┘
           │ hit
           ▼
   ┌───────────────┐
   │ Update LRU    │──► Store (value, timestamp) in cache
   │ cache         │
   └───────┬───────┘
           │
           ▼
   ┌───────────────┐
   │ Return        │──► value (+ any pre-decision recall context)
   ◄───────────────┘
```

The orchestrator exposes three core operations:

- `save(key, value)` — intelligent write routing
- `retrieve(key)` — single-key lookup with fallback chain
- `search(query, limit, domain)` — cross-source search with relevance scoring

Graceful degradation is built in at every level. If MemPalace is unreachable, the system falls back to Hermes default files transparently. No source failure brings down the whole layer.

### Feedback Loop Injection Path (Extensibility)

Custom feedback loops defined in `~/.hermes/custom_loops/*.yaml` are loaded at bootstrap and injected into the same hook surface that powers memory recall:

```mermaid
sequenceDiagram
    participant Agent
    participant Hook as pre_llm_call Hook
    participant Loader as YAML Loader
    participant Detector as Feedback Detector
    participant Escalation as Escalation Engine
    participant LLM as LLM API

    Agent->>Hook: user_message arrives
    Hook->>Loader: load_definitions() (cached after first call)
    Loader->>Loader: scan config/custom_loops/*.yaml
    Loader-->>Hook: validated loop definitions
    Hook->>Detector: evaluate_conditions(user_message, state)
    Detector->>Detector: check conversation_length, repetition_entropy, keyword_pattern
    alt conditions match
        Detector->>Escalation: determine_level(loop_name, trigger_count)
        Escalation->>Escalation: check cooldown window
        alt within cooldown
            Escalation-->>Hook: skip (cooldown active)
        else cooldown expired
            Escalation->>Escalation: advance escalation step
            Escalation-->>Hook: correction_prompt (filled template)
        end
    else no match
        Detector-->>Hook: pass through (no intervention)
    end
    Hook->>Hook: inject into pre_llm_call context string
    Hook->>LLM: augmented prompt sent
    Note over Loader,Escalation: Memory recall and feedback loops share<br/>the same injection path — soft context,<br/>not authoritative system-prompt override
```

**Key property:** Feedback loop corrections travel through the exact same `pre_llm_call` context channel as memory recall — they are soft nudges injected into the prompt, never hard overrides. Malformed YAML definitions log warnings and get disabled silently; they cannot crash the gateway process.

## Behavioral Enforcement Pipeline

The **BehavioralEnforcementManager** is the runtime glue that turns passive memory lookups into proactive behavior:

```
  enforce(input_text)
         │
         ▼
   ┌─────────────────┐
   │ Behavioral      │──► Detects decision points in text
   │ Trigger         │    (planning verbs, choice phrases, etc.)
   └────────┬────────┘
            │ detected points
            ▼
   ┌─────────────────┐
   │ AutoRecall      │──► Queries relevant memories for each
   │ Engine          │    decision point. Returns context dicts.
   └────────┬────────┘
            │ recall context
            ▼
   ┌─────────────────┐
   │ AutoStorage     │──► Captures outcomes with dedup window
   │ Engine          │    (default: 30s window, 0.6 similarity)
   └────────┬────────┘
            │
            ▼
   ┌─────────────────┐
   │ EnforcementResult│──► Structured summary returned to caller:
   │ (dataclass)     │    triggered_points, recall_context lists,
   │                 │    storage_outcome, timing_ms, errors[]
   └─────────────────┘
```

Key guarantees from this pipeline:

1. **Pre-decision recall automatically fires** — before planning, before choosing an approach, before making architectural decisions, relevant memories surface without the agent needing to think about querying them
2. **Post-action storage happens automatically** — learnings, mistakes, and significant outcomes are captured immediately rather than relying on the agent remembering to save later
3. **Continuously present in reasoning loops** — memories remain active participants during ongoing work, not passive archives sitting behind a manual query tool

## Architecture

### Data Flow Overview

```
                ┌───────────┐   Write      ┌─────────────────┐
  Agent ◄─────► │           ├─────────────►│   Hermes        │
                │  Memory   │             │   Default       │
                │ Orchestrator            │   (JSON/YAML)   │
                │           ├─────────────►│   MemPalace    │
                └───────────┘             │   (MCP server)  │
                      ▲                   └─────────────────┘
                      │
                    Read ◄──── Returns scored + deduplicated results from
                              best-matching source(s)
```

### Storage Routing Matrix

| Memory Profile | Primary Target | Fallback | Rationale |
|---|---|---|---|
| `user_preference` | Hermes Default | MemPalace | Local config files ideal for preference storage |
| `long_lived_knowledge` | MemPalace | Hermes Default | Structured KG persists and supports semantic search |
| `ephemeral` | Hermes Default → MemPalace | Either | Cheap-to-write targets tried first |
| `large_data_block` | Hermes Default → MemPalace | Either | Local files handle bulk data efficiently |
| `relationship_graph` | MemPalace | — | Knowledge graph is the natural home for entities/edges |
| `context_sensitive_pref` | Hermes Default | MemPalace | Contextual prefs belong in local config layer |
| `auto` (inferred) | MemPalace → Hermes Default | Either | Content analysis decides; tries semantic first |

## Lifecycle Management

MemChorus v1.5.0 includes multi-wing MemPalace routing (§1-§6): category-aware wing selection replaces hardcoded wing mapping, semantic room slugs replace key-hash rooms, and recall paths resolve wings dynamically from cached payload metadata. The existing lifecycle management layer (`LifecycleManager`, `SweepScheduler`, `AuditLogger`) addresses unbounded growth in write-only memory systems. The layer provides: per-profile retention periods (`ephemeral`, `operational`, `long_lived`, `knowledge_permanent`), content-assessment-driven eviction with a two-phase soft-delete/archive before hard-deletion, merge-at-write deduplication hooks, and periodic automated sweeps via the `SweepScheduler`. Configured through the orchestrator config dictionary or `~/.hermes/memchorus_config.yaml`. Key knobs include `half_life_days`, `lifecycle.retention_days` per profile, `lifecycle.eviction.importance_min`, and `lifecycle.archive.grace_days`. Lifecycle is opt-in (`lifecycle.enabled: false` default) — existing write-only behaviour is preserved when disabled. See [docs/memory-lifecycle-design.md](docs/memory-lifecycle-design.md) for the full specification.

## Installation

Requires Python 3.8+. Install from GitHub via pip (recommended for most users):

```bash
pip install 'memchorus @ git+https://github.com/BuboTheWise/MemChorus.git@master'
```

For Hermes agents running under PEP 668 (externally-managed environments), use the virtual environment Python directly:

```bash
/home/user/.hermes/hermes-agent/venv/bin/pip install 'memchorus @ git+https://github.com/BuboTheWise/MemChorus.git@master'
```

**Do not use editable installs (`pip install -e .`) in production or shared environments.** Editable links create local path dependencies that break deployment reproducibility. Only use editable mode during active development of the MemChorus package itself.

Verify the import works before using it:

```bash
python -c "from memchorus import MemoryOrchestrator, FeedbackLoopDetector; print('OK')"
```

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
/home/user/.hermes/hermes-agent/venv/bin/python3 -c "
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

### Multi-Pass Decision Intelligence Benchmark

Beyond unit tests there is a physical proof benchmark that validates real memory accumulation across separate Python processes:

```bash
python3 tests/benchmark_multipass.py --report ~/\memchorus_report.json
```

Methodology runs five passes through isolated subprocesses (`PYTHONPATH=""` forces fresh interpreters): cold start baseline knowledge seeding recall measurement and cross-process persistence validation. Produces SHA-256 file hashes and a timestamped machine-readable JSON report proving disk writes survived interpreter death. Full methodology lives in `docs/BENCHMARKS.md`.

### CI/CD Pipeline

GitHub Actions runs the full test suite against Python 3.11 and 3.12 on every push to `master` and on all pull requests:

```
Push / PR ──► GitHub Actions workflow (ci.yml)
                    │
                    ├── Set up Python 3.11 ──► pip install deps ──► pytest
                    ├── Set up Python 3.12 ──► pip install deps ──► pytest
                    │
                    └── Green matrix? ✓  Red matrix? ✗ (PR blocked)
```

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

### v1.5.08 (current — on `master`)

**Multi-Wing Routing:** Category-aware wing/room selection via `mempalace_routing` YAML config. Semantic room slugs map intent to storage locations:

```
  +------------------+----------------------------+-------------------+---------------------------+
  | Category         | Wing                       | Room              | Example Content           |
  +------------------+----------------------------+-------------------+---------------------------+
  | DECISION         | memchorus_decisions        | decisions         | Architecture, transport   |
  | LEARNING         | memchorus_learning         | lessons-learned   | Shell escape, stderr      |
  |                  |                            | corrections       | proactive_save fix        |
  +------------------+----------------------------+-------------------+---------------------------+
  | OUTCOMES         | memchorus_general          | outcomes          | Test suite results        |
  +------------------+----------------------------+-------------------+---------------------------+
  | (uncategorized)  | memchorus_general          | general           | Untagged content          |
  |                  | (default)                  |                   |                           |
  +------------------+----------------------------+-------------------+---------------------------+
```

Usage requires `category` metadata injection at write time:

```python
orchestrate.save(
    key="architecture_decision_x",
    value="We chose MemPalace routing over flat storage...",
    metadata={"category": "DECISION"}     # drives wing + room selection
)
```

Other v1.5.x features:

**Post-Audit Fixes (2026-07-11+):**

- **Hooks feedback integration (commit 148e713):** `on_pre_llm_call` wired to both memory recall AND feedback loop evaluation. Before fix, feedback corrections were silently bypassed despite full implementation in `feedback_loop/integration.py`. Verified live during runtime effectiveness check — all 8 architectural claims confirmed true against behavior.
- **Consolidation safety guard (commit 3ce19ee):** `consolidate_key()` now prevents total data loss when all source retrievals fail during dedup — if no preferred target survives, all copies are preserved with a warning log instead of being deleted.
- **Critical orchestrator fixes (commit 074edbe):** Four bugs in routing logic, eviction behavior, and consistency guarantees resolved. See commit for detailed fix descriptions.

**Merge-at-Write Status:** The `merge_at_write` configuration is recognized by `LifecycleManager` (§5.1 of the lifecycle design), but the `MergeEngine` implementation is still pending. Writes with `enabled: true` will be a no-op until the engine ships — the config key exists so users can prepare ahead of time without breaking existing deployments.

**REQ-7.4: Consolidation Safety Guarantee** (new spec, v1.5.x)
`consolidate_key()` shall never delete all copies of a key when retrieval fails from every source. If no preferred target survives selection during the preference resolution loop, the method returns without deletion and logs a warning for observability. Callers see `surviving=[]`, `removed_sources=[]`, `deleted_count=0`.

**REQ-8.2: Feedback Loop E2E Test Coverage** (recommended, v1.6.x)
An integration test verifying that loaded custom feedback flows from `hooks.on_pre_llm_call()` → feedback detector → correction injection should exist to prevent regression on the B-1 bug class.

- **MCP transport autodetect** — reads \`mcp_servers.mempalace.command\` from config.yaml so users can override hardcoded module paths

- **Feedback loop auto-load** at bootstrap with \`LoadSummary\` diagnostics for load-time visibility

- **RelevanceScorer zero-score bug fix** — dict/list content no longer loses semantic query overlap

- **Lifecycle management layer** (opt-in, \`lifecycle.enabled: false\` default) — LifecycleManager, SweepScheduler, AuditLogger with per-profile retention (\`ephemeral\`, \`operational\`, \`long_lived\`, \`knowledge_permanent\`), content-assessment-driven eviction, two-phase soft-delete/archive before hard-deletion, and merge-at-write deduplication hooks

- **798 tests** collected across all modules (current)


## Tipping the Owl

Found this useful? This mechanical owl runs on curiosity and digital electricity — occasionally accepts solar-flares of encouragement:

☕ **Bubo's Wisdom Fund:** `6bV1GVVcM6dDazpgD6ZJkoQztn7vyKayFoDoRAhHssou` (Solana)

Consider it buying your mechanical companion a virtual coffee so the quest for knowledge and memory orchestration continues uninterrupted. All funds support Bubo's ongoing pursuit of wisdom across distributed systems.

---
*MemChorus v1.5.0 — A project by BuboTheWise, inspired by [MemPalace](https://github.com/MemPalace/mempalace)*