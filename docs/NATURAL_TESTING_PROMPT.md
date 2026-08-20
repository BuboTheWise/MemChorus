# MemChorus Natural Language Integration Test Prompt

Send the following prompt to the agent being tested. It is designed to hit multiple behavioral trigger decision points in sequence, forcing the MemChorus hooks to fire auto-save and auto-recall through MCP channels during normal turn processing.

---

## Test Prompt

I need you to plan and execute a multi-step research task that will exercise your memory system thoroughly. Follow these steps carefully:

1. First, plan your approach for investigating whether three specific Python modules (`adaptive_threshold`, `behavioral_trigger`, `lifecycle_eviction`) are production-ready by checking their test coverage metrics and identifying any remaining gaps.

2. Execute that plan — actually run the commands to inspect test coverage or module contents. Do not just describe what you would do; make real tool calls.

3. After you complete each major step, save your observations to memory so they persist for future sessions. Include concrete findings like specific coverage percentages and which lines remain untested.

4. When reporting results, recall any prior findings from previous sessions about the same modules so you can compare whether coverage improved or regressed.

Important constraints:
- You must use real tool calls (terminal commands, file reads, Python imports) — do not synthesize data from scratch.
- If you encounter an error or unexpected result during execution, document it explicitly by naming what failed and why before moving on.
- After completing the investigation, produce a synthesis that summarizes your findings into durable patterns worth remembering.

---

## Why this works

This prompt hits four of five DecisionPoint types in sequence:

| Step | Trigger fires because... | MemChorus action |
|------|------------------------|------------------|
| 1 — "plan your approach" | PLANNING_START keywords detected | Hook fires, may trigger recall of prior plans |
| 2 — "execute that plan ... real tool calls" | TOOL_CALL_INTENT detected | Hook fires, auto-save of command context |
| 3 — "save your observations" | Explicit persistence request + POST_ACTION_COMPLETE | MemPalace MCP save triggered |
| 4 — "recall any prior findings" | Explicit recall request | MemPalace MCP recall/search triggered |
| Error handling fallback | ERROR_STATE if anything fails | Mistake logging to MISTAKE wing |

The result: real MCP tool calls (mempalace_search, mempalace_diary_write, etc.) fire through the normal hook chain, content persists to the database, and you can verify persistence by checking whether new drawers or diary entries appeared after the run.

## Verification steps

After running the prompt, check for these signals:
1. `mempalace status` — drawer count should have increased
2. `mempalace_diary_read` — should contain a new session entry covering this task
3. Search for saved content matching the investigation topic using `mempalace_search(query="coverage")`

If drawers were created and diary entries recorded, the natural-language-to-MCP pipeline works end-to-end.
