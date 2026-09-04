LIFECYCLE ANALYSIS FINDINGS — 2026-07-24
======================================================================
Author: executor agent (executor) | Task: internal analysis task
Source: ~/.hermes/workspace/Code/MemChorus (analysis snapshot)

GAP017.5 — LifecycleConfigResolver._resolve_lifecycle_config()
----------------------------------------------------------------------
Location: src/memchorus/lifecycle_manager.py lines 93-108.

The resolver merges raw config into DEFAULT_LIFECYCLE_CONFIG via raw_overrides(
default, raw), which delegates to _merge_recursive for nested dict updates. Returns:
resolved lifecycle dict. If raw is None or not a dict, falls back to defaults.

Key findings:
- Config precedence is properly enforced (raw overrides everything in DEFAULTS).
- Comment at line 96 about 'user-provided nesting' references PR #48 (commit
  879a93e - docs/lifecycle-config-nesting) — doc-only PR, no functional impact.
- _merge_recursive at lines 54-59: iterates raw keys; if a key exists in default
  and both values are dicts, recurses. Otherwise raw wins. Classic deep merge.

GAP017.6 — create_merge_engine()
----------------------------------------------------------------------
Location: src/memchorus/lifecycle_merge.py lines 25-46.

create_merge_engine checks the resolved lifecycle config for merge_at_write with
an 'enabled' boolean. Returns None when disabled (default), otherwise instantiates
MergeEngine(orchestrator, override_days=eviction.retention_override_days). No
actual merge operations performed — just engine instantiation/gate.
Returns: Optional[MergeEngine].

GAP017.7 — AuditLogger._do_rotate()
----------------------------------------------------------------------
Location: src/memchorus/lifecycle_manager.py lines 631-645.

Reads the JSONL audit log, drops oldest entries when exceeding max_entries (default
10,000). Keeps most recent max_entries + 1 buffer for the incoming line. On file
not found or rotation write failure → silent via try/except. No data loss occurs —
file is never truncated before new content is written.

GAP017.8 — Hook entry point registration namespace
----------------------------------------------------------------------
Location: src/setup.py lines 46-50.

Entry point group is "hermes_agent.plugins" with the memchorus = memchorus.hooks
entry point. The docstring at line 6 says discovered via setup.cfg entry_points, but
the actual registration is in setup.py (not setup.cfg). Namespace mismatch: hooks.py
line 66 references hermes.plugins.lifecycle which doesn't match reality.

GAP017.9 — pre-save merge engine flow in orchestrator.save()
----------------------------------------------------------------------
Location: src/memchorus/orchestrator.py lines 602-605, 634-639.

Two invocation sites for _merge_engine within the save pipeline:
1) Lines 602-605 — explicit source_name path invokes merge_result = self._merge_
   engine.pre_save_check(key, value, source_name). If should_proceed is False,
   merges the final_value into the new write. Otherwise proceeds normally. Never
   blocks the save.
2) Lines 634-639 — profile-based routing path calls the same pre_save_check with
   key, value, and effective_profile. Same merge_or_override behavior on the content.

Pattern applies consistently without divergence between explicit source vs. profile
targeting paths. The merge result either modifies the content before writing or
leaves it unchanged.

GAP017.10 — Memory source error propagation behavior
----------------------------------------------------------------------
Location: src/memchorus/hermes_memory_source.py (lines 151, 176, 199, 224, 300,
    320, 334, 365, 379, 483, 532, 535)
         src/memchorus/mempalace_memory_source.py (lines 61, 71, 143, 167, 233,
           237, 248, 252, 423, 432, 464, 467, 471, 475, 483, 493, 532, 555,
           592, 609, 683, 689, 721, 735, 774, 779, 832, 850, 880, 887, 1011,
           1021, 1112, 1130, 1140)

Both sources use except Exception: broad handlers that catch everything and return
a safe fallback value. HermesDefault saves/retrieves/searches all degrade to False,
None, or [] on any failure. MemPalace wraps MCP calls in the same pattern — no
exceptions ever propagate outward from these methods. Orchestrator.save() relies on
a per-source _try_save_to(True/False) result; if one source fails silently, it falls
through to the next without raising.

GAP017.11 — AuditLogger rotation safety analysis
----------------------------------------------------------------------
Location: src/memchorus/lifecycle_manager.py lines 183-193 (log method), lines
631-645 (_do_rotate function).

The log() method checks _enabled first, acquires a threading.Lock, then calls
_do_rotate. The rotate logic reads all lines, truncates to maintain max buffer size,
then rewrites the file with only recent entries. FileNotFoundError in open() for
reading is caught — rotation becomes a no-op on missing or unreadable files. Write
failures during rotation are also silenced via exception handlers. No risk of partial
truncation because we always read all lines into memory before attempting to write.

GAP017.12 — Lifecycle engine lazy initialization timing
----------------------------------------------------------------------
Location: src/memchorus/lifecycle_manager.py lines 264-296.

Engines are lazily instantiated on first call rather than at constructor time to
avoid import cycles during init. self._retention_engine and self._eviction_engine
start as None, then each gets constructed from config when its corresponding method
runs. This means retention_days/eviction configuration isn't validated until the
first actual request, rather than upfront at construction. If someone accesses these
methods before sweep or merge operations trigger them, they could silently miss
configuration errors present at startup time.

VERBOSITY LEVEL (lines 62-84): The resolver itself is silent — no logging during
merge operations except when the parent LifecycleManager calls _resolve_lifecycle_config(
raw_lifecycle) and passes the result along the chain. No DEBUG/INFO logs are emitted
during resolution, so misconfigurations won't surface in logs at that stage either.

AUTO-BOOTSTRAP FIVE-STEP SEQUENCE:
----------------------------------------------------------------------
Location: src/memchorus/auto_bootstrap.py lines 430-517 (bootstrap_orchestrator)
and src/memchorus/hooks.py lines 2468-4696 (register).

Step 1: Read MEMCHORUS_AUTO_ENABLED environment variable. True by default, set to
false to disable all hooks globally and prevent bootstrap side-effects from firing.

Step 2: Parse ~/.hermes/memchorus.yaml or ~/.memchorus.yaml as a fallback YAML configuration file.
Merges into the base defaults dict with env var overrides taking precedence. Raises
warnings on parse failures but does not fail fatally.

Step 3: Merge resolved config into MemoryOrchestrator constructor via the orchestrator
config parameter. Sets half_life_days cache_max_size, and source priority_order from
the resolved values when present in the final effective configuration dictionary.
Applies DEFAULT_LIFECYCLE_CONFIG merging logic during _resolve_lifecycle_config() to
handle nested sub-configs correctly at this step as well before passing through.

Step 4: Probe MemPalace source availability — attempt to instantiate MemPalaceMemorySource
with MCP transport detection. If MCP unavailable, orchestrator continues with
hermes_default only (AC-1 graceful degradation). _instance_holder[0] is set for GC safety.

Step 5: Register behavioral lifecycle hooks via PluginContext when entry point triggers.
Pre_llm_call injects memory recall + feedback loop corrections into the prompt. Post_tool_call
auto-captures significant outcomes. On_session_start performs auto-orientation context search.
All three hook methods return None on any exception — zero leak policy for error handling.

CONFIGURATION PRECEDENCE CHAIN:
----------------------------------------------------------------------
1. Environment variables (MEMCHORUS_DEFAULT_SOURCE, MEMCHORUS_CACHE_SIZE)
2. YAML config file (~/.hermes/memchorus.yaml or ~/.memchorus.yaml)
3. _DEFAULTS hardcoded in auto_bootstrap.py lines 60-115

Resolving happens at bootstrap time in a single pass through _resolve_config() which
merges layers top-down with env vars winning over YAML winning over defaults. No later
re-read of the disk file after bootstrap completes — config is frozen at instantiation.

ERROR HANDLING VERIFICATION:
----------------------------------------------------------------------
Hooks (hooks.py lines 1802-1947): All three hook methods wrapped in try/except Exception:
at lines 180-183, 295-296, 339-340. Inner exception handlers catch feedback loop failures,
storage engine failures, and orientation module import errors with graceful degradation to None.

Orchestrator (orchestrator.py save method): _try_save_to catches Exception per source
at line 441-442. Merge engine checks at lines 603-605 and 635-639 never raise — they only
modify value in-place when should_proceed is False. Enforcement lock prevents recursion
during both read (pre-decision recall) and write (post-action storage) enforcement paths.

LIFECYCLE ENGINE CHAIN:
----------------------------------------------------------------------
orchestrator._initialize_lifecycle() calls create_merge_engine(self, resolved) at line 246
of orchestrator.py. If merge_at_write.enabled is True in the resolved config, a MergeEngine
instance is created and stored as self._merge_engine. Otherwise _merge_engine remains None
and checks at lines 602/634 short-circuit via the `if self._merge_engine is not None` guard.

SweepScheduler wired manually during bootstrap:
1. LifecycleManager instantiated with resolved config (orchestrator.py line 223-226)
2. If LM.is_enabled True, SweepScheduler created and started (lines 228-239)
3. Scheduler runs sweep_cycle() every interval_secs in a background thread

ENTRY POINT DISCOVERY:
----------------------------------------------------------------------
Hermes Gateway scans entry_points groups for plugins. It reads:
1. setup.py hermes_agent.plugins group → memchorus = memchorus.hooks
2. Imports module, calls register(ctx) function with PluginContext instance
3. register() loads plugin.yaml (line 446-453 in hooks.py), triggers lazy bootstrap
   via _trigger_lazy_bootstrap(), instantiates MemChorusHooks, registers the three lifecycle hooks
4. Version is logged via __import__('memchorus').__version__ at line 470

HOOK CHAR LIMIT OVERRIDE:
----------------------------------------------------------------------
hooks._resolve_char_limit() reads HERMES_PROFILE environment variable defaults to 'default',
loads config.yaml from ~/.hermes/profiles/<profile>/config.yaml memchorus.hook_char_limit if set,
clamps to min=200 max=10000. Falls back to _MAX_BLOCK_CHARS (800 default). This allows per-
profile override of the hard ceiling without changing global defaults.

CRITICAL ISSUE LOG:
----------------------------------------------------------------------
[ISSUE-001] Docstring drift at hooks.py line 6 says 'setup.cfg entry_points' but actual
registration is in setup.py lines 46-50 under 'hermes_agent.plugins'. Minor documentation issue
but not functional — code runs correctly via the setup.py path. Not worth fixing right now.

[ISSUE-002] Hook registration uses ctx.register_hook("pre_llm_call", ...) naming convention at
hooks.py lines 4176-4178. These strings MUST match what Hermes Gateway expects in its internal hook registry.
If the gateway renames these hooks, they silently do nothing since register_hook swallows unknown names
(assumes gateway handles validation). No way to audit this without checking gateway source directly.

[ISSUE-003] _resolve_char_limit caching via module-level variable HERMES_MEMCHORUS_CHAR_LIMIT at line 350
of hooks.py: once set, this value persists for the entire process lifetime. Changing config.yaml after
bootstrap won't have an effect unless the module is reloaded. Edge case but worth noting.

END OF ANALYSIS | executor agent | 2026-07-24

