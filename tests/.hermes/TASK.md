## Task: Fix test regressions from SessionSearchMemorySource addition

### Context
Task t_25a76d04 merged 3 commits into master adding `SessionSearchMemorySource` as the third memory source (alongside `hermes_default` and `mempalace`). Two existing tests failed because they hardcoded assumptions about exactly 2 sources. The fixes are small but necessary before we can claim production readiness.

### Problem
**Test 1:** `test_single_source_operations_work_through_that_path_only()` — Only unregistered `mempalace`, leaving `session_history` still present so `len(orch.memory_sources) == 1` assertion failed (actual: 2).

**Test 2:** `test_all_sources_down_returns_false()` — Mocked only `hermes_default` and `mempalace` as unavailable, but `session_history` was still real/available, so the safety net fallback succeeded when it shouldn't have returned False.

### Fix
Both tests now use iteration over existing source names instead of hardcoded keys, making them forward-compatible with additional sources.

### Verification Evidence
- Both previously-failing tests: ✅ PASSED
- Full test suite (29 tests across both files): ✅ 29/29 PASSED in 4.87s
- Branch: `fix/test-regressions-session-source`
- Files changed:
  - `tests/test_orchestrator_comprehensive.py` — unregisters all non-hermes sources dynamically
  - `tests/test_profile_routing.py` — mocks all available sources as unavailable

### Acceptance Criteria
- [x] Tests pass on branch (verified locally)
- [ ] PR created on GitHub
- [ ] Cthugha reviews (or Orchestrator self-reviews) and approves
- [ ] Squash-merged to master, pushed to origin
- [ ] Installed fresh from GitHub for runtime verification
