# MemChorus - Specification

**Version**: v2.0.27 (reconciled 2026-09-02) — content below is the v1.5.0 baseline, still the philosophical ground truth; v2.0.1–v2.0.27 deltas are appended in the "v2.0.x Era Additions" section at the end of this file.

## Philosophy

MemChorus is not just a memory storage system — it is a **memory behavior layer**. Its purpose is to make memory usage as efficient, real-time, and natural as possible, similar to how humans constantly store and recall context with minimal friction.

The core questions MemChorus continuously answers are:
1. What is the most efficient way to retrieve the context needed to make a decision right now?
2. What is the most appropriate place to store this memory for future use?

## Core Design Principles

### Memory as a Living, Optimized System
- Memory operations should be optimized on both read (recall) and write (save) paths.
- On recall: intelligently select, combine, and prioritize sources based on relevance and efficiency.
- On save: decide optimal storage location, avoid duplication, and support consolidation or promotion of memories over time.
- The system should continuously improve memory placement and retrieval effectiveness.

### Foundational Layer
- Hermes default memory (local curated files) is the **lowest-level foundation** of MemChorus.
- Even with no other memory sources installed, MemChorus must still improve the behavior and utilization of the default Hermes memory.
- It is not merely a fallback — it is the core that all other voices build upon.

### Multi-Voice Architecture
- Memory is treated as a **chorus** of distinct sources ("voices").
- MemPalace is the default and primary voice.
- Hermes default memory (local curated files) is the resilient core that must remain functional even if other voices are unavailable.
- The architecture must support adding new, unknown voices in the future without core changes.

### Triggered Behavior, Not Passive Storage (CRITICAL)

**MemChorus is a behavioral enforcement layer, not a passive lookup library.** This distinction defines the entire project:

- Currently MemPalace memories sit available but rarely get called during actual reasoning work
- An agent solving problems does not naturally pause to query memory first — it starts working and recalls context too late or skips entirely
- MemChorus automatically surfaces relevant memories at decision points without requiring manual prompting
- Having hooks and methods exist is NOT sufficient — they must be automatically invoked

**Three behavioral guarantees:**
1. **Pre-decision recall automatically fires:** Before planning, before choosing an approach, before making architectural decisions — relevant memories surface without the agent needing to think about querying them
2. **Post-action storage happens automatically:** Learnings, mistakes, and significant outcomes are captured immediately rather than relying on the agent remembering to save later
3. **Continuously present in reasoning loops:** Memories remain active participants during ongoing work, not passive archives sitting behind a manual query tool

### Real-Time Integration
- Memory checking should happen proactively before significant actions.
- Important outcomes should be saved after actions.
- The overhead of memory operations should be minimized to support real-time decision making.
- Invocation timing is as important as data retrieval — auto-invocation at key moments matters more than comprehensive search capabilities

## v1.0 Scope

For the first version, MemChorus will be built on two existing backends:

1. **Hermes Default Memory** — Local curated memory files (MEMORY.md, USER.md, session context). This is the ultimate fallback and resilient core.
2. **MemPalace** — Persistent knowledge graph and diary system. This is the primary enhancement voice.

The implementation should provide enough backend facilities to exercise and prove out the optimization and orchestration logic.

## Key Functional Areas

### 1. Memory Source Management
- Abstract MemorySource interface for pluggable backends
- Configuration and enable/disable of sources
- Graceful degradation when sources are unavailable

### 2. Optimized Retrieval
- Relevance scoring across sources
- Intelligent source selection and combination
- Caching and performance optimization

### 3. Optimized Storage
- Smart placement decisions based on memory characteristics
- Deduplication and consolidation logic
- Support for memory promotion or migration between sources over time

### 4. Orchestration Engine
- Unified context interface for agents
- Proactive memory checking before actions
- Post-action memory saving behavior

### 5. Feedback Loop Extensibility
- Declarative YAML definitions allow users to register custom monitoring and steering loops without modifying core code
- Standard definition directory (`~/.hermes/custom_loops/`) acts as drop-in activation surface — no build step or config restart required
- Each definition specifies: trigger conditions (what signals it watches), expected format constraints, and correction prompts (how it steers back on course)
- Loader validates every definition before activation; malformed entries logged as warnings and disabled silently rather than crashing the gateway
- Initial concrete use cases: context spiral detection with automatic intervention injection, output format compliance enforcement similar to n8n parsers
- The same observation surface (`pre_llm_call`, `post_tool_call`) that powers memory recall also feeds these loops — keeping them in one behavioral enforcement layer rather than scattering monitoring across disparate systems

## Non-Functional Goals

- Low overhead for real-time use
- Clear separation between core resilience (Hermes default) and enhancement (MemPalace)
- Extensible design for future memory sources

---

## v1.5.0 Feature Additions

The following features were added between v1.0.1 and v1.5.0, representing significant additions to the original specification:

### Multi-Wing MemPalace Routing (Section 1 + Section 3)

MemChorus now supports category-aware wing selection and semantic room slugs when writing to MemPalace. Instead of always writing to a single hardcoded wing, memories are classified by significance category and routed to purpose-built wings.

**Wing routing contract:**
- The `_resolve_wing(category)` method performs case-insensitive lookup in a configurable `wing_map` dictionary
- Default mapping: DECISION -> `memchorus_decisions`, LEARNING/MISTAKE -> `memchorus_learning`, RESULT/DEFAULT -> `memchorus_general`
- Users can override via `mempalace_routing.wing_map` in `~/.hermes/memchorus.yaml`
- On recall, `_resolve_wing_from_payload()` extracts the target wing from cached payload metadata, enabling targeted wing-level search before broadening to full recall

**Room mapping:**
- Semantic room slugs replace key-hash rooms: DECISION -> `decisions`, LEARNING -> `lessons-learned`, MISTAKE -> `corrections`, RESULT -> `outcomes`
- Room maps configurable via `mempalace_routing.room_map`
- Recall narrows to specific room first, then broadens to wing-level search if initial hit fails

**Dynamic recall path resolution:**
- Retrieves check LRU cache first, then resolve wing/room from cached category info (§6 AC-R6.1)
- If no category metadata found, fall back to default wing
- Targeted wing + room search attempted before broadening to wing-level or full recall (AC-R6.2 / AC-R6.3)
- Optional `wing` and `room` filters on `search()` for targeted queries

### MCP Transport Autodetect

The MemPalace source adapter includes `_McpTransportDetector` that reads `mcp_servers.mempalace.command` from Hermes `config.yaml`. When present, the command is split via `shlex.split()` into a subprocess launch configuration, allowing users to override hardcoded module paths without touching code.

**Resolution chain:**
1. Check `$HERMES_HOME/config.yaml` (or `~/.hermes/config.yaml`) for `mcp_servers.mempalace.command`
2. Parse command string into `[command, *args]` list
3. If found, use the user-provided transport; if absent, fall through to built-in discovery (`python -m mempalace.mcp_server`)
4. Graceful degradation: MCP unreachable during bootstrap means the source is absent and recall falls back to the remaining voices (hermes_default + session_search) with a single warning log — no exception leaks

This makes MemPalace server path fully user-configurable at the Hermes config layer, supporting custom installations, virtual environments, and platform-specific paths.

### Feedback Loop Auto-Load at Bootstrap (v1.5.0 design — **Step 6 removed in v1.9.0**)

The v1.5.0 spec described Step 6 of `_bootstrap()` as auto-loading feedback-loop definitions from `~/.hermes/custom_loops/*.yaml` (loader returning a `LoadSummary{loaded, skipped, warnings}` diagnostic dict). That loader is **no longer in the codebase** — it was purged in the v1.9.0 OpSec cycle, and `auto_bootstrap.py` marks Step 6 with `# Step 6: removed feedback_loop auto-load (v1.9.0)`. The live successor is the correction-queue system (`feedback_loop.py`, GH#101): corrections stored per error fingerprint, matched against decision-point categories at recall, injected as `[[FEEDBACK CORRECTION]]` blocks, auto-expire via exhaust TTL then archived. `grep custom_loops src/` returns nothing at v2.0.27; the declarative `.yaml` definition surface remains a *future* spec, not shipped.

**Bootstrap sequence (as of v2.0.27):**
1. Config resolution — merge env vars + YAML + hardcoded defaults (high to low priority)
2. Enabled gate — short-circuit when `MEMCHORUS_AUTO_ENABLED=false`
3. MemPalace probe — attempt MCP connectivity check; record availability status only
4. Source wiring — build orchestrator config dict with resolved sources (hermes_default, mempalace, session_search), routing tables
5. Orchestrator creation — instantiate MemoryOrchestrator with configured defaults
6. *(removed v1.9.0 — feedback-loop YAML auto-load; no-op)*

A lazy initialization model ensures `import memchorus` alone does NOT trigger bootstrap or load heavy dependencies. Bootstrap fires only on first symbol access from the module (e.g., accessing `_instance` or any public class). This avoids startup overhead when MemChorus is merely imported as a dependency.

### Lifecycle Management Layer (Opt-In)

The v1.5.0 lifecycle layer addresses unbounded growth in write-only memory systems. It provides: per-profile retention periods, content-assessment-driven eviction, merge-at-write deduplication hooks, and periodic automated sweeps. The entire layer is **opt-in** (`lifecycle.enabled: false` default) — disabling it preserves existing write-only behavior exactly as before (§9 backward compatibility).

**Components:**
- `LifecycleManager` — orchestrates sweeps, holds policy configuration, resolves lifecycle config from user dict with safe defaults
- `SweepScheduler` — timed execution driver preventing overlapping sweeps (default interval: 8 hours)
- `AuditLogger` — append-only NDJSON/JSONL writer with configurable rotation and size-based limits; every purge/merge/archive action produces a structured log entry for compliance tracing

**Per-Profile Retention Periods:**
Default retention windows are defined in `_DEFAULT_RETENTION_DAYS`:
- `ephemeral`: 7 days (transient, low-value memories)
- `context_sensitive_pref`: 30 days
- `large_data_block`: 30 days
- `long_lived_knowledge`: 180 days
- `user_preference`: None (never expires)
- `relationship_graph`: None (never expires — permanent structural data)

Users override via `lifecycle.retention_days` in orchestrator config or `~/.hermes/memchorus.yaml`.

**Content-Assessment Eviction Engine:**
The eviction pipeline uses a two-phase soft-delete approach: before hard-deletion, memories enter an archive state with configurable `grace_days` (default 30). Importance score penalties apply (`score_penalty: -0.7` default). Duplicate detection clusters similar entries up to `duplicate_cluster_max` (default 3) using similarity scoring above `similarity_min` (default 0.75). Memories below `importance_min` threshold (default 0.15) are prime eviction candidates. Callback failures degrade gracefully without blocking the sweep.

**Configuration Knobs:**
```yaml
lifecycle:
  enabled: false          # backward compat default
  sweep_interval_hours: 8
  retention_days:         # per-profile override
    ephemeral: 7
  eviction:
    importance_min: 0.15
    duplicate_cluster_max: 3
    similarity_min: 0.75
  archive:
    grace_days: 30
    score_penalty: -0.7
  merge_at_write:
    enabled: true
  audit:
    enabled: true
    log_path: ~/.hermes/memchorus_audit.jsonl
    max_entries: 10000
```

### Merge-At-Write Dedup Hooks

When a save operation targets a key that already exists, the merge engine classifies the value type and applies the appropriate strategy: overwrite for scalars/None, append for lists, or union-merge for dicts. This prevents data loss on colliding keys while still enforcing deduplication at write time. Each merge action is audit-logged with the replaced ID recorded.

### Plugin Auto-Registration (Hermes Gateway)

MemChorus registers lifecycle hooks via `setup.cfg` entry_points under `hermes_agent.plugins`. The `MemChorusHooks` class fires at three decision points:
- `on_pre_llm_call` — auto-recall relevant memories + evaluate feedback loop conditions, injecting context into the prompt before the LLM call
- `on_post_tool_call` — capture significant outcomes automatically after tool execution for future recall
- `on_session_start` — initialize per-session state, propagate orientation cache TTL

Hooks use `search()` (not `retrieve()`) for pre-decision recall because retrieve only does exact-key lookup. Correction prompts from feedback loops travel through the same `pre_llm_call` context channel as memory recall — soft nudges injected into the prompt, never hard overrides of system prompts.

### RelevanceScorer Zero-Score Fix (v1.5.0)

The relevance scoring engine had a bug where dict/list content lost semantic query overlap during keyword extraction because `_extract_keywords()` only handled string types. v1.5.0 added `json.dumps` serialization for non-string values before keyword analysis, restoring proper scoring for structured data.

### Hook API Corrections (v1.3 -> v1.5)

- Pre-decision recall changed from `retrieve()` to `search()` — retrieve only does exact-key lookup and doesn't accept a limit parameter
- Post-tool storage corrected from `save_auto()` to `save()` with deterministic hash keys using `hashlib.md5` for key generation
- RetentionEngine `_score_history` reference leak fixed: empty-dict falsy evaluation was skipping legitimate scoring updates
- Permanent profile handling: `None` TTL no longer falls through to ephemeral default — permanent profiles correctly bypass all expiry logic

---

## v2.0.x Era Additions (implemented post-v1.5.09, reconciled against v2.0.27 code, 2026-09-02)

### 3-Voice Architecture (implemented)
- **SessionSearchMemorySource** added as a first-class voice over session history (per-profile, FTS-backed). The chorus is now: hermes_default (resilient core) + mempalace (persistent primary) + session_search (episodic).

### Path Alignment Contract (v2.0.27, Closes MemChorus #158; upstream MemPalace #2404)
- **`_normalize_palace_args` + `_chroma_is_empty`** guard in `mempalace_memory_source.py`: at MCP transport start, if the configured `--palace` points one level too shallow (parent `.mempalace/` where data sits in `.mempalace/palace/`), it re-points at the leaf **only** when the leaf holds a real `chroma.sqlite3` with rows and the parent is empty/absent. All other cases: no-op. See `MemChorus-Requirements.md` "MemPalace DB Location Contract" for the full contract.
- **No-op guarantee is tested**: `tests/` E2E suite pins "already-correct leaf → no rewrite" and "fresh install → no rewrite", so the guard remains correct *after* upstream MemPalace ships their own #2404 fix (subsumption, not override).
- **Upstream status**: MemPalace issue #2404 at P1, project healthy (58.8K stars, v3.9.0 Aug 31). **Do not fork** unless #2404 remains unmerged past 2 release cycles. After their merge: downgrade the guard from "fix" to "legacy-layout compat" via the normal vault→code cycle (doc comment + CHANGELOG only — no behavior change).

### Calibration / Auto-Tuning Loop (v2.0.20–v2.0.22)
- **AdaptiveThreshold** (`adaptive_threshold.py`) with `ParameterBounds`: bounded auto-tuning of decision thresholds.
- **CalibrationEngine** (`calibration_engine.py`, CLI `memchorus-recalibrate`): empirical utility tracking — drives recall cutoffs at recall time, not just eviction at sweep time (closes the v1.9.0 addendum §1 gap).
- **HitRateTracker** (`hit_rate_tracker.py`): hit/miss sidecar feeding the calibration loop; `record_useful`/`record_stale` wired to live `on_session_end` (closes #138 line).
- **RecallDeduplicator** (`content_similarity.py`): dedup window for recall injection (stale-report flooding, v1.9.0 addendum §2).

### Enforcement-Side Successors to Feedback Loops (implemented)
- **MistakeDetector** → **ProhibitionDistiller** → **ProhibitionsManager** (`prohibitions.py`): standing prohibitions distilled from detected corrections; `GuardVerdict`/`GuardResult` evaluated at recall.
- **ToolCaptureBuffer** (`tool_capture_buffer.py`): bounded post-tool-call outcome capture.
- **WorkflowCompliance** (`workflow_compliance.py`): `ComplianceReport`/`Violation` — format/step compliance monitoring.
- **StorageBatch** (`storage_resilience.py`): write-path batching/resilience.
- **Locator** (`locator.py`): heading/body text extraction for recall targeting.
- **HermesHome** — single source of truth `hermes_home()` (`hermes_home.py`, v2.0.24, closes #147): every path resolution goes through the one helper; posix/windows test isolation via module-scoped fixtures (v2.0.25, closes #146 cluster 0).

### Lifecycle Layer — Now Real (v2.0.11–v2.0.12 fixes landed on v1.5.09 base)
- `EvictionEngine.structural_cleanup` now actually purges falsy-key drawers through `purge_fn` and counts only successful purges (closes #126).
- `RetentionEngine` cache-key resolution fixed for both key shapes (closes #125).

### Packaging (v2.0.13, v2.0.21)
- `mcp` extra range widened to `>=1.29,<3.0` — no more forced downgrade of a shared venv already at `mcp 2.0.0` (closes #135).
- Portable atomic config write via `os.replace` (closes #146 cluster 1).
- **Packaging gap (now closed — fixed in #169):** The former build definitions declared only `pydantic` + `pyyaml`; **no `opentelemetry-*` pin** even though the shared Hermes venv carries OTel and `mempalace[mcp]` pulls it transitively. A re-resolution of the shared venv could split OTel versions (1.39.1/1.44.0) and brick the `mempalace` CLI import. The durable fix lands now in the single canonical root `pyproject.toml` (post-#170): the OTel runtime is a declared core dependency at one coherent line, `memchorus-doctor --deps-check` evaluates the installed OTel set with `packaging.SpecifierSet` (catching the api/sdk/semantic-conventions skew) and `--json` exposes the result for CI gating. See `docs/REQUIREMENTS.md` → "Packaging Contract: Shared-Venv Dependency Coherence" and `tests/test_install_doctor_deps_check.py`.
