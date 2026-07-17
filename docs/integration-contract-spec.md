# MemChorus-Hermes Integration Specification

**Version:** 1.0.0 | **Status:** Draft -> Implementation | **Author:** Bubo
**Created:** 2026-07-17 | **Verified against:** Hermes source HEAD, MemChorus v1.5.0 installed
**Tracked via:** Kanban task t_04ea7c1d (diagnosis), implementation tasks TBD

---

## 1. Problem Statement

MemChorus lifecycle hooks (`pre_llm_call`, `post_tool_call`, `on_session_start`) are correctly registered and discovered by Hermes at runtime — plugin entry points work, hooks fire repeatedly — but memory auto-capture and recall never actually function because of parameter contract mismatches between what Hermes passes and what MemChorus reads. The result: zero memories saved from tool execution, zero context recalled before LLM calls, despite the pipeline looking complete in documentation.

### Evidence Summary (2026-07-16/17 Bubo verification)

| Symptom | Root Cause | Verified By |
|---------|------------|-------------|
| No memories captured from tool output | `hooks.py:169` reads `kwargs.get("tool_output")` but Hermes passes `"result"` | model_tools.py:1005-1020, agent.log entries |
| No memory recalled before LLM calls | `hooks.py:81` reads `kwargs.get("input_text") or kwargs.get("messages", "")` but Hermes passes `"user_message"` and `"conversation_history"` | turn_context.py:527-536 |
| Recall injection silently blocked | Hooks return `{"injected_context": ...}` but turn_context.py:553 checks only `r.get("context")` for truthy strings | turn_context.py:538-569 |
| Feedback loop context missing | Feedback kwargs (`conversation_length`, `tool_calls_this_turn`, etc.) not present in actual pre_llm_call payload | turn_context.py:527 vs hooks.py:126-129 |

### Prior Incorrect Assessment

A previous analysis hypothesized that Hermes does not scan entry points and that a bridge adapter file at `~/.hermes/hooks/` was needed. This was disproven: `hermes_cli/plugins.py:217` explicitly defines `ENTRY_POINTS_GROUP = "hermes_agent.plugins"`, line 1658 calls `importlib.metadata.entry_points()`, and line 1870 invokes `register(ctx)`. Agent logs confirm hooks fire repeatedly. The mechanism works; the field names are wrong.

---

## 2. Contract Specification

### 2.1 Hermes Hook Invocation Signature — post_tool_call

**Source:** `/home/bubo/hermes-agent/model_tools.py:974-1025`

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

**Source:** `/home/bubo/hermes-agent/agent/turn_context.py:522-570`

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

### 2.3 pre_llm_call Return Shape Contract

**Source:** turn_context.py:538-569

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

## 3. Post-toolcall Auto-Storage Policy (updated 2026-07-17)

### 3.1 BehavioralTrigger Gate with Length Fallback

**File:** `src/memchorus/hooks.py`, `on_post_tool_call()`
**Implemented in:** commits a74663c, 7470890 (2026-07-17)

The `post_tool_call` hook applies a two-layer filter before passing content to auto-storage:

1. **Behavioral decision-point detection** — if `BehavioralTrigger.detect(output_str)` returns True, the content passes regardless of length. Catches planning, reflection, and architectural reasoning patterns.

2. **Length-based unconditional fallback** — if output is >= 150 characters AND no behavioral markers detected, the content STILL passes through auto-storage. This prevents real but non-decisional output (git status, pip summaries, diagnostics) from being silently skipped.

3. **Short output gate** — results below 150 characters with no detected decision points are dropped to prevent noise-flooding from trivial outputs like `'OK'` or empty stubs.

**Configurable:** `config.auto_storage.min_unconditional_length = 150` (default). Lower values increase noise risk; higher values skip legitimate short diagnostics.

### 3.2 Query Echo Artifact Filter

**File:** `src/memchorus/auto_storage_engine.py`, `_is_query_echo()` function
**Implemented in:** commits a74663c, 7470890 (2026-07-17)

Before content reaches auto-storage, it passes through `_is_query_echo()` which deterministically detects recall query templates — the structured search prompts that `on_post_llm_call()` injects into the conversation. Without this filter, those queries leak back through the tool pipeline and get stored as genuine memory content, polluting the knowledge base with artificial "recall" artifacts rather than actual observations.

Returns True for patterns matching `[MemChorus Memory Recall]` blocks and similar query echo structures. Content flagged as query echoes is silently dropped with a debug log entry.

---

## 4. Expected Behavior After Fix

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

## 5. Test Plan

**Acceptance Criteria:**
- [ ] Live session: post_tool_count hook entries in agent.log show non-None results (not just ENTRY)
- [ ] Live session: pre_llm_call hook returns dicts with `"context"` key containing actual memory content
- [ ] After session ends: newly captured memories appear in MemPalace drawers and/or Hermes memory files
- [ ] In fresh session: agent receives recalled context from previous session's saves

**Verification Commands:**
```bash
# Check if hooks are producing non-empty results (not just firing)
grep "MemChorus.*save\|MemChorus.*recall" ~/.hermes/logs/agent.log | tail -20

# Verify memories were actually written to disk
ls -la ~/.hermes/memories/ | wc -l

# Check MemPalace drawer count changed since last run
mcp mempalace_status  # or equivalent
```

---

## 6. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| `conversation_history` is large (many message dicts) — search() on raw list fails | Medium | Build search text from last N turns only, not full history |
| Feedback loop module import still fails in installed version | Low | Already handled by except block; warning logged, does not block main recall path |
| Return dict has both `"source"` and `"context"` — Hermes discards if extra keys present | Low | Hermes line 553 only checks `r.get("context")`; extra keys are ignored, not rejected |

---

## 7. Version Tracking

| Version | Date | Author | Change |
|---------|------|--------|--------|
| 1.0.0 Draft | 2026-07-17 | Bubo | Initial spec from empirical verification |
