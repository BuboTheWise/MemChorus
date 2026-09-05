# MemChorus - Requirements

This document outlines the detailed requirements for the MemChorus memory orchestration system and tracks implementation status against shipped code.

Last audited: 2026-09-02 against commit `604a656` (origin/master, v2.0.27).

> **Workflow rule (user, 2026-08-31):** architecture/feature changes are landed in the vault
> project docs **first** as the point of truth — code, issues, and PRs follow, citing the docs.
> Never the reverse. And the live install (`~/.hermes/hermes-agent`) is read-only: every fix
> goes vault → repo PR → GitHub merge → reinstall from GitHub → retest.

## Overall Goals

| # | Goal | Status |
|---|------|--------|
| 1 | Implement MemorySource abstract base class | [x] Done — `memory_source.py` defines all 5 abstract methods |
| 2 | Build HermesDefaultMemorySource (resilient core) | [x] Done — `hermes_memory_source.py`, JSON file-backed with configurable directory |
| 3 | Build MemPalaceMemorySource adapter | [x] Done — `mempalace_memory_source.py` integrates live MCP server calls |
| 4 | Implement orchestration and optimization logic | [x] Done — `orchestrator.py` + `relevance_engine.py` with scoring, caching, deduplication |
| 5 | Follow ORGANIZATION.md structure & naming conventions | [x] Done — all source under `src/`, tests under `tests/`, standard Python layout |
| 6 | Repository installable as skill by third parties | [x] Done — README "Agent Quick Start" (per-profile MemPalace isolation + pip-from-GitHub + `memchorus-init`) fully documents install; setup.py packaging verified |

## Functional Requirements

### Feedback Loop Extensibility Requirements (NEW — uses same hooks as B-1/B-2 but for monitoring + steering)
**Problem:** The `pre_llm_call` / `post_tool_call` observation points are valuable beyond memory recall — they provide a general bus for detecting agent behavioral issues (context spiraling, output format violations, repetition patterns) and injecting corrections. Currently this capability is hardcoded if it exists at all; it needs to be pluggable.

**Implementation reality (audit 2026-09-02, v2.0.27):** the declarative-YAML surface (`custom_loops/*.yaml` loader + `schema_v1` validator) was **purged in v1.9.0** and never re-shipped; the live successor is the **correction-queue system** (`feedback_loop.py` for GH#101): corrections stored per error fingerprint, matched against decision-point categories at recall, injected as `[[FEEDBACK CORRECTION]]` blocks, auto-expire via exhaust TTL then archived. The YAML-definition surface below remains the *future* spec for user-declarable loops and is still not in `src/`:

- [ ] Declarative YAML schema for defining custom feedback loops (`schema_v1`) including: `name`, `trigger_event` (which hook fires), `conditions` (what signals to watch for), `correction_prompt` (how to steer), `cooldown_interval`, `priority`, and `enabled` flag
- [ ] Standard definition directory (`~/.hermes/custom_loops/`) — users drop `.yaml` files there and they activate without restart or core modification
- [ ] Loader validates all definitions before activation; invalid entries disabled silently with warning logs, never crash gateway process
- [ ] Correction prompt injection via existing `pre_llm_call` context path (same mechanism as memory recall) — keeps intervention soft and natural rather than authoritative system-prompt override
- [ ] Escalation pattern: lightweight checks every turn by default; fuller diagnostics when thresholds crossed (conversation length, repetition entropy, compression depth)

### Memory Source Management Requirements
- [x] MemorySource abstract base class with standard methods:
  - `save(key, value) -> bool` ✓
  - `retrieve(key) -> Any` ✓
  - `search(query, limit) -> List[Dict]` ✓
  - `is_available() -> bool` ✓
  - `get_source_info() -> Dict` ✓

### Behavioral Enforcement Requirements (CRITICAL — solves the "mechanical usage" problem)
**Problem:** Memory tools exist but agents rarely invoke them during actual work. An agent solving a problem starts working immediately without checking relevant past memories first, then forgets to save learnings afterward. Having hooks available is not enough — they must fire automatically.

- [x] Automatic pre-decision recall triggers before planning/approach choice/decision making
  - Fires via `BehavioralEnforcementManager.enforce()` hooked into `pre_llm_call` lifecycle event (B-1, commit `3bc2c29`)
  - Four DecisionPoint types cover: planning start, approach selection, tool call intent, error recovery
  - Surfaces relevant memories through orchestrator.search() or orchestrator.retrieve() with best-fit source routing

- [x] Automatic post-action storage captures significant outcomes immediately after completion
  - Fires via `AutoStorageEngine.capture_outcome()` hooked into `post_tool_call` lifecycle event
  - Detects significance categories (LEARNING, MISTAKE, DECISION, RESULT) via keyword scanning plus trivial-content filtering and deduplication
  - Routes saves through `recommended_sources()` per B-2 fix — enabled gating (AC1), priority tiering (AC2), write restrictions (AC3)

- [x] Continuous contextual presence during active work loops
  - Memories remain available participants via orchestrator retrieval cache and relevance scoring engine
  - `ContextWeight` system allows domain-aware tuning of source-type boosting in the scorer
  - Retrieval LRU cache (`_retrieve_cache`) minimizes repeated lookups within a session
  - Pre-decision recall results inject inline as memory blocks into LLM context

### Hermes Default Memory Source
- [x] Integration with local curated memory files (MEMORY.md, USER.md)
- [x] Local file-based storage using JSON serialization
- [x] Configurable storage directory (`data_dir` parameter on constructor)
- [x] Graceful degradation when files are unavailable (`is_available()` returns False on missing path)

### MemPalace Memory Source
- [x] Integration with MemPalace MCP server (live client via `mcp_local`)
- [x] Functional search, retrieval, and KG query operations — not simulated (MCP server started during bootstrap)
- [x] Configurable endpoint — connects to local MCP or accepts external URL
- [x] Availability guard — handles offline MCP gracefully with `is_available()` returning False

### MemoryOrchestrator
- [x] Registration and management of memory sources (`memory_sources` dict, `_source_enabled` flags)
- [x] Intelligent retrieval decisions based on availability + priority (`candidate_sources()`, relevance scoring, deduplication)
- [x] Optimized storage placement via `recommended_sources()` with write_type-aware routing (B-2, commit `0fc5101`)
- [x] Mandatory pre-action recall enforcement: detects decision points automatically through plugin hooks — no manual agent trigger needed
- [x] Mandatory post-action storage: captures learnings/mistakes/decisions after tool completion via AutoStorageEngine pipeline
- [x] Continuous contextual presence: LRU retrieval cache + relevance scorer keeps memories active during reasoning loops
- [x] Decision point detection: four canonical DecisionPoint types (planning, approach, intent, error) plus recursion guards against infinite recall loops
- [x] Graceful degradation: missing orchestrator returns None from hooks; unavailable sources skipped with `continue`; exceptions caught and logged

## Non-Functional Requirements

### Performance
- [x] Low overhead for real-time use — lazy bootstrap (single import on first hook fire, not at plugin load)
- [x] Minimal latency in retrieval operations — LRU cache (`_retrieve_cache`) avoids repeated source queries for same key
- [ ] Efficient storage management — deduplication via Jaccard similarity exists but consolidation/promotion between sources is not yet implemented

### Reliability
- [x] Graceful failure handling — every hook catches exceptions, logs warnings, returns None rather than crashing Hermes
- [x] Memory persistence across sessions — both sources persist (HermesDefault writes JSON files, MemPalace writes to SQLite-backed MCP)
- [ ] Recovery from partial failures — graceful degradation exists but no retry backoff or automatic re-registration of transiently-failed sources

### Extensibility
- [x] Easy to add new memory source types — implement MemorySource ABC, register with orchestrator
- [x] Clean plugin architecture for future voices — `register(ctx)` pattern + lazy bootstrap means adding a source does not affect boot time for other agents
- [ ] Version compatibility tracking per source — no version negotiation layer exists yet
- [ ] Declarative feedback loop definitions via YAML in `~/.hermes/custom_loops/` — **verified absent from `src/` (audit 2026-09-02)**; spec target only
- [ ] Loader validation pipeline for user-defined loops — schema checks before injection so malformed entries never crash gateway

## Quality Assurance

All components should include:
- [x] Comprehensive unit tests — suite spans ~114 test modules (audit 2026-09-02), including `test_palace_path_alignment.py` (guard no-op + reader-sees-writer E2E, v2.0.27), `test_mempalace_robustness_fixes.py` (28 tests, v2.0.15), multi-turn session simulation; earlier milestone count was 279 passing across 18 files (commit `3d7f82f`)
- [x] Integration validation — multi-turn session simulation harness (`tests/test_session_simulation.py`) provides real subprocess lifecycle coverage: spawn child Python process → save content → verify cross-process persistence via disk artifacts → recall from fresh interpreter. 4 test cases, commit `3d7f82f`. Individual component tests also exist (18 files total).
- [x] Documentation for each component — every module carries extensive docstrings (187+ total across 9 source files)
- [x] Clear error handling and logging — logger at module level in every file; warnings on failures, exceptions caught and logged without leaking

## Implementation Constraints

### Development Environment
- [x] Works within Hermes ecosystem — plugin loaded via `plugins.enabled` in config.yaml, hooks registered through Hermes `ctx.register_hook` API
- [x] Follows established project structure conventions — src/ for source, tests/ for tests, setup.py for distribution, standard Python packaging
- [x] Uses standard Python libraries only — `install_requires` is now explicit: `pydantic>=2.0,<3.0` + `pyyaml>=5.4`; `mcp>=1.29,<3.0` lives under the `[mcp]` extra (audit 2026-09-02). The former `opentelemetry-*` gap is now **closed in #169** — the OTel runtime is a declared core dependency in the canonical root `pyproject.toml` and `memchorus-doctor --deps-check` guards it (see MemPalace DB Location Contract / packaging notes)

### Repository Structure
- [x] Properly structured for skill distribution — README "Agent Quick Start" covers per-profile install (audit 2026-09-02)
- [x] Clear installation instructions in README.md — "Prompt 1/2" agent quick start sections provide pip + config + `memchorus-init` steps (audit 2026-09-02)
- [x] All source files under src/
- [x] Setup scripts and configuration files included (setup.py with editable install support)

## Test Plan

- [x] Unit tests for each MemorySource implementation (hermes_memory_source, mempalace_memory_source — 279 total)
- [x] Integration tests for MemoryOrchestrator (orchestrator_test.py, recommended_sources test coverage for AC1-AC3)
- [x] End-to-end functionality validation — multi-turn session simulation harness via `tests/test_session_simulation.py` covers full subprocess lifecycle (spawn → save → persist → recall) with real process boundaries; 4 test cases pass (commit `3d7f82f`)
- [ ] Performance validation — no benchmark suite yet; latency under sustained hook-fire load is unmeasured
- [x] Error handling scenarios — graceful degradation on missing orchestrator, unavailable sources, and exception paths all tested

---

### MemPalace DB Location Contract (2026-08-31)

**Canonical: there is ONE source of truth for where an agent profile's MemPalace data lives.** Writer, reader, config, and docs must all agree. This is the contract that prevents the class of bug discovered on 2026-08-31.

**Path resolution chain (mempalace 3.8.0, `config.py`):**

```
1. --palace CLI flag          → set as MEMPALACE_PALACE_PATH env var by mcp_server.py
2. $MEMPALACE_PALACE_PATH     → explicit override (any profile config or env)
3. ~/.mempalace/config.json   → palace_path key (file config)
4. Default                    → ~/.mempalace/palace
```

The **palace directory** is the directory *containing* `chroma.sqlite3`. If `--palace /home/x/.hermes/profiles/<profile-a>/.mempalace`, the reader opens `/home/x/.hermes/profiles/<profile-a>/.mempalace/chroma.sqlite3`.

**Known discrepancy (RESOLVED 2026-09-02 in v2.0.27 — the table below is historical, kept as the bug anatomy):**

| Component | Path used | Result |
|---|---|---|
| **Writer** (memchorus `mempalace_memory_source` files drawers to a `palace/` subdir of the profile home) | `profiles/<name>/.mempalace/palace/` | 158 rows (real data) |
| **Reader** (mempalace-mcp `--palace` from each profile's config.yaml) | `profiles/<name>/.mempalace/` | 0 rows (empty shell) |
| **README** (MemChorus repo, v1.9+ section) | `profiles/<name>/workspace/mempalace/palace/` | dir doesn't exist on any profile |
| **MemPalace default** (no `--palace`, no env, no config.json) | `~/.mempalace/palace/` | 158 rows (original shared seed) |

**Contract (what must be true after the fix):**

1. `--palace` in **every** profile's `config.yaml` must point to a directory that *is the palace directory* (i.e., contains `chroma.sqlite3` with the real rows).
2. Writer and reader must open the **same** `chroma.sqlite3`. One path, one file.
3. The README's documented layout must match the actual on-disk layout. If the canonical location is `profiles/<name>/.mempalace/palace/`, the README says that. If it moves, README moves with it.
4. The default (pre-profile) agent's palace belongs at the **default** location (e.g., `~/.hermes/.mempalace/` or `profiles/default/.mempalace/palace/`), NOT at `profiles/<profile-b>/` (the "<profile-b>" path in `profiles/` is an artifact of pre-split history and should be migrated).
5. Profiles with **no** `.mempalace` dir (currently: <profile-c>) either get one configured or are deliberately unconfigured (noted in the vault).
6. **Regression test (first-class acceptance criterion):** After writing a drawer through the memchorus memory source, a subsequent MCP reader call (`mempalace_status` or `mempalace_search`) in the same profile must see it. This test is in the test suite, not just a manual verification.

**Evidence (2026-08-31, all three active profiles):**

```
profiles/<profile-a>/.mempalace/chroma.sqlite3  → 0 embeddings  (empty shell, reader target)
profiles/<profile-a>/.mempalace/palace/chroma.sqlite3 → 158 embeddings  (real data, writer target)
profiles/<profile-b>/.mempalace/chroma.sqlite3     → 0 embeddings
profiles/<profile-b>/.mempalace/palace/chroma.sqlite3 → 158 embeddings
profiles/<profile-c>/.mempalace/chroma.sqlite3     → 0 embeddings
profiles/<profile-c>/.mempalace/palace/chroma.sqlite3 → 158 embeddings
```

Note: <profile-c> has `mempalace` in its `config.yaml` `--palace` args block — the config block exists but the reader points at the empty shell. <profile-c>'s `config.yaml` grep for "mempalace" returns 0 lines (block may be under `mcp_servers` not `mcp_servers.mempalace` key name, or config structure differs). Investigate during fix.

**Fix owner:** maintainer. **Status: SHIPPED (v2.0.27, merged 2026-09-01, PR #157 — guard code, 7-test regression suite, README migration section, MemChorus #158 closed; upstream MemPalace #2404 at P1, OPEN).** All six contract points are met as tested: the guard normalizes reader `--palace` to the leaf dir holding `chroma.sqlite3`; `test_palace_path_alignment.py` pins "writer's drawer visible to MCP reader" E2E plus no-op cases; README "Known on-disk layout" + "Migrating an existing installation" sections match reality. **Fork trigger:** #2404 unmerged past 2 upstream release cycles. **Post-merge bookkeeping:** downgrade guard from "fix" to "legacy-layout compat" (doc comment + CHANGELOG only) via the normal vault→code cycle — no behavior change at their merge.

### Packaging Contract: Shared-Venv Dependency Coherence (2026-09-02)

**Problem (verified, 2026-09-02):** `setup.py` declares only `pydantic` + `pyyaml` (`mcp` under the extra). **No `opentelemetry-*` pin**, yet the Hermes shared venv carries OTel and `mempalace[mcp]` pulls it transitively. A `pip install 'memchorus[mcp] @ git+...master'` re-resolution can split OTel versions (observed 1.39.1 + 1.44.0 coexisting) and brick the `mempalace` CLI on import (`ModuleNotFoundError: …_exporter_metrics`). The 2026-09-02 force-re-pin to 1.44.0 is an **interim local fix only** — per the no-local-edit rule it must be superseded by the full cycle (this spec → code → maintainer PR → merge → reinstall → `pip check` clean → retest).

**Contract (what must be true):**
1. Every `memchorus[mcp]` install from GitHub leaves the shared venv `pip check`-clean, including coherent OTel versions (all `opentelemetry-*` at one version line; `opentelemetry-semantic-conventions` matched).
2. The expected co-located dependency set (chromadb's OTel requirement path, mcp, pydantic, pyyaml) is declared or documented so a fresh reinstall is deterministic.
3. **Regression gate:** post-install verification includes `pip check` + an import-level smoke test of the `mempalace` CLI, in the same pass as the per-profile 157-drawer verification.

**Owner:** maintainer (code cycle). **Status:** Spec'd 2026-09-02; **implemented in #169** — the OTel runtime is a declared core dependency in the canonical root `pyproject.toml`, `memchorus-doctor --deps-check` verifies an installed shared venv for OTel skew (`packaging.SpecifierSet` against the declared set) and `--json` emits a CI-consumable result. Contract items 1–3 are satisfied by the declared pin (item 1–2) plus the doctor gate (item 3), regression-tested in `tests/test_install_doctor_deps_check.py`.

---

## Recent Bugs (2026-07)

### KWARG Contract Mismatch — FIXED (PR #27, commit `e3f4f89`)

**Root cause:** Hooks used `kwargs.get("tool_output")` and `kwargs.get("injected_context")` against a live Hermes contract sending `"result"` and expecting return key `"context"`. Effect: hooks fired but silently returned None because values were not at expected keys.

**Impact:** All pre-LLM recall and post-tool auto-save returned empty. Zero persistence despite correct hook registration.

**Fix:** Aligned kwargs access patterns with verified Hermes turn_context.py contract. Added test coverage in `test_bug3_filters.py`.

### Recall Injection Bloat — IMPLEMENTED (v2.0.20–v2.0.22: RecallDeduplicator + calibration-driven recall cutoffs)

**Root cause:** `_format_context_block()` in `hooks.py` had no content length limit per search hit. Auto-save captured large tool outputs (40KB+), which recall then reinjected verbatim every turn — bloating LLM context with 123KB+ blocks.

**Impact:** Hook output consuming massive context window space instead of focused, relevant memories. Agent gets drowned in raw content rather than actionable recall.

**Fix planned:** Add per-hit truncation limit (e.g., first 300 chars + ellipsis) so large stored items still signal their existence without flooding context.

### Duplicate Query Echo Guard — OPEN

**Root cause:** `_is_query_echo()` import and check appeared twice consecutively in `on_post_tool_call` (lines ~210-215 and ~219-222). Dead code from patch overlap.

**Impact:** Two redundant imports per tool call cycle. Functionally harmless but wasteful and confusing.

**Fix planned:** Remove duplicate block.

## Summary of Implementation Status

As of 2026-09-02 (commit `604a656`, v2.0.27):

| Category | Requirements Met | Notes |
|----------|-----------------|-------|
| Core Interface | 5/5 complete | MemorySource ABC fully implemented |
| Feedback Loops | 0/5 declarative-YAML pending | YAML schema, loader, directory, validation, injection — all spec-drafted, **verified absent from `src/`**; live successor is the correction-queue system (`feedback_loop.py`, GH#101, `[[FEEDBACK CORRECTION]]` blocks) |
| Behavioral Enforcement | 3/3 complete | B-1 pre-decision recall + post-action storage both wired through plugin hooks |
| Hermes Default Source | 4/4 complete | JSON-backed, configurable, graceful degradation |
| MemPalace Source | 4/4 complete | Live MCP integration (not simulated) + v2.0.27 path-alignment guard (PR #157) |
| Orchestrator | 7/7 complete | Source management, priority routing, caching, enforcement |
| Performance | 2/3 partial | Low overhead achieved; consolidation/promotion pending |
| Reliability | 2/2 complete | Graceful handling verified empirically; kwarg contract mismatches fixed (PR #27) |
| Extensibility | 2/5 partial | Clean plugin arch done; version negotiation, feedback loop definitions, loader validation all pending |
| Quality Assurance | 4/4 complete | ~114 test modules incl. E2E path-alignment + robustness suites |
| Constraints | 6/6 complete | Install docs now in README "Agent Quick Start" (audit 2026-09-02); **OTel packaging gap closed — declared in `pyproject.toml`, doctor `--deps-check` gate, #169** |

**Bottom line:** The core behavioral enforcement loop is functional and the reader/writer path contract (MemPalace #2404 class) is shipped in code (v2.0.27) with regression tests, README alignment, and an upstream P1 issue. One live code-level item remains open: the **declarative feedback-loop YAML surface** — spec target only, live behavior covered by the correction-queue system. (The former OTel/shared-venv packaging gap is now **closed in #169** — the OTel runtime is a declared core dependency and `memchorus-doctor --deps-check` is the regression gate.) Remaining aspirational gaps are unchanged: performance benchmark suite, memory consolidation/promotion between sources, version negotiation, and the loader validation pipeline.
