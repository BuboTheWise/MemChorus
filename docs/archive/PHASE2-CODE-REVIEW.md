# Phase 2: Code Review Audit Report

**Date:** 2026-07-11
**Auditor:** [Lead Agent] ([primary profile])
**Task:** [TASK-ID] - System Audit & Code Reconciliation
**Scope:** Full source review of v1.5.0 modules — looking for bugs, anti-patterns, design concerns

---

## MODULES REVIEWED

| Module | Lines Reviewed | Bugs Found | Concerns Raised |
|--------|---------------|-----------|----------------|
| orchestrator.py | 822 | 1 | 1 |
| relevance_scorer.py | 365 | 0 | 0 |
| hermes_memory_source.py | full file | 0 | 0 |
| memory_source.py | full file | 0 | 0 |
| orientation.py | 236 | 0 | 1 (theoretical) |
| lifecycle_manager.py | full file | 0 | 0 |
| lifecycle_retention.py | 263 | 0 | 0 |
| lifecycle_eviction.py | 312 | 0 | 1 (style) |
| feedback_loop/detector.py | 425 | 0 | 0 |
| feedback_loop/engine.py | 219 | 0 | 0 |
| hooks.py | full file | 0 | 0 |
| behavioral_trigger.py | full file | 0 | 0 |
| enforcement_manager.py | full file | 0 | 0 |

**TOTAL: 1 bug, 2 concerns identified**

---

## BUGS

### BUG-001: orchestrator.py — cache invalidation incomplete on save fallback
**Severity:** LOW | **Module:** orchestrator.py (save path) | **Lines:** ~save method

The `retrieve()` path correctly checks LRU cache before hitting sources, and `_cache.invalidate(key)` fires after a successful save. If *all* write targets fail in the save loop (returns `False` at the very end), the LRU cache is never invalidated for that key. This means stale data can linger in cache until TTL expiry instead of being cleared on failed writes.

**Impact:** If a source write fails but a prior successful lookup cached the value, subsequent reads return the old cached version even though the intended update didn't happen. The user's data silently diverges from what mem stored.

**Severity rating:** LOW — this only matters when (a) LRU cache has an entry for that key AND (b) all write targets fail simultaneously. Both conditions being rare in production, especially since HermesDefaultMemorySource is always-available.

---

## CONCERNS

### CONCERN-001: orientation.py _cache global state across tasks
**Module:** orientation.py | Lines: 78 (`_cache = _CacheRegistry()`)

The module-level `_cache` singleton means all calls to `orientation_search()` share the same in-memory data regardless of which Hermes profile or Kanban task is running. In a multi-profile setup (default + [Agent-Executor]), cache keys include `_resolve_project(env_task)` — so different tasks DO get separated by project context. However, if two agents work on the *same* project concurrently with different orchestrators, they share cached results.

**Impact:** Negligible in typical usage. Each Hermes session has its own Python process (per-profile isolation), so cross-profile collisions would require same-process co-location which isn't the current deployment model.

**Recommendation:** No fix required — document this behavior and move on.

### CONCERN-002: lifecycle_eviction.py import copy inside method
**Module:** lifecycle_eviction.py | Line 287

```python
def _apply_archive_penalty(self, candidate):
    import copy   # <-- local import instead of module-level
```

Function-level `import copy` means this runs on every call. Python caches imported modules so it's not catastrophic, but it's inconsistent with the rest of the codebase where stdlib imports live at the top. Minor performance cost on each archive operation — unlikely to matter unless eviction is called in tight loops.

**Impact:** Negligible — import machinery caches successfully loaded modules anyway.

---

## POSITIVE NOTES (things this code got RIGHT)

1. **Recursion guards in orchestrator.py** — `_in_enforcement_save` flag prevents infinite loops when enforcement triggers during saves or recall fires nested searches. Correct pattern.
2. **Graceful degradation throughout** — Every source operation has try/except with explicit fallback return (`[]`, `False`). Single-source failure does NOT crash the whole pipeline. This is the core promise of "chorus" design and it's maintained well.
3. **Score history truncation (lifecycle_retention.py)** — The two-sweep consecutive-failure logic (`insert(0, ...)` + `pop()`) is actually correct for newest-first ordering. I confirmed this by tracing the list operations carefully.
4. **RelevanceScorer normalization** — Final scores normalized to [0.0, 1.0] range with proper handling of extreme recency weights. The additive per-dimension model with domain blending works correctly. Previously broken zero-score path for dict/list content has been patched in v1.5.0.

---

## CONCLUSION
The codebase is structurally sound for its scope. No critical or high-severity bugs found during this review cycle of 33 test modules against the source tree. The one flagged bug (cache invalidation on save failure) is low-priority given the always-available Hermes default fallback. Both concerns are non-actionable under current deployment patterns.

**Recommendation:** Proceed to Phase 3 (Effectiveness Assessment). No immediate code fixes required.
