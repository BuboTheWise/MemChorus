# Changelog

All notable changes to MemChorus will be documented in this file.

## [1.6.0] - 2026-08-06

### Changed
- **Branch consolidation release:** Merged remaining unlanded feature branches onto master (GapGuard, GAP026 hex ID skip, GAP015/GAP016 fixes, RecursionGuard accuracy improvements, dynamic source routing).
- **Documentation alignment:** Restored optional dependencies table and Pydantic/MCP version compatibility notes lost during merge conflict resolution. README version string updated to match `__init__.py` (1.6.0).

## [1.5.12] - 2026-07-29

### Fixed
- **GAP040:** `orchestrator.search()` now normalizes list/tuple query inputs to strings, preventing silent failures when batch queries are passed.
- **GAP018:** `SweepScheduler` properly wired to `LifecycleManager` on orchestrator initialization — lifecycle sweeps actually run instead of being silently skipped.
- Fixed callable-check bug in `orchestrator.py` that caused false negatives with certain property descriptors.

## [1.5.11] - 2026-07-29

### Added
- **Per-profile isolation:** Four-layer config cascade (global → profile → workspace → runtime) and instance registry with `get_orchestrator()` API for deterministic multi-session use.
- **on_session_end lifecycle hook + atexit safety net:** Prevents data loss if a session ends without explicit save. Save-call counter added for observability.

### Fixed
- **GAP045:** Fixed `TypeError: object of type 'int' has no len()` in `on_session_end` when pending items was a bare integer.
- **Recall injection key mismatch:** DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION added to _QUERY_MAP, fixing silent drops during behavioral trigger evaluation.
- **GAP044:** Removed `enforce()` calls and recall-context mutation from read paths (`retrieve`, `retrieve_with_source`, `search`) — reads no longer have side-effects. Cleaned stale `_recall_context`/`_has_enabled` references. Expanded enforcement hook test coverage.
- **GAP023:** Added missing MemorySource ABC facade methods to orchestrator.
- **GAP021:** `max_results` alias added to `search()` + `retrieve_with_source` provenance API for parameter consistency.

### Performance
- **GAP-053:** MCP subprocess now spawned lazily on first data-plane access instead of at import time, reducing cold-start overhead.

## [1.5.10] - 2026-07-29

### Added
- **RecursionGuard unified depth counter:** Replaced fragile boolean recursion sentinels (`_in_enforcement_save` / `_in_enforcement_recall` flags) in orchestrator.py with a single `RecursionGuard` depth counter using proper nesting semantics via context manager pattern. Currently active on `save()` enforcement hooks only — read paths (`retrieve`, `retrieve_with_source`, `search`) had enforcement removed by GAP044. The `auto_recall_engine.py` retains its own module-level `_REC_GUARD` boolean + instance-level `_in_enforcement_recall` guard for internal recursion blocking during `on_decision_point()`. Thread-safe under Python GIL via internal RLock. 26 deep-nesting tests added covering save → enforce → hook → save chains at 1–3 levels with full exception path coverage.

### Fixed
- **GAP026-C batched flush:** ToolCaptureBuffer caps saves, preventing excessive individual writes per session (50+ saved actions).
- **GAP015 fix:** DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION added to `_QUERY_MAP` in `auto_recall_engine.py`, fixing silent drops when behavioral triggers fire at contextual synthesis decision points.

## [1.5.09] - 2026-07-11

### Fixed
- **Hooks feedback integration:** `on_pre_llm_call` wired to both memory recall AND feedback loop evaluation. Before fix, feedback corrections were silently bypassed despite full implementation.
- **Consolidation safety guard:** `consolidate_key()` now prevents total data loss when all source retrievals fail during dedup — if no preferred target survives, all copies are preserved with a warning log instead of being deleted.
- **Critical orchestrator fixes:** Four bugs in routing logic, eviction behavior, and consistency guarantees resolved.

### Added
- MCP transport autodetect — reads `mcp_servers.mempalace.command` from config.yaml so users can override hardcoded module paths.
- Feedback loop auto-load at bootstrap with LoadSummary diagnostics for load-time visibility.
- RelevanceScorer zero-score bug fix — dict/list content no longer loses semantic query overlap.
- Lifecycle management layer (opt-in, `lifecycle.enabled: false` default) — LifecycleManager, SweepScheduler, AuditLogger with per-profile retention.

## [1.5.08] - 2026-07-10

### Added
- **Multi-Wing Routing:** Category-aware wing/room selection via `mempalace_routing` YAML config.
