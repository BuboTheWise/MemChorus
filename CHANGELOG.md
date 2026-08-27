# Changelog

All notable changes to MemChorus will be documented in this file.

## [2.0.12] - 2026-08-26

### Fixed
- **`EvictionEngine.structural_cleanup` reported N cleanups while deleting nothing (closes #126):** `lifecycle_eviction.py` accepted a `purge_fn` callback but never invoked it, and the loop `continue`d on falsy drawer keys — the exact keys that represent drained, purge-eligible drawers — so the returned count accumulated against drawers that were actually left untouched. Empty (falsy) drawer keys are now purged through `purge_fn(source, drawer_key)`; the return count reflects only successful (truthy) purges; every attempt is audit-logged with a `purged=True/False` field. `purge_fn=None` degrades gracefully (attempt logged, 0 counted). Both `purge_fn` and `audit_log` are now `Optional[...] = None` so existing call sites that pass required values are unaffected. Added `TestEvictionStructuralCleanup` regression suite covering purge-fn invocation per empty key with count parity, non-empty drawers left un-purged and uncounted, `purge_fn=None` returning 0 without raising, and failed purges not being counted. Bumps `__version__` 2.0.11 → 2.0.12.

## [2.0.11] - 2026-08-26

### Fixed
- **Orientation cache invalidation reached only one branch (closes #125):** in `orientation.py`, the per-key project identity for the `_CacheRegistry` key was computed separately in the cache-read path and the cache-write path, so a key written under the resolved project could be looked up under the raw caller value and vice-versa. `clear_orientation_cache()` / `clear_project()` therefore silently left entries on the other branch of the key, keeping stale results serving after a cache clear. The project value is now resolved once and used identically for both the lookup and the insert. Added `TestClearProjectBothKeyShapes` regression suite proving a clear issued against either key shape purges entries registered under the other, and that a subsequent search re-runs (cache miss) rather than returning the cleared entry. Bumps `__version__` 2.0.10 → 2.0.11.

## [2.0.10] - 2026-08-25

### Fixed
- **Stale README test-suite counts (closes #128):** the Testing section claimed '90+ test modules with 1378 collected tests'. Corrected to the measured live collection: `101 test files with 1,585 collected tests` (verified against `pytest --co` on this branch). Docs-only; no code or behavior change. Bumps `__version__` 2.0.09 → 2.0.10 (the task's planned 2.0.06 had already been consumed by the UTC-timestamps fix).

## [2.0.09] - 2026-08-25

### Fixed
- **Relevance recency crash on naive ISO-8601 timestamps (closes #123):** `RelevanceScorer._score_recency` in `relevance_engine.py` compared offset-naive parsed datetimes against a timezone-aware "now", raising `TypeError` (and corrupting recall scores) whenever a memory timestamp carried no UTC offset. Naive parsed values are now normalised to UTC before the delta computation, matching the established pattern in `lifecycle_retention.py`. Added `TestScoreRecencyNaiveIsoTimestamps` regression suite: naive timestamps produce bounded float scores, naive and aware scoring agree to 6 decimal places, and `None`/unparseable input degrades to the neutral 0.5 floor.

## [2.0.08] - 2026-08-25

### Fixed
- **guard-001 regex alternation typo (closes #130):** the `tool_call_check` pattern in the `guard-001-no-editable-install` prohibition rule had a second regex alternative of `\-\\s*e` (escaped dash followed by a literal backslash before `\\s`). Because the alternative is anchored after `\\s+`, it could never match a real invocation — the first alternative `-e` was the only one that actually matched. Normalised to `-\\s*e`. Added `test_guard001_tool_call_check_matches_editable_install_spacings` to lock in `.search()` success on both `pip install -e ./hermes-agent` and `pip install  -e  ./hermes-agent`. No rule id/severity/condition/block_action/rationale changes.

## [2.0.07] - 2026-08-25

### Fixed
- **MergeEngine docstring defaults (closes #127):** the `lifecycle_merge.MergeEngine` class docstring documented `similarity_min 0.3` and `duplicate_cluster_max 5`, but the constructor actually uses `0.75` and `3`. The docstring was corrected to match the code; no behavior change.

## [2.0.06] - 2026-08-24

### Fixed
- **Producer UTC-aware timestamps (closes #124):** the `proactive_check` / `proactive_save` stamping sites in `hermes_memory_source.py` and `session_search_memory_source.py` used naive `datetime.datetime.now()` (no timezone), producing ambiguous ISO timestamps whose ordering and meaning were machine-local. All 7 sites now emit `datetime.now(tz=datetime.timezone.utc)`, so proactive timestamps carry explicit `+00:00` offsets and sort correctly across timezones.

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
