# Guards as pre_tool_call Gates — Block-Action Contract

This document specifies the contract between MemChorus behavioral guards
(ProhibitionManager) and the Hermes host's tool-dispatch chain, as implemented
for Issue #272 (building on the #264 mechanism).

## Overview

Since #264, behavioral guards are no longer prompt-injection-only. A
severity-3 (BLOCK) guard that matches a tool call produces a Hermes
`pre_tool_call` directive that **aborts execution before the tool handler
runs**. Severity-2 (WARNING) escalates to the human approval gate. Severity-1
and non-matching calls pass through unaffected.

## The Dispatch Chain (host-verified)

```
handle_function_call(name, args)          ← model_tools.py (entry point)
  └─ resolve_pre_tool_block(...)          ← hermes_cli/plugins.py
       └─ _get_pre_tool_call_directive_details(...)
            └─ invoke_hook("pre_tool_call", ...)   ← PluginManager
                 └─ MemChorusHooks.on_pre_tool_call(tool_name=..., args=...)
                      └─ ProhibitionsManager.scan_tool_call(command, ...)
                           → GuardResult (triggered? verdict: block|warning|allow)
```

If a hook returns a directive, `resolve_pre_tool_block` extracts the veto
message and `handle_function_call` **returns it as the tool result without
ever dispatching to the handler**. The model sees `{"error": "<veto message>"}`
instead of the tool's output.

## Directive → Host-Action Mapping

| MemChorus verdict (severity) | Directive returned to host       | Host action                                             |
|------------------------------|---------------------------------|---------------------------------------------------------|
| BLOCK (severity 3)           | `{"action": "block", "message": M, "reason": M}` | Handler NOT called; result is `{"error": M}` |
| WARNING (severity 2)         | `{"action": "approve", "message": M, "reason": M}` | Human approval gate (tools.approval) |
| ALLOW (severity 1 / no match)| `None`                          | Normal dispatch; handler runs as usual                  |
| Guard internal error         | `None`                          | Observer semantics — never veto on a guard bug          |

### Key contract invariants (pinned by tests/test_272_pre_tool_call_gate_e2e.py)

1. **Block without message is ignored.** A `{"action": "block"}` directive
   lacking a non-empty `message` string is silently dropped by
   `_get_pre_tool_call_directive_details` — the tool proceeds. MemChorus
   always pairs `action: "block"` with a non-empty `message` (and `reason`).

2. **Block aborts before handler dispatch.** The tool handler function is
   never called. A sentinel registered on the real `tools.registry` confirms
   zero handler invocations.

3. **The veto message is the tool result.** The model sees
   `json.dumps({"error": M})` — not the handler's output. The message includes
   the guard ID for traceability (e.g. `guard-001-no-editable-install`).

4. **Severity-2 routes to approval.** Not a hard block but a human-gate
   escalation. Fail-closed: approval error/deny/timeout → block with message.

5. **Observer semantics on guard failure.** If the guard pipeline raises
   internally (config missing, scan error), the hook returns `None` and the
   tool proceeds. A guard bug must never block the agent.

6. **Opt-out toggle.** `prohibitions.enabled: false` in the orchestrator
   config disables the gate entirely — all tool calls proceed unvetted.

## Message Format

The veto message is built by `_guard_block_message` and contains:

```
[GUARD] <block_action> — <rationale> (guard: <rule_id>)
```

When multiple rules match, a suffix `(N behavioral guards matched.)` is
appended. The `guard_id` is the `id` field of the matched `Prohibition` rule,
giving the agent (and the user) a stable, greppable reference.

## Test Coverage

`tests/test_272_pre_tool_call_gate_e2e.py` exercises the full chain:

- **AC2 (host contract):** real `handle_function_call` → `resolve_pre_tool_block`
  → `invoke_hook` with a registered callback returning a block/approve directive.
  Proves: block → no execution + veto result; no-directive → execution;
  messageless block → execution (contract invariant #1).

- **AC1 (MemChorus gate):** real `ProhibitionsManager` (seeded rule, compiled
  regex, verdict mapping) + real `MemChorusHooks.on_pre_tool_call` registered
  on the real `PluginManager` hook registry, running through real
  `handle_function_call`. Only the orchestrator singleton is mocked.
  Proves: severity-3 rule blocks execution; seeded default rule blocks;
  non-matching call proceeds; opt-out disables gate; severity-2 → approve;
  guard error → allow.

## Version

Introduced in MemChorus v2.0.74 (Issue #272).