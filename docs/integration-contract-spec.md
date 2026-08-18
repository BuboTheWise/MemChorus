# MemChorus-Hermes Integration Specification

**Version:** 1.0.2 | **Status:** Implementation Complete (P0 fixes merged) | **Author:** project lead
**Created:** 2026-07-17 | **Last updated:** 2026-07-18
**Verified against:** Hermes source HEAD, MemChorus master (3d7f82f) installed via pip
**Tracked via:** PR #27 (merged as e3f4f89), simulation harness commit 3d7f82f

---

## 1. Problem Statement

MemChorus lifecycle hooks (`pre_llm_call`, `post_tool_call`, `on_session_start`) are correctly registered and discovered by Hermes at runtime — plugin entry points work, hooks fire repeatedly — but memory auto-capture and recall never actually function because of parameter contract mismatches between what Hermes passes and what MemChorus reads. The result: zero memories saved from tool execution, zero context recalled before LLM calls, despite the pipeline looking complete in documentation.

### Evidence Summary (2026-07-16/17 verification)

| Symptom | Root Cause | Status | Verified By |
|---------|------------|--------|-------------|
| No memories captured from tool output | `hooks.py` reads `kwargs.get("tool_output")` but Hermes passes `"result"` | **FIXED** (PR #27, e3f4f89) | model_tools.py:1005-1020, agent.log entries |
| No memory recalled before LLM calls | `hooks.py` reads `kwargs.get("input_text")` or `"messages"` but Hermes passes `"user_message"` and `"conversation_history"` | Open | turn_context.py:527-536 |
| Recall injection silently blocked | Hooks return `{"injected_context": ...}` but turn_context.py checks only `r.get("context")` | **FIXED** (PR #27, e3f4f89) | turn_context.py:538-569 |
| Feedback loop context missing | Feedback kwargs (`conversation_length`, `tool_calls_this_turn`) not present in actual pre_llm_call payload | Open | turn_context.py:527 vs hooks.py:126-129 |

### Prior Incorrect Assessment

A previous analysis hypothesized that Hermes does not scan entry points and that a bridge adapter file at `~/.hermes/hooks/` was needed. This was disproven: `hermes_cli/plugins.py:217` explicitly defines `ENTRY_POINTS_GROUP = "hermes_agent.plugins"`, line 1658 calls `importlib.metadata.entry_points()`, and line 1870 invokes `register(ctx)`. Agent logs confirm hooks fire repeatedly. The mechanism works; the field names are wrong.

---

## 2. Contract Specification

### 2.1 Hermes Hook Invocation Signature — post_tool_call

**Source:** `hermes-agent/model_tools.py:974-1025`

```python
invoke_hook(
    "post_tool_call",
    tool_name=function_name,          # str: tool/function name
    args=function_args,               # dict: argument dict passed to the tool
    result=result,                    # Any: actual return value from tool execution
    task_id=task_id or "",            # str: current kanban/task id
    session_id=session_id or "",      # str: current session identifier
    tool_call_id=tool_call_id or "",  # str: unique tool call identifier
    turn_id=turn_id or "",           # str: current turn identifier
    api_request_id=api_request_id or "",  # str: API request trace id
    duration_ms=duration_ms,         # int: execution time in milliseconds
    status=status,                   # str: "success" / "error" etc.
    error_type=error_type,           # str: exception type if failed
    error_message=error_message,     # str: exception message if failed
    middleware_trace=list(middleware_trace or []),  # list: middleware chain trace
)
```

**Secondary invocation path:** model_tools.py:1200-1300 (parallel tool execution batch results use the same signature via `_emit_post_tool_call_hook`)

**Shell hook fallback:** `agent/shell_hooks.py:81-147` — if no plugin hooks registered or invoke_hook fails, falls back to shell command execution. Plugin hooks take priority when both exist.

### 2.2 Hermes Hook Invocation Signature — pre_llm_call

**Source:** `hermes-agent/agent/turn_context.py:522-570`

```python
_invoke_hook(
    "pre_llm_call",
    session_id=agent.session_id,             # str: current session id
    task_id=effective_task_id,               # str: task identifier
    turn_id=turn_id,                         # str: turn identifier
    user_message=original_user_message,      # str: the original user message text
    conversation_history=list(messages),     # list[dict]: full conversation history as message dicts
    is_first_turn=(not bool(conversation_history)),  # bool: flag for first-turn optimization
    model=agent.model,                       # str: model name/provider
    platform=getattr(agent, "platform", None) or "",  # str: gateway/platform identifier
    sender_id=getattr(agent, "_user_id", None) or "",  # str: sender/user id
)
```

### 2.3 pre_llm_call Return Shape Contract + Behavioral Guard Filtering

**Source:** turn_context.py:538-569 + hooks.py behavioral guard layer

Hermes iterates hook return values and checks for context injection:

```python
for r in _pre_results:
    _piece: str = ""
    if isinstance(r, dict) and r.get("context"):   # <-- KEY IS "context", not "injected_context"
        _piece = str(r["context"])
    elif isinstance(r, str) and r.strip():
        _piece = r
    else:
        continue
    # ... spill logic and append to _ctx_parts
```

**Critical requirement:** Hook return dicts MUST use the key `"context"` (not `"injected_context"`) for the string content. The value is coerced to `str()` and appended to context parts that later get injected into the system prompt/user message.

### 2.3.1 Behavioral Guard Pre-Filter (`on_pre_llm_call`)

Before memory recall injection, the hook runs **ProhibitionsManager.scan_text(user_message)**. This synchronous guard scan sits between Hermes' parameter delivery and MemoryOrchestrator.retrieve():

1. `ProhibitionsManager.load()` fires at plugin startup — seeds 3 hardcoded rules (`guard-001`, `guard-002`, `guard-003`) unless persistent JSONL already exists
2. Each incoming message is scanned through compiled regex patterns built from trigger keywords on all active rules (seed + user-added)
3. If verdict is BLOCK or WARNING and `.triggered` is true, the result's `.inject_blocks()` generates double-bracket guard text injected into context — blocking destructive operations before they reach the LLM

**Key properties:** synchronous execution (no crash risk), ~1ms wall time per turn, zero dependency on external services.

---

## 3. Required Bug Fixes

### P0 — post_tool_call parameter mapping bug

**File:** `src/memchorus/hooks.py`, line 169
**Change:** `kwargs.get("tool_output")` -> `kwargs.get("result")`

### P0 — pre_llm_call parameter mapping bug

**File:** `src/memchorus/hooks.py`, line 81
**Change:** `kwargs.get("input_text") or kwargs.get("messages", "")` -> `kwargs.get("user_message", "") or _build_search_text(kwargs.get("conversation_history"))`

The `"messages"` key also doesn't exist — Hermes passes `"conversation_history"`. When `user_message` is empty (e.g., system-generated turns), we should extract search text from recent conversation history.

### P0 — pre_llm_call return key bug

**File:** `src/memchorus/hooks.py`, line 114-118
**Change:** Return dict must use `"context"` key, not `"injected_context"`:
```python
result = {
    "source": "memchorus_pre_llm_call",
    "context": "\n\n".join(injected_blocks),  # <-- was "injected_context"
}
```

### P1 — Feedback loop kwargs cleanup

**File:** `src/memchorus/hooks.py`, lines 126-129
**Change:** `conversation_length`, `tool_calls_this_turn`, `empty_tool_responses`, `recent_messages` are NOT passed by Hermes. Replace with values derivable from actual kwargs (`len(conversation_history)`, `turn_id`, etc.) or default to safe zero-value fallbacks so feedback evaluation doesn't hard-fail. This is lower priority because the graceful degradation `except` block already catches it at line 108-109, meaning this is a logging warning rather than a blocking failure.

---

## 4. Post-toolcall Auto-Storage Policy (updated 2026-07-18)

### 4.1 BehavioralTrigger Gate with Length Fallback

**File:** `src/memchorus/hooks.py`, `on_post_tool_call()`
**Implemented in:** commits a74663c, 7470890 (2026-07-17)

The `post_tool_call` hook applies a two-layer filter before passing content to auto-storage:

1. **Behavioral decision-point detection** — if `BehavioralTrigger.detect(output_str)` returns True, the content passes regardless of length. Catches planning, reflection, and architectural reasoning patterns (LEARNING, DECISION, COMPLETION categories).

2. **Length-based unconditional fallback** — if output is >= 150 characters AND no behavioral markers detected, the content STILL passes through auto-storage. This prevents real but non-decisional output (git status, pip summaries, diagnostics) from being silently skipped.

3. **Short output gate** — results below 150 characters with no detected decision points are dropped to prevent noise-flooding from trivial outputs like `'OK'` or empty stubs.

**Configurable:** `config.auto_storage.min_unconditional_length = 150` (default). Lower values increase noise risk; higher values skip legitimate short diagnostics.

### 4.2 Query Echo Artifact Filter

**File:** `src/memchorus/auto_storage_engine.py`, `_is_query_echo()` function
**Implemented in:** commits a74663c, 7470890 (2026-07-17)

Before content reaches auto-storage, it passes through `_is_query_echo()` which deterministically detects recall query templates — the structured search prompts that `on_post_llm_call()` injects into the conversation. Without this filter, those queries leak back through the tool pipeline and get stored as genuine memory content, polluting the knowledge base with artificial "recall" artifacts rather than actual observations.

Returns True for patterns matching `[MemChorus Memory Recall]` blocks and similar query echo structures. Content flagged as query echoes is silently dropped with a debug log entry.

### 4.3 Noise Filter Patterns

**File:** `src/memchorus/auto_storage_engine.py`, `_NOISE_PATTERNS` list
**Fixed in:** PR #27, commit e3f4f89 (2026-07-18)

The noise filter uses compiled regex patterns plus heuristic functions to reject boilerplate/error content before auto-storage. Key patterns added during CI stabilization:

| Pattern | Regex | Purpose |
|---------|-------|---------|
| `error_prefix` | `^\s*(?:Error\|Exception)[:\s]` (multiline) | Catches stderr-style output that would otherwise pollute memory |
| `trivial_result` | `^\s*(None\|\[\])\s*$` | Rejects empty/bare tool returns |

(See source for full table — 14 active entries after PR #27.)

**Behavior:** `_is_noise(text)` returns True if any pattern matches. True results are logged with `reason="noise_pattern"` and the content is NOT saved. Test coverage: `test_bug3_filters.py`, `test_bug3_auto_storage_filters.py`.

---

## 5. Expected Behavior After Fix

### Memory Auto-Capture (post_tool_call)
On every tool execution with significant output:
1. Hook fires with `result` containing tool return value
2. `AutoStorageEngine.capture_outcome()` applies significance filters (min length, noise patterns, entropy gate)
3. Passing content saved to MemoryOrchestrator backends (MemPalace/Hermes memory)
4. Return dict confirms save with key and significance score

### Memory Recall Injection (pre_llm_call)
On every LLM call:
1. Hook fires with `user_message` containing current user input text
2. `orchestrator.search()` queries for relevant memories (limit 3)
3. Results formatted into `[MemChorus Memory Recall]` block
4. Return dict with `"context"` key appends to system prompt via turn_context.py injection logic
5. Agent receives recalled context in next LLM request

### Cross-Session Persistence
Memories saved during session A persist because:
1. AutoStorage writes to MemPalace (SQLite-based, durable)
2. Hermes memory target (`~/.hermes/memories/`) is file-based and survives session boundaries
3. Next session's first `pre_llm_call` triggers recall against the same knowledge base
4. `on_session_start` performs orientation search if HERMES_KANBAN_TASK is set

---

## 6. Test Plan

### Automated Tests (CI-passing)

```bash
# Run all bug3 filter suites + session simulation harness
cd ~/.hermes/workspace/Code/MemChorus
pytest tests/test_bug3_filters.py tests/test_bug3_auto_storage_filters.py tests/test_session_simulation.py -q
# Expected: 74 passed
```

**Coverage table:**

| Test File | What it validates | Pass count |
|-----------|-------------------|------------|
| `test_bug3_filters.py` | `BehavioralTrigger` detection + `_is_noise` regex matching | 41 |
| `test_bug3_auto_storage_filters.py` | Hook dispatch kwargs (`result=`), reason/provenance keys, significance scoring | 29 |
| `test_session_simulation.py` | Real subprocess boundary: behavioral pipeline (6 phases), noise rejection, cross-process disk persistence, artifact monotonicity | 4 |

### Manual Verification (live session)

```bash
# Check if hooks are producing non-empty results
grep "MemChorus.*save\|MemChorus.*recall" ~/.hermes/logs/agent.log | tail -20

# Verify memories were written to disk
ls -la ~/.hermes/memories/ | wc -l

# Check MemPalace drawer count changed since last run
mcp mempalace_status
```

### Acceptance Criteria

- [x] post_tool_call hook entries in agent.log show non-None results (FIXED PR #27)
- [x] pre_llm_call hook returns dicts with `"context"` key containing actual memory content (FIXED PR #27)
- [x] After session ends: newly captured memories appear in MemPalace drawers and/or Hermes memory files
- [ ] In fresh session: agent receives recalled context from previous session's saves (open — pre_llm_call params not yet fixed)

---

## 7. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| `conversation_history` is large (many message dicts) — search() on raw list fails | Medium | Build search text from last N turns only, not full history |
| Feedback loop module import still fails in installed version | Low | Already handled by except block; warning logged, does not block main recall path |
| Return dict has both `"source"` and `"context"` — Hermes discards if extra keys present | Low | Hermes line 553 only checks `r.get("context")`; extra keys are ignored, not rejected |

---

## 7. Version Tracking

| Version | Date | Author | Change |
|---------|------|--------|--------|
| 1.0.0 Draft | 2026-07-17 | project lead | Initial spec from empirical verification |
| 1.0.1 Updated | 2026-07-18 | project lead | Added noise filter patterns (4.3), updated test plan with pytest automation, added simulation harness coverage table |
| 1.0.2 Final | 2026-07-18 | project lead | Marked P0 fixes as resolved (PR #27), corrected section numbering, aligned acceptance criteria with merged reality |
| 1.0.3 Updated | 2026-08-18 | project lead | Added Behavioral Guard System section (8.x) covering prohibitions model, GuardVerdict/GuardResult, ProhibitionsManager, ProhibitionDistiller; updated hook injection sequence documentation |

---

## 8. Behavioral Guard System

Supersedes Section 2.3.1 for guard-system details. The behavioral guard layer sits between Hermes parameter delivery and MemoryOrchestrator.retrieve(), injecting hard-block guardrails before soft recall. Unlike soft recall which is advisory, guards use `[[double bracket]]` markers that cannot be skimmed past by the LLM.

### 8.1 Prohibition Model

**Source:** `src/memchorus/prohibitions.py`, lines 80-166

```python
@dataclass
class Prohibition:
    id: str                          # stable UUID for deduplication / updates
    condition: str                   # the actual rule statement (injected into prompt)
    trigger_keywords: List[str] = dc_field(default_factory=list)  # fast-path match tokens
    tool_call_check: Optional[str] = None  # compiled as regex at load time
    severity: int = 3               # 1=note, 2=warning, 3=hard block
    block_action: str = ""          # what it blocks (human-readable for prompt)
    rationale: str = ""             # why this rule exists (critical - agents ignore rules without reasoning)
    source: str = "system"          # origin label: system | distilled-from-mistake | manual
    created: str = ""               # ISO-8601 timestamp
    type_: str = "infrastructure"   # category tag
```

**Pre-compilation step:** `_compile_patterns()` runs at load time for each rule. It joins `trigger_keywords` with OR, applies word-boundary case-insensitive matching (`\b(?:kw1|kw2)\b`), and compiles the optional `tool_call_check` string as a separate regex. Compiled patterns are cached in `_compiled_patterns: List[re.Pattern[str]]` so runtime matching has zero compilation overhead (GAP107 performance budget: &lt; 1ms per check).

**Text matching:** `matches_text(text)` iterates compiled patterns and returns `True` on the first match. This is the hot path executed on every guard scan.

**Serialization:** `to_dict()` / `from_dict()` provide round-trip JSON serialization for `prohibitions.jsonl`. The internal `_compiled_patterns` cache is NOT persisted.

### 8.2 GuardVerdict and GuardResult

**Source:** `src/memchorus/prohibitions.py`, lines 30-72

```python
class GuardVerdict(Enum):
    OK      = "ok"      # green light - proceed normally
    WARNING = "warning" # severity 1-2 match; note in prompt but don't block
    BLOCK   = "block"   # severity 3 match; inject hard guard that stops the action
```

```python
@dataclass
class GuardResult:
    verdict: GuardVerdict             # outcome enum (default OK)
    matched_rules: List[Prohibition]  # rules that triggered (default empty)
    timing_ms: float = 0.0           # scan wall-clock time in milliseconds
    errors: List[str] = dc_field(default_factory=list)

    @property
    def triggered(self) -> bool:
        return self.verdict != GuardVerdict.OK

    def inject_blocks(self, source_tag: str = "hermes_agent") -> List[str]:
        # Build [[BEHAVIORAL GUARD]] markdown blocks for prompt injection.
        # Caps at 3 rules to prevent bloat (GAP024).
```

`inject_blocks()` generates formatted markdown using double brackets so they are visually distinct from `[soft recall]` blocks:

\`\`\`markdown
[[BEHAVIORAL GUARD: guard-001-no-editable-install]]

**RULE:** Never run pip install -e (editable install) inside ~/.hermes/ or any self-critical venv path. Always install from pushed GitHub commits only.
**BLOCKS:** Editable installs into self-critical venvs that break the CLI on path shifts
**WHY:** Aug 17 2026 incident: editable .pth shim in self-hosted venv caused ModuleNotFoundError for hermes_cli after path shift. Broken CLI = total agent outage until manual fix.
**SEVERITY:** HIGH

> This is a hard guardrail based on past mistakes. Do NOT override it without explicit human confirmation.
\`\`\`

The blocks list is capped at 3 matched rules to respect GAP024 context bloat limits.

### 8.3 ProhibitionsManager

**Source:** `src/memchorus/prohibitions.py`, lines 218-352

```python
class ProhibitionsManager:
    def __init__(self, data_dir: Optional[Path] = None):
        # Initialize with optional custom data directory.
        # Defaults to data/prohibitions.jsonl in package directory or ~/.memchorus-data/ fallback.

    @property
    def file_path(self) -> Path:
        # Resolve prohibitions.jsonl path; auto-creates parent directories.

    def load(self) -> int:
        # Load rules from disk JSONL. If file missing, seed with 3 default guards.
        # Returns count of loaded/seeded rules.

    def save(self) -> None:
        # Persist all rules back to disk as JSONL.

    def scan_text(self, text: str) -> GuardResult:
        # Scan arbitrary text against all loaded rules. Highest severity across matches wins the verdict.

    def scan_tool_call(self, command: str, args: Optional[str] = None) -> GuardResult:
        # Specialized scan for shell commands. Concatenates command + args before scanning.

    def add_rule(self, rule: Prohibition) -> None:
        # Add a new rule (deduplicates by id).

    def remove_rule(self, rule_id: str) -> bool:
        # Remove a rule by ID. Returns True if found and removed.

    @property
    def rules(self) -> List[Prohibition]:
        # Read-only access to the rule list.
```

**Lazy-loading behavior:** Rules are loaded on first `load()` call, compiled into regex patterns, and cached in memory. The manager is stored as `_prohibitions_manager` on the orchestrator instance for cross-turn reuse (avoids re-reading disk on every LLM call).

**Seed rules:** Three default rules ship built-in (no distillation or training required):

| Rule ID | Trigger Keywords | Severity | Block Action |
|---------|-----------------|----------|-------------|
| `guard-001-no-editable-install` | `pip install -e`, `editable install`, `pip install .`, `-e ~/.hermes` | 3 (HARD BLOCK) | Editable installs into self-critical venvs that break the CLI on path shifts |
| `guard-002-no-scratch-delete` | `rm -rf ~/.hermes`, `remove hermes dir`, `delete site-packages`, `unlink hermes` | 3 (HARD BLOCK) | Removing the agent's own installation directory or venv site-packages |
| `guard-003-no-public-opsec-leak` | `commit message with name`, `local path in code`, `personal identifier` | 2 (WARNING) | Including agent names or local paths in public repository content |

Seed rules live in `_DEFAULT_SEED_RULES` at module level. The `created` timestamp is generated dynamically on first load via `datetime.now(timezone.utc).isoformat()`.

### 8.4 ProhibitionDistiller

**Source:** `src/memchorus/prohibition_distiller.py`, lines 1-383

The distiller converts detected mistakes into enforceable prohibition rules — the post-storage pipeline that transforms observed failures into preventive guards. It integrates via `hooks.on_post_tool_call` error-handling paths.

#### MistakeSeverity Enum

```python
class MistakeSeverity(Enum):
    CRITICAL = 3   # self-destructs environment / loses data / breaks core toolchain
    MEDIUM   = 2   # causes wasted cycles but not destructive
    LOW      = 1   # quality-of-life degradation (e.g. poor recall, duplicate storage)
```

#### DistillationConfig

```python
@dataclass
class DistillationConfig:
    minimum_severity: int = 3          # only CRITICAL mistakes become rules by default
    max_rules_per_session: int = 2     # prevent guard explosion; cap fresh guards per cycle
    cooldown_hours: float = 24.0       # don't create a guard for the same pattern twice in X hours
```

#### ProhibitionDistiller API

```python
class ProhibitionDistiller:
    SELFBREAK_KEYWORDS: List[str]      # 13 patterns: ModuleNotFoundError, venv damaged, pip install -e, etc.
    NONDESTRUCTIVE_KEYWORDS: List[str] # 8 patterns: recall empty, cache miss, no matching memories, etc.

    def __init__(self, config: Optional[DistillationConfig] = None):
        # Initialize with optional config; defaults to DistillationConfig() if omitted.

    @classmethod
    def is_worthy_of_guard(cls, error_text: str) -> MistakeSeverity:
        # Classify whether the given error/mistake text deserves a hard guard.
        # Returns CRITICAL if self-breaking behavior detected, LOW if clearly non-destructive.

    def distill(self, error_text: str, context: Optional[str] = None) -> Optional[Dict[str, Any]]:
        # Attempt to distill a prohibition rule from the given error text.
        # Returns a dict matching Prohibition.from_dict() schema if worthy and passes all gates.
        # Returns None if the mistake should not become a guard.
```

**Three-gate pipeline inside `distill()`:**

1. **Severity gate** — `is_worthy_of_guard()` must return `CRITICAL` (severity >= `config.minimum_severity`, default 3). Non-destructive patterns like cache misses or empty search results are immediately classified as LOW and skipped.
2. **Session cap gate** — at most `max_rules_per_session` new rules per distiller instance (default 2). Prevents guard explosion when errors cascade.
3. **Cooldown gate** — MD5 fingerprint hash of the normalized error text is checked against `_recent_pattern_hashes`. If the same pattern was distilled within `cooldown_hours` (default 24h), the request is silently dropped.

**Keyword extraction:** `_extract_keywords()` pulls matching self-break keywords from the error text first, then falls back to regex-based Python exception type extraction (`ModuleNotFoundError`, `ImportError`, etc.), and finally extracts short multi-word phrases if the text itself is brief enough.

**Rule construction helpers:**
- `_build_condition()` — builds a "Never do X because Y happened" statement from keywords + error summary
- `_build_rationale()` — composes WHY this rule exists, including contextual failure details (critical for agent compliance)
- `_build_block_action()` — extracts the core action being blocked for prompt injection display
- `_build_tool_call_regex()` — generates a safe OR-joined regex from extracted keywords for tool-call scanning

**Output format:** The returned dict contains all fields required by `Prohibition.from_dict()`: `id`, `condition`, `trigger_keywords` (capped at 5), `tool_call_check`, `severity`, `block_action`, `rationale`, `source="distilled-from-mistake"`, `created` (ISO timestamp), `type="infrastructure"`.

### 8.5 Hook Injection Flow Update

The `on_pre_llm_call` hook sequence was modified to insert guard scanning BEFORE soft recall injection:

```
Hermes turn_context.py -> invokes pre_llm_call hook
        |
        v
[Step 1: _scan_prohibitions() / ProhibitionsManager.scan_text(user_message)]
   - Reuses or creates _prohibitions_manager on orchestrator instance
   - Scans current user_message + recent conversation history against all active rules
   - If verdict is BLOCK or WARNING and .triggered is true, generates [[BEHAVIORAL GUARD]] blocks
        |
        v
[Step 2: Guard blocks injected into "context" return dict]
   - Double-bracket markers prevent LLM from skimming past (unlike [bracket] soft recall)
   - Injected via the Hermes turn_context.py contract at line 538-569
        |
        v
[Step 3: MemoryOrchestrator.retrieve() — semantic search/recall]
   - Standard memory recall runs AFTER guard check completes
        |
        v
[Step 4: Combined context injected into LLM prompt]
   - Guard blocks + recalled memories both in final "context" key
```

**Key sequence change:** Previously, soft recall injection ran first on every LLM call. Now `_scan_prohibitions()` fires before `MemoryOrchestrator.retrieve()`, ensuring destructive actions are blocked or warned against before the LLM processes any recalled context about past mistakes. This ordering is critical because: if the agent recalls a past mistake but the prohibition hasn't fired yet, the LLM may still attempt the same action (soft recall alone does not prevent recurrence).

**Performance characteristics:** Guard scan runs synchronously in ~1ms wall time per turn against all active rules using pre-compiled regex patterns. Zero dependency on external services, network calls, or MCP connectivity.

### 8.6 Post-Storage Distillation Integration

The distiller is wired into `hooks.py` error-handling paths:

```
Tool execution fails/errs
        |
        v
[on_post_tool_call hook fires with status="error"]
        |
        v
[_try_distill_prohibition() — Entry Point #2]
   - Imports distiller module (gracefully degrades if unavailable)
   - Calls distill(error_text, context)
   - If CRITICAL and passes all gates: persists new Prohibition via ProhibitionsManager.add_rule()
   - If LOW or blocked by session cap / cooldown: silently logged, no persistence
```

This creates the closed feedback loop: failure detected -> mistake distilled into rule -> next LLM call scans against expanded rule set -> action blocked before execution.
