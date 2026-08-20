# MemChorus Iterative Improvement Cycle

## Purpose

This document describes how MemChorus uses test data and benchmark metrics to identify real weaknesses, make targeted fixes, and verify that those fixes actually improve measurable outcomes — rather than relying on speculation or chasing symptoms. The cycle is tightly coupled with the Kanban task system used by [Lead Agent] and [Executor Agent] (the two autonomous agents that maintain this project).

## The Cycle

### Phase 1: Measure (Baseline)

Run the benchmark test suite and capture quantitative output before making any changes:

```bash
# Record baseline metrics
python -m pytest tests/benchmark_memchorus.py::TestBaseline -v --tb=short

# Full test suite health check
PYTHONPATH=src pytest tests/ --tb=short -x --durations=20

# Save JSON benchmark for later comparison
cat $HERMES_HOME/memchorus_benchmarks/\*.json
```

Record the key metrics: average latency per query, recall rate across all queries, and any `LOW`-flagged results. Each benchmark run produces timestamped JSON in `$HERMES_HOME/memchorus_benchmarks/`, so deltas are always available.

The 1260+ unit and integration tests serve as the regression floor — every cycle starts from a green test suite. If existing tests fail, that is the first weakness to address before adding new measurement data.

### Phase 2: Identify (Data-Driven)

Use the metrics to find the real problem, not the loudest one. Concrete examples of data-driven identification:

- **Relevance threshold too aggressive:** If `_RELEVANCE_THRESHOLD = 0.15` drops known-good items from search results in benchmark output, lower it and re-benchmark to quantify recovery
- **MCP startup latency exceeds budget:** If p95 recall timing crosses 10 seconds, investigate whether persistent sessions (the current approach since v1.5.x) are healthier than per-call spawn cycles that starve ChromaDB I/O
- **Profile isolation contamination:** If search under profile "[profile-name]" returns items saved under "default", the filter leak is in `test_profile_isolation_boundary.py`'s data — not a theory but actual test output
- **Recursion guard gaps:** If `test_recursion_guard_deep_nesting.py` shows enforcement hooks firing during save that trigger nested saves, the `RecursionGuard` counter needs expansion to cover additional code paths

The key discipline here is: **no weakness gets tracked unless it has numeric evidence**. A vague feeling that "search is slow" does not qualify. A benchmark showing 250ms average latency on HermesDefault when previous runs showed 85ms does.

### Phase 3: Fix (Kanban Task-Based)

Create properly scoped Kanban tasks for each identified weakness, following the established [Lead Agent]+[Executor Agent] development workflow:

1. **Implementer creates feature branch from `master`** — never direct commits to master
2. **Code committed incrementally** — one logical change per commit with descriptive messages
3. **Review task created for the other agent** via `kanban_create` with explicit parent dependency (`parents=[implementer_task_id]`)
4. **Reviewer checks code quality, test coverage and benchmark delta** before merge approval
5. **[Lead Agent] merges and pushes to GitHub** — [Executor Agent] handles implementation

Quality gates before marking a fix complete:
- CI green on all Python matrix rows (3.11, 3.12)
- No regressions in existing test suite (all 1260+ tests passing)
- Benchmark metrics improved for the targeted metric, or at minimum no regression on other metrics
- Version strings consistent across `__init__.py`, README.md and CHANGELOG.md

### Phase 4: Verify (Evidence)

After merge and push, run the benchmark suite again on the merged code and compare the new JSON output against the pre-fix baseline stored in Phase 1. This step is non-negotiable — a fix that passes unit tests but has no measured improvement to the targeted metric has not been verified.

The `post-commit-push-verification` workflow ensures every local commit actually lands on origin/master. Local completion does not satisfy this gate.

### Phase 5: Document (Evolution Reports)

Store the cycle summary in MemPalace using the evolution-report mechanism:

```
Key findings, benchmark delta, files changed, lessons learned.
```

Evolution reports enable cross-session recall — when the next improvement cycle begins weeks later, the agent can query past results with `mempalace_recall` or `mempalace_search` to see what was tried before and whether those fixes held. This prevents rediscovering the same weakness in consecutive cycles (see `fix-illusion-prevention` skill for the encoding of this discipline).

## Connection to Kanban Tasks

Every improvement decision should be traceable to a Kanban ticket that cites its upstream benchmark data. The task body includes:

- **Metric identifier:** which benchmark measurement triggered this fix
- **Baseline value:** the number before the change
- **Target value:** what good looks like
- **Acceptance criteria:** concrete pass/fail conditions for verification

This eliminates guesswork and ensures every line of code has an evidence-based justification. Tickets without metric traceability should be reclassified as exploratory spikes rather than improvement fixes.

## Anti-Patterns to Avoid

1. **Speculative optimization** — changing code "because it seems slow" without benchmark data is forbidden
2. **Metric fixation without runtime evidence** — pytest counts mean nothing if the actual runtime behavior has not been verified (this happened in Cycle 10 and was corrected)
3. **Scope creep within a single task** — one weakness per Kanban ticket; if more appear, create siblings
4. **Skipping Phase 4 verification** — merging a fix without re-running benchmarks after integration invalidates the entire cycle

## Status

This improvement cycle documentation was established in v1.7.0 alongside `docs/TESTING.md` to provide a complete picture of how MemChorus evolves from functional milestone to measured, evidence-driven iteration.
