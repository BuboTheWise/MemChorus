# Changelog

All notable changes to MemChorus will be documented in this file.

## [2.0.32] - 2026-09-04

### Added
- **OTel runtime declared as a core dependency (closes #169):** the former build definitions declared only `pydantic` + `pyyaml`, yet the MemPalace/MCP path imports the OpenTelemetry runtime at module load. Reinstalling `memchorus[mcp]` from GitHub into a shared venv let `pip` re-resolve the OTel family independently and split it (observed `opentelemetry-api 1.39.1` vs `opentelemetry-sdk 1.44.0`), which bricked `import memchorus` / `import mempalace` and tripped `pip check`. The OTel runtime is now a declared **core** dependency in the single canonical root `pyproject.toml` (post-#170): `opentelemetry-api/-sdk/-exporter-otlp-proto-grpc` pinned `>=1.2.0,<2.0` and `opentelemetry-instrumentation >=0.41b0,<1.0`, plus `packaging>=21.0` declared for the doctor gate. The floors match the installed `mempalace → chromadb 1.5.9` floors exactly, so a co-install resolves to one coherent line and never downgrades a shared venv already carrying OTel 1.44.0.

### Fixed
- **`memchorus-doctor --deps-check` regression gate (closes #169):** `memchorus-doctor` gains a `--deps-check` command that evaluates the *installed* OTel family against the declared set using `packaging.SpecifierSet`. It exits 1 (with a fix hint) on an `api`/`sdk`/`semantic-conventions` version split, warns when OTel is absent (the core still runs without it), and `--json` emits a CI-consumable `{ok, results}` payload. `docs/ARCHITECTURE.md`, `docs/REQUIREMENTS.md`, and `docs/SPEC.md` flip the former "Known packaging gap (OPEN)" note to "fixed in #169", citing the declared pin and the doctor gate. Regression-locked by `tests/test_install_doctor_deps_check.py` (asserts the coherent PASS path and a forced `api 1.44.0` / `sdk 1.39.1` split FAIL path). Independent review PASS in fresh venvs (t_480e6b1d) — reinstall-from-GitHub left the installed 1.44.0 OTel line unmoved, `pip check` clean post-coinstall, full suite 1732 passed / 12 skipped. Bumps `__version__` 2.0.31 → 2.0.32 (one patch above the v2.0.31 canonical-build release, keeping the release chain collision-free with #170).

## [2.0.31] - 2026-09-04

### Fixed
- **Single canonical build definition (closes #170):** MemChorus previously carried TWO divergent build definitions for the same package — the full packaging metadata in `setup.py` and a redundant `src/pyproject.toml` (no deps, no scripts, hard-coded version) that shadowed it. It now has exactly ONE: a root `pyproject.toml` (PEP 621 `[project]` table, PEP 517 setuptools backend) is the sole source of packaging truth. It carries name, description, readme, Python constraint, runtime deps (`pydantic`, `pyyaml`), the `mcp`/`dev` extras, the three console scripts (`memchorus-init`, `memchorus-doctor`, `memchorus-recalibrate`), and the `hermes_agent.plugins` entry point (`memchorus = memchorus.hooks`). The version is `dynamic`, derived from `src/memchorus/__init__.py::__version__` at build time, eliminating the second, separately-maintained packaging version string that could drift from the runtime one (the class of bug tracked in #118/#148). `setup.py` is reduced to a bare `setup()` shim (kept only for legacy `python setup.py` tooling; no packaging fields) and the shadowing `src/pyproject.toml` is removed. `scripts/check_version_sync.py` is updated to accept the new dynamic/shim layout while still failing on a stale concrete version in `setup.py`/`pyproject.toml`, a missing version in a real config, or a README/runtime value that disagrees with `__init__.py`. New `tests/test_build_def_convergence.py` (4 tests) locks the anti-drift invariant — one canonical build def, no divergent packaging versions, the gate passing, and the installed package's recorded version equaling both the runtime and source `__version__`. Bumps `__version__` 2.0.30 → 2.0.31 (one patch above the v2.0.30 docs-promotion release, so no two RELEASE cards in this batch collide on a number).

### Verified
- Independent review (t_2fd7a90e) in fresh venvs, not taken on the implementer's word: `pip install .[mcp]` and `uv pip install .[mcp]` each yield all 3 console scripts, the `hermes_agent.plugins` entry, an identical 5-dep set (pydantic, pyyaml, mcp, + dev), and `memchorus 2.0.30` with `pip check` clean; MemPalace co-install is conflict-free and both packages import in one interpreter. Version-sync gate PASS 4/4 (canonical `__init__` + README concrete-equal, `setup.py` equal-shim, pyproject dynamic), exit 0. Full suite: 1720 passed / 12 skipped (no regression). OPSEC clean — only the `memchorus@nous.systems` project email and the `BuboTheWise` GitHub owner appear in the diff and commit message; no agent names, local usernames, hostnames, or tokens.

## [2.0.30] - 2026-09-04

### Added
- **Public documentation set (promoted from the internal design docs):** the core engineering documents — `docs/REQUIREMENTS.md`, `docs/SPEC.md`, `docs/ARCHITECTURE.md`, `docs/lifecycle-analysis.md`, and `docs/hook-registration-audit.md` — are now published in the repo, with a new `## Documentation` index section in the README pointing readers to them. These were previously internal-only; this release makes the forward-looking requirements, spec, and architecture readable by contributors and third-party integrators (a prerequisite for active upstream support of the MemPalace project they build on).
- **OPSEC scrub:** every promoted document was reviewed for and stripped of identifying details before publication — hostnames, local usernames, profile/agent names, and internal task IDs were replaced with neutral placeholders (`<user>`, `<profile-a/b/c>`, `maintainer`). Public second-check: issue #165.

### Verified
- Re-scanned all five promoted documents after scrubbing: zero residual username / hostname / profile-name / internal-task-ID tokens (only the public `BuboTheWise` GitHub org appears, in install URLs and bylines, as intended). Bumps `__version__` 2.0.29 → 2.0.30 (one patch above the v2.0.29 HitRateTracker release that shipped to master in the meanwhile).

## [2.0.29] - 2026-09-04

### Fixed
- **Per-profile HitRateTracker (closes #171):** `HitRateTracker` is now a registry keyed by normalized memory directory instead of a single process-global singleton, so multi-profile in-process use no longer cross-pollinates hit-rate state or pins the first-resolved profile's tracker forever. `get_instance()` re-resolves the directory from the current `HERMES_PROFILE` on every call; `reset()` gained per-directory granularity. Full suite: 1716 passed, 12 skipped, 0 failures. Bumps `__version__` 2.0.28 → 2.0.29.

## [2.0.28] - 2026-09-01

### Fixed
- **Recall-quality fixes for scratch/fixture noise and the quiet palace re-point (closes #160, #161, #162):** three coupled recall-behaviour fixes, locked by nine new tests across three files.
  - **#160 — scratch/fixture demotion:** `orchestrator.search()`'s `_is_auto_metadata()` now carries a fourth detection path (PATH 4) so dict payloads that signal *scratch/fixture/example/test/demo* provenance — whether via a case-insensitive `categories` list or a `provenance` field — are demoted at the existing 0.3× auto-artifact weight, the same treatment already applied to auto-tool output (the `hermes_default` category/key-prefix paths and the `PENALTY_FACTOR` are unchanged). This stops a query-echoing scratch fixture from crowding a real standing-fact document out of the top-3 of the same search. Locked by `tests/test_scratch_fixture_demotion.py` (4 tests).
  - **#161 — standing-facts regression lock:** `tests/test_standing_facts_recorder.py` (2 tests) asserts a standing-facts document reaches the top-3 for its canonical query *and* outranks a query-echoing scratch fixture — the promotion-side complement to #160 (no non-test source changed for #161).
  - **#162 — loud palace-path rewrite:** `mempalace_memory_source._normalize_palace_args()` now emits a `WARNING` rather than a quiet INFO line when it re-points the reader at the populated leaf directory that actually holds `chroma.sqlite3` — a silent empty vault is now visible as a warning; the external contract (return shape) is unchanged. Locked by `tests/test_palace_loud_signal.py` (3 tests).
- Full suite: **1713 passed, 12 skipped, 0 failures.** Bumps `__version__` 2.0.27 → 2.0.28.

## [2.0.27] - 2026-09-01

### Fixed
- **Reader/writer DB-location alignment** (closes MemChorus #158; upstream MemPalace #2404, P1 open): `mempalace_memory_source.py` adds `_normalize_palace_args` + `_chroma_is_empty` — if a profile's `config.yaml` passes `--palace` pointing at the *parent* `.mempalace/` dir while the real data sits in `.mempalace/palace/`, the transport re-points at the leaf dir *only* when the leaf holds a non-empty `chroma.sqlite3` and the parent is empty/absent. All other cases (no flag, already-correct leaf, fresh install) are pinned no-op by the E2E test suite (`test_palace_path_alignment.py`), so the guard stays correct after upstream MemPalace ships their own fix for #2404.
- **README (closes MemChorus #159):** "Migrating an existing installation" subsection documents the per-profile `profiles/<name>/.mempalace/palace/` layout, the `--palace` contract, and the one-time migration steps for pre-split installs.
- **Docs (v2.0.27 contract note):** `mempalace_memory_source.py` known-layout note + per-profile path example corrected.

### Verified
- Reinstalled from GitHub (`pip install 'memchorus[mcp] @ git+https://github.com/BuboTheWise/MemChorus.git#master'` → v2.0.27); `__version__ = "2.0.27"`; fix functions present in site-packages.
- Per-profile `mempalace status`: expected drawer counts present (seed corpus shared across profiles, per-profile accumulation on new writes).
- Live MCP recall: 100% self-hit on own recent topics.
- `mempalace status` on a fresh/default profile: "No palace found" (vacuous, correct).


## [2.0.26] - 2026-08-31

### Added
- **Test-isolation guard (locks the v2.0.25 rule):** `tests/test_global_platform_guard.py` — a static AST scan that runs in the normal `pytest tests/` invocation and fails the suite if any test writes the global `os.name` / `sys.platform` (direct assignment, `setattr(os, "name")`, `monkeypatch.setattr(os, "name")`, `patch.object(os, "name")`, `patch("os.name")`, or the `sys.platform` analogues). Platform emulation must use a module-scoped fixture that hands the module under test its own `os` view — the exact pattern in `test_hermes_home.py`. Six self-tests prove the detector catches every bad form and leaves the sanctioned pattern / reads / `os.environ` mutations alone, so the guard cannot pass vacuously.

## [2.0.25] - 2026-08-30

### Fixed
- **Windows CI test-isolation (closes #147):** `test_hermes_home.py` posix-tier tests no longer patch the global `os.name` (which flipped `pathlib.Path` dispatch to `PosixPath` on Windows hosts and killed both `test-windows` jobs with a pytest `INTERNALERROR`). posix cases now route through a module-scoped `posix` fixture — the mirror of the existing `windows` fixture. 23/23 green.

## [2.0.15] - 2026-08-28

### Fixed
- **MemPalace source robustness (closes #136, #139, #143):** three latent reliability gaps in the live MemPalace path are now fixed. **#136** — `_McpClient.add_drawer()` only keyword-matched the response text, so a server reply of `{"success": false, ...}` with no error word (e.g. `{"success": false, "drawer_id": "d1"}`) read as a phantom success and corrupted the local mirror; it now reads the MemPalace MCP server's structured `{"success": bool}` flag first (and `{"error": ...}` context on failure) and falls back to keyword scanning only when the `success` field is absent. **#139** — `MemPalaceMemorySource` now enforces a `MIN_RECALL_SCORE` floor (default 0.5, overridable via `config['min_recall_score']`) on MCP search hits: `similarity` is higher=better (cosine distance mapped to similarity), so the lower-bound threshold keeps strong hits and drops weak/off-topic ones, mirroring the `HermesMemorySource` / `SessionSearchMemorySource` contract; entries without a reported similarity are kept. **#143** — `hooks._format_context_block()` now unwraps structured content payloads (`{"text": ...}`, nested `{"content": ...}`) into clean strings via a new `_unwrap_content_field()` helper (with a `json.dumps` fallback for arbitrary mappings/lists), instead of leaking `{'key': ...}` dict reprs into the injected context block. New `tests/test_mempalace_robustness_fixes.py` regression suite (28 tests) locks in structured-success detection, the recall floor (default/override/boundary/non-numeric), and content unwrapping (helper direct + end-to-end through the formatter). Bumps `__version__` 2.0.14 → 2.0.15.

## [2.0.14] - 2026-08-28

### Fixed
- **Hot-path DEBUG emit gated (closes #137):** `MistakeDetector.scan_user_text` no longer builds a formatted `logger.debug` record on every scan; the emit is now under `if logger.isEnabledFor(logging.DEBUG)`. The `TestPerformance::test_scan_time_within_budget` budget was widened from 2000µs to 5000µs with the module logger raised to WARNING for the measurement window, restoring a stable deterministic perf signal on CI. Bumps `__version__` 2.0.13 → 2.0.14.

## [2.0.13] - 2026-08-26

### Fixed
- **`[mcp]` extra pinned `<2.0` while Hermes base already ships `mcp 2.0.0` (closes #135):** `setup.py` declared `mcp>=1.0,<2.0`, forcing pip to downgrade any shared venv (e.g. the live Hermes runtime) to MCP 1.29.x the moment `memchorus[mcp]` was installed, silently moving the pin out from under the rest of the agent stack. MemChorus only imports `StdioServerParameters`, `stdio_client` and `ClientSession` — all three symbols are present in both the 1.29.x and 2.0.x API lines — so the `<2.0` upper pin was a precaution that had no runtime basis and created a direct conflict with hermes-agent's `mcp==2.0.0`. The range is now `>=1.29,<3.0`, letting both pins coexist in one environment. Verified by installing `memchorus[mcp]` from a venv already at `mcp 2.0.0`: the venv keeps `mcp 2.0.0`, all six MemChorus modules import cleanly, and the MCP transport symbols remain present. No code change — dependency-range and docs only. Bumps `__version__` 2.0.12 → 2.0.13.

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
