# MemChorus — Readiness Checklist & Next-Steps Plan

**Date:** 2026-06-24
**Version:** 1.0
**Author:** [Lead Agent] ([primary profile])
**Task:** [TASK-ID] (child of [TASK-ID], synthesized from [TASK-ID] assessment parent chain)

---

## 1. Executive Summary

MemChorus v1.0 is architecturally sound with all core source modules verified on disk, documentation reconciled and clean, and token-cost analysis showing net-positive ROI at current usage (4+ tasks per session). The primary blocker to resumed production deployment is expanding the test suite to cover failure-simulation scenarios. This document provides a prioritized checklist of prerequisites, short-term milestones, resource requirements, and explicit success criteria for the first development sprint.

**Overall Status: READY WITH CONDITIONS — Go/No-Go recommendation from Comprehensive Assessment is GO, contingent on addressing P0 items below.**

---

## 2. Current State Snapshot

### Source Code (Verified On Disk)
| Module | Path | Status |
|--------|------|--------|
| `orchestrator.py` | Code/MemChorus/src/memchorus/ | Present, 8,470 bytes |
| `_memchorus_core.py` | Code/MemChorus/src/memchorus/ | Present, 11,461 bytes |
| `memory_source.py` | Code/MemChorus/src/memchorus/ | Present, 2,605 bytes (ABC) |
| `hermes_memory_source.py` | Code/MemChorus/src/memchorus/ | Present, 11,036 bytes |
| `mempalace_memory_source.py` | Code/MemChorus/src/memchorus/ | Present, 3,051 bytes |
| `__init__.py` | Code/MemChorus/src/memchorus/ | Present, version "1.0.0" |

### Test Files (Verified On Disk)
| Test File | Coverage | Status |
|-----------|----------|--------|
| `test_memchorus.py` | General orchestrator behavior | Committed |
| `test_hermes_source.py` | HermesDefaultMemorySource adapter | Committed |
| `test_graceful_degradation.py` | Graceful degradation paths | Committed (basic) |
| `test_imports.py` | Import path validation | Committed |
| `integration_test_memchorus.py` | Integration demonstration | Untracked, not committed |
| `example_memchorus_usage_improved.py` | Usage examples | Untracked, not committed |

### Documentation (Verified On Disk)
- **9 canonical documents** in Bubo_Wisdom/Projects/MemChorus/ — all reconciled, naming conventions enforced
- Errant Vault/MemChorus/ duplicate directory removed during reconciliation (task [TASK-ID])
- Code repo README.md and SKILL.md present and correct

### Git State
| Check | Status |
|-------|--------|
| Latest commit | `f99b37d` "level-set cleanup for MemChorus" |
| Ahead of origin | **1 commit** (needs push) |
| Review branch | `review/mempalace-memorysource-review` exists with 18 changed files |
| .gitignore | **MISSING** — needs creation to exclude __pycache__ artifacts |
| Untracked files | `*.[pc]`, `example_memchorus_usage_improved.py`, `integration_test_memchorus.py`, `final_test.sh` in repo root |

---

## 3. Readiness Checklist

### P0 — Blockers (Must resolve before first sprint)

- [ ] **Push local commit ahead of origin**
  - Master branch is 1 commit ahead (`f99b37d`). Push to remote so repository state matches.
  - Action: `git push origin master` from [Lead Agent] profile only (per GitHub operations rule).

- [ ] **Add .gitignore to exclude pycache artifacts**
  - Multiple `__pycache__/` entries appear in working directory status. This pollutes git, inflates repo, and risks committing compiled bytecode.
  - Action: Create `.gitignore` covering: `__pycache__/`, `*.pyc`, `*.egg-info/`, `.pytest_cache/`.

- [ ] **Install package for test execution**
  - Tests fail with `ModuleNotFoundError: No module named 'memchorus'`. The src-layout requires `pip install -e .` or PYTHONPATH pointing to `src/` before pytest can import the module.
  - Action: Run `pip install -e .` in an isolated venv or set PYTHONPATH appropriately.

- [ ] **Expand graceful degradation test coverage (Failure Simulation — P0 per Comprehensive Assessment)**
  - Current `test_graceful_degradation.py` covers basic availability scenarios but lacks mocked MCP-server-unavailable end-to-end testing, deterministic dual-source overlap priority ordering, and profile-isolation boundary enforcement.
  - Action: Write three new test modules or extend existing ones:
    1. `test_mcp_failure_e2e.py` — mock MemPalaceMCP server being offline, verify HermesDefault continues serving queries without crash.
    2. `test_priority_ordering_overlap.py` — inject overlapping results from both sources and verify correct deduplication + priority scoring.
    3. `test_profile_isolation_boundary.py` — attempt cross-profile memory contamination and verify architectural enforcement blocks it.
  - Assignee: [Agent-Executor] (primary coding agent).

### P1 — Immediate Sprint Targets (v1.0 Hardening)

- [ ] **Clean untracked files from repo root**
  - `integration_test_memchorus.py`, `example_memchorus_usage_improved.py`, and `final_test.sh` are in the repo root but not committed. Decide: commit or delete based on whether they add value beyond existing tests/examples.
  - Action: Review each, either commit with descriptive message or remove if obsolete/superseded.

- [ ] **Close the review branch**
  - `review/mempalace-memorysource-review` has diverged from master with documentation files migrated into code (violating documentation separation pattern). Rebase or merge clean state before deleting.
  - Action: Assess remaining diff against master, cherry-pick any surviving code changes that predate the last level-set cleanup, discard documentation bleed-through, delete branch.

- [ ] **Implement session-level search result cache**
  - Token cost analysis ([TASK-ID]) estimates 30-40% reduction in redundant MCP queries by caching results within a session.
  - Action: Add an LRU-style cache keyed `(query, source_name, ttl)` to `_memchorus_core.py`.

- [ ] **Lazy health monitoring with TTL**
  - Reduce per-operation health check overhead from ~60 tokens to ~12 tokens by checking availability on a time/count interval rather than every call.
  - Action: Add `health_check_ttl` parameter to `MemoryOrchestrator.__init__`, implement TTL-aware availability checks.

### P2 — Medium-Term (v2.0 Planning)

- [ ] **Adaptive query strategy**
  - Skip MemPalace search when Hermes injection already has sufficiently relevant context in recent turns. Requires query relevance scoring against existing memory content.

- [ ] **Batch MCP round trips**
  - Group multiple semantic searches into fewer, broader queries to reduce per-call overhead.

- [ ] **Resolve cross-profile KG deployment decision**
  - Separate palace directories per profile vs shared directory. Blocker for full rollout of cross-profile memory sharing.

---

## 4. First Development Sprint — Milestones & Success Criteria

### Sprint Duration Target: 2 weeks (10 working days)

#### Milestone 1: Foundation (Day 1-2)
| Deliverable | Success Criterion |
|-------------|-------------------|
|.gitignore committed and effective | No `__pycache__` or `.pyc` in `git status` after next run |
| Local commit pushed to origin | `origin/master` matches local HEAD |
| Package installable via `pip install -e .` | `import memchorus` succeeds in clean shell environment |

#### Milestone 2: Test Suite (Day 3-5)
| Deliverable | Success Criterion |
|-------------|-------------------|
| MCP failure simulation test | `pytest test_mcp_failure_e2e.py -v` passes; orchestrator degrades gracefully when MemPalace offline, zero crashes |
| Priority ordering overlap test | `pytest test_priority_ordering_overlap.py -v` passes; overlapping source results correctly deduplicated and ranked |
| Profile isolation boundary test | `pytest test_profile_isolation_boundary.py -v` passes; cross-profile attempts blocked at orchestrator layer |
| All existing tests still pass | Full suite: `pytest -v` returns exit code 0, no failures |

#### Milestone 3: Untracked file cleanup & branch resolution (Day 6-7)
| Deliverable | Success Criterion |
|-------------|-------------------|
| Repo root clean of obsolete untracked files | `git status` shows only expected changes |
| Review branch merged or deleted | `git branch -a` no longer shows stale review branch, master contains any surviving useful code |

#### Milestone 4: Performance optimizations (Day 8-10)
| Deliverable | Success Criterion |
|-------------|-------------------|
| Session-level cache implemented | Unit test proves 30%+ query reduction over cached results; cache TTL configurable |
| Lazy health monitoring with TTL | Health check token overhead reduced by >75% (60 to ~12 tokens/op); unit test proves interval-based checking |

---

## 5. Resource Requirements

### Required for This Sprint
| Resource | Details | Status |
|----------|---------|--------|
| [Lead Agent] ([primary profile]) | Orchestrator, code review, GitHub operations | Available |
| [Agent-Executor] (executor profile) | Primary implementation of P0/P1 code changes | Available via Kanban assignment |
| Python environment with memchorus installed | `pip install -e .` in test workspace | Setup needed (P0 item 3) |
| pytest + mock framework | For failure simulation tests | Already available (pytest 9.1.0 present) |

### Not Required for This Sprint (Future Consideration)
| Resource | Details | When Needed |
|----------|---------|-------------|
| Integration test CI pipeline | GitHub Actions or equivalent | Post-sprint P2 milestone |
| Cloud API testing environment | For token cost validation on non-local providers | v2.0 planning phase |
| Performance benchmarking harness | Quantified cache/latency measurements | Post-cache implementation review |

---

## 6. Risk Register

| Risk | Severity | Current State | Mitigation |
|------|----------|---------------|------------|
| MCP server unavailable during test runs | Low | Tests can mock; graceful degradation handles it natively | Use mocks for all offline-path testing |
| Review branch diff is messy to resolve | Medium | 18 files changed, many are doc bleed-through | Cherry-pick only code changes; discard docs from review branch |
| Existing tests may break after package install fix | Medium | Tests currently fail at import stage | Run full suite immediately after P0 item 3 resolves |
| Agent time allocation ([Lead Agent] + [Agent-Executor]) | Medium | Kanban board load determines priority | Ensure sprint tasks are discrete and independently completable |

---

## 7. Dependencies & Prerequisite Ordering

```
P0-1: Push local commit to origin
       |
P0-2: Add .gitignore
       |
P0-3: Fix package installation for test execution
       |
       +---> P0-4a: MCP failure simulation tests
       +---> P0-4b: Priority ordering overlap tests
       +---> P0-4c: Profile isolation boundary tests
                |
              P1-1: Clean untracked files (parallel)
                |
              P1-2: Close review branch (parallel)
                |
              P1-3: Session-level cache (depends on test suite passing)
                |
              P1-4: Lazy health monitoring with TTL (depends on test suite passing)
```

---

## 8. Open Decisions

| Decision ID | Description | Options | Decision Maker | Status |
|-------------|-------------|---------|----------------|--------|
| D-001 | Should `integration_test_memchorus.py` be committed or deleted? | Commit, delete, or rewrite | [Lead Agent] (review) | Pending after Milestone 3 assessment |
| D-002 | Cache TTL default value? | 60s / 120s / configurable in orchestrator | [Lead Agent] + [Agent-Executor] discussion | Open |
| D-003 | Should graceful degradation tests use unittest.mock or pytest-mock fixtures? | Either; pytest-mock is less boilerplate | [Agent-Executor] implementation choice | Open |

---

## 9. Next Immediate Action

**Push forward after P0 items are complete.** The Comprehensive Assessment ([TASK-ID]) gives a clear GO recommendation with conditions. This checklist captures those conditions in actionable form. Once the four P0 blockers are cleared and the first set of failure-simulation tests passes, MemChorus v1.0 can enter production deployment mode with confidence.

---

*Document produced by [Lead Agent] ([primary profile]) as deliverable for Kanban task [TASK-ID] on 2026-06-24.*
*Synthesized from: Comprehensive Assessment ([TASK-ID]), Reconciliation Report, ground-truth repository state verification, and token-cost analysis ([TASK-ID]).*
