# Changelog

All notable changes to MemChorus will be documented in this file.

## [2.0.05] - 2026-08-24

### Fixed
- **Auto-init plugin enablement toggle:** `memchorus-init --enable-plugin` used `action=store_true` with `default=True`, so the CLI branch was never actually disabled. Replaced with a `--disable-plugin` toggle (off by default) so plugin enablement is genuinely toggleable. Closes #129.
- **Stale entry-point group reference:** a comment in `hooks.py` cited the plugin discovery group as `hermes.plugins.lifecycle`; corrected to the real group `hermes_agent.plugins` (as declared in `setup.py`). Closes #133.
- **README CLI docs:** updated the `memchorus-init` example and option table to document the renamed `--disable-plugin` flag.

## [Unreleased — 2.0.1-pre] - 2026-08-21

### Fixed
- **McpTransportDetector Shape B config parsing:** Hermes 3.x config files split MCP server definitions into separate `command` and `args` keys instead of a single shell-style command string. The transport detector previously ignored the `args:` key, causing MemPalace subprocesses to launch without `-m mempalace.mcp_server` arguments (silently falling back to local cache). Parser now correctly detects both legacy Shape A (single-string) and Hermes-native Shape B (split dict) formats. Backwards compatible — no breaking changes.
- **Test suite count corrected:** README references updated from 1388 to 1378 to match current live collection output (minor reduction due to environment-sensitivity shifts in two conditional MCP integration tests).

### Added
- **`_McpTransportDetector` unit test suite** (7 tests): Dual-shape config validation, missing args fallback, dead-path handling, and cache behavior coverage.

## [2.0.0] - 2026-08-19

### Fixed
- **OPSEC sanitization:** Complete sweep of public-facing repo — scrubbed internal agent names from examples, sanitized PKG-INFO build artifact, expanded .gitignore to exclude internal Hermes/Kanban workflow directories and scratch artifacts. Removed hardcoded local paths throughout README CalibrationEngine section. Test fixture `[executor]` replaced with `test_executor` to avoid PyYAML quoting collision.
- **Category validation:** Strict enforcement of known enum categories (`LEARNING`, `MISTAKE`, `DECISION`, `RESULT`). Stale test fixtures using deprecated `AUTO` string cleaned to match runtime guards. Tests that relied on `AUTO` as a real category tag updated.
- **Integration hook tests skip in CI:** `_hermes_plugins_list()` subprocess call now gracefully caught when the Hermes CLI is unavailable (GitHub Actions runners), preventing hard failures and allowing clean skips instead.
- **Duplicate @staticmethod decorator:** Removed redundant second `@staticmethod` on `_try_save_to` in orchestrator, fixing silent warning under Python 3.12+.

### Added
- **Install Doctor CLI diagnostic:** New `memchorus-doctor` command (`python -m memchorus.install_doctor`) runs 8 health checks: Python version (>=3.11), dependency integrity (pydantic/PyYAML), memory source registration, plugin hook state, config validation, auto-tune pipeline components (HitRateTracker/MistakeDetector/CalibrationEngine/AdaptiveThreshold), data directory readability/writability, and test suite discoverability. Returns exit code 0 on healthy install, non-zero on failures. Includes 25 unit tests.
- **GAP008 LRU cache eviction tests:** Unearthed and committed pre-existing test coverage verifying `OrderedDict` based LRU eviction in `_retrieve_cache`.
- **Test suite growth:** Test count increased from ~798 to 1378 across 90+ test modules, covering the full pipeline including parallel execution resilience.

### Removed
- **Stale feedback_loop references:** README post-audit section cleaned — removed reference to `feedback_loop/integration.py` which was already deleted in v1.9.0.

## [1.9.0] - 2026-08-14

### Added
- **Recall-time relevance boosting via CalibrationEngine:** `boost_factor_for_key()` computes per-key boosts from HitRateTracker history (frequency bonus + signal quality ratio). `RelevanceScorer.weighted_score()` applies multiplicative boost to recall results, producing 3x scores for high-value keys vs. baseline. New `MIN_OBSERVATIONS=3` sentinel prevents boosting on insufficient data.
- **Workflow compliance feedback loop verification:** Added verification tests ensuring behavioral enforcement hooks fire correctly across decision points and lifecycle events.

### Changed
- **OPSEC hardening:** Removed all hardcoded personal paths, PII, and sensitive identifiers from source code and documentation. Paths normalized to `~/.hermes/` or `$HOME` equivalents throughout. License header compliance fixed.
- **Storage resilience for ChromaDB compactor failures:** Vector store operations now tolerate ChromaDB internal state errors (compaction failures, checkpoint corruption) with graceful fallback instead of cascading crashes.

### Fixed
- **CI xdist singleton isolation:** Replaced fragile `sys.modules` mocking in `test_full_pipeline_integration` with direct singleton `_index` population wrapped in try/finally — eliminates cross-test pollution under parallel execution, restores all 16 tests green.

### Removed
- **feedback_loop module removed:** Legacy feedback loop code deprecated and fully extracted. Workflows simplified while preserving existing functionality through enforcement hooks.

## [1.8.0] - 2026-08-13

### Added
- **Auto-tuning framework (HitRateTracker + MistakeDetector + AdaptiveThreshold + CalibrationEngine):** Full pipeline from recall outcome data → hit-rate tracking → mistake detection → adaptive threshold adjustment → YAML write-back. Includes `graceful_degradation` for unregistered profiles returning default confidence thresholds.
- **Memory benchmark module** with quantifiable metrics for recall accuracy and precision measurements.

### Changed
- **MemPalace subprocess optimization:** stderr noise suppression via filter wrapper, reducing console clutter during background operations.

## [1.7.0] - 2026-08-12

### Added
- **Testing documentation:** New `docs/TESTING.md` covering the full multi-layer test strategy — unit, integration, E2E MCP tests, benchmark metrics methodology and failure-mode testing approach (self-contained for third-party agents).
- **Improvement cycle documentation:** New `docs/IMPROVEMENT-CYCLE.md` describing the data-driven feedback loop from benchmark measurement through Kanban task-based fixes to post-merge verification and evolution reporting.
- **README testing section upgrade:** Added current test count (1260+ across 75 modules), benchmark infrastructure references, iterative improvement cycle cross-reference and MemPalace attribution link.

### Changed
- **Version bump to v1.7.0** (minor — new documentation capabilities reflecting the functional milestone reached).
- **MemPalace attribution clarified:** README now prominently links to [MemPalace GitHub](https://github.com/MemPalace/mempalace) as primary enhancement backend with explicit graceful-degradation explanation.

## [1.6.0] - 2026-08-06

### Changed
**Consolidation release.** Source version aligned with `__init__.py` = 2.0.0.
- **Documentation alignment:** Restored optional dependencies table and Pydantic/MCP version compatibility notes lost during merge conflict resolution. README version now matches `__init__.py` (2.0.0).

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
