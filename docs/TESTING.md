# MemChorus Testing Strategy

## Overview

MemChorus maintains a multi-layered test suite designed to verify correctness across all memory backends and produce quantitative metrics for continuous improvement. The current suite collects **1260+ tests across 75 modules**, covering unit, integration and end-to-end execution paths.

This document is self-contained — if you are a third-party agent integrating MemChorus, this page tells you what is tested, how to run it locally, what the benchmarks measure, and how failure-mode testing works without needing to read any other file first.

### Test Layers

| Layer | Scope | Approximate Count | MCP Required |
|-------|-------|-------------------|-------------|
| Unit tests | Individual method correctness (scoring, routing, profile classification) | ~900 | No |
| Integration tests | Multi-source orchestration, hook firing, lifecycle behavior | ~300 | Optional |
| E2E MCP tests | Full synthetic natural scenario against live MemPalace server | 10 | Yes (gated by env var) |
| Benchmark metrics | Timing, accuracy and failure-mode measurement | 8+ | Partial |

See `docs/BENCHMARKS.md` for the original multi-pass persistence methodology that preceded the current benchmark module.

## Running the Test Suite

### Local execution (default — MCP tests skipped)

```bash
cd path/to/MemChorus
PYTHONPATH=src pytest -v --tb=short
```

This runs all 1260+ collected tests except the live MemPalace connectivity tests, which are safe to skip when no MCP server is running. The conftest.py file sets up automatic asyncio cleanup between modules and batched garbage collection (every 50 tests) to reduce overhead with `pytest-xdist`.

### With live MCP backend

```bash
RUN_LIVE_MCP=1 pytest -v --tb=short
```

The 10 E2E synthetic tests in `tests/test_synthetic_natural_e2e.py` spawn a real MemPalace subprocess and validate the complete save-recall-search pipeline end-to-end. These require `mempalace` to be installed (typically via pipx). They verify:

1. **Persistent session stability** — the MCP process survives multiple consecutive operations
2. **Save + retrieve round-trip fidelity** — payloads survive a complete write-read cycle with exact content
3. **Semantic search correctness** — saved entries are discoverable by their query terms
4. **Profile isolation** — data written under one profile cannot leak into another
5. **Context window stability** — the session remains coherent across multiple turns

### Parallel E2E failure test

The file `tests/test_mcp_failure_e2e.py` runs a critical end-to-end simulation: it registers both sources, then deliberately unregisters MemPalace to verify that orchestrator save and retrieve continue working through HermesDefault fallback. This proves graceful degradation in production-like conditions without mocking — real source instances, not fakes.

### CI serialization note for MCP tests

The MCP-dependent E2E tests run on a single `pytest-xdist` worker via:

```python
pytestmark = pytest.mark.xdist_group("mcp_e2e")
```

This prevents parallel workers from fighting over the same ChromaDB instance — repeated CI failures taught us that spawning multiple MCP subprocesses in parallel causes I/O starvation and random test ordering. Do not remove this marker. The unit and integration tests still benefit freely from xdist parallelism; only the MCP E2E group is serialized.

## Benchmark Metrics

### What `test_memchorus_benchmark.py` measures

The benchmark module (`tests/benchmark_memchorus.py`) provides quantitative, comparable measurement instead of定性 pass/fail. It defines two test classes:

- **`TestBaseline`** — benchmarks a single `HermesDefaultMemorySource` with no orchestrator, establishing the before-integration floor
- **`TestPostIntegration`** — runs through the full orchestrator with all registered sources, showing what multi-source routing actually adds

Each benchmark run seeds 13 known facts into the source(s), then executes 8 search queries measuring:

| Metric | Meaning |
|--------|---------|
| `latency_ms` | Wall-clock time per query (monotonic clock) |
| `recall_count` | How many expected keys were found in the hits |
| `recall_rate` | Fraction of expected results that appeared (0.0 to 1.0) |
| `top_result_key` | The first result returned — useful for ordering quality checks |

Output lands as timestamped JSON files under `~/.hermes/memchorus_benchmarks/`. Run the benchmark before and after a change, then compare the two JSON outputs to prove or disprove improvement.

```bash
# Generate baseline
python -m pytest tests/benchmark_memchorus.py::TestBaseline -v --tb=short

# Generate post-integration results
python -m pytest tests/benchmark_memchorus.py::TestPostIntegration -v --tb=short

# Produce a delta comparison report from the last two JSON snapshots
python tests/benchmark_memchorus.py --report
```

A recall rate below 0.5 for any query is flagged as `LOW` in the test output and warrants investigation. Average latency across all queries should remain under 100ms for HermesDefault and under 2000ms when MemPalace MCP is involved (accounting for subprocess startup cost).

### How to interpret benchmark results

- **Higher recall_rate** means more expected content was found — a good sign
- **Lower latency_ms** means each query returned faster — also desirable
- **Compare within-test-class**: baseline vs post-integration tells you whether orchestrator overhead actually helps
- **Beware of cache effects**: the LRU cache in the orchestrator (60s TTL, 256 entries) can artificially improve second-run results. The benchmark seeds fresh data each time to minimize this

## Failure Mode Testing Methodology

MemChorus is designed for graceful degradation when backends time out, crash, or become unavailable. The test suite encodes failure-mode coverage in several areas:

### Source unavailability

Tests deliberately unregister or mock-fail individual sources and verify that the orchestrator still serves requests through remaining backends. `test_phase2_multi_source.py` covers the multi-source routing with one source down. `test_mcp_failure_e2e.py` provides the full end-to-end version using real instances.

### MCP subprocess crash recovery

When the MemPalace MCP server process crashes or becomes unresponsive, `MemPalaceMemorySource.is_available()` returns `False` and subsequent calls short-circuit to a cached fallback response. The test in `test_mempalace_mcp_integration.py` verifies that a live session survives at least 10 consecutive save-retrieve cycles before any crash recovery path would even trigger — proving normal operation under sustained load.

### ExceptionGroup handling

Python 3.11+ introduces `ExceptionGroup` for concurrent failures. The tests in `test_mcp_exceptiongroup_handling.py` simulate multiple sources failing simultaneously and verify that errors are collected without the orchestrator crashing or losing partial results.

### Recursion guard testing

Deep nesting of enforcement hooks can cause stack overflow if recursion detection fails. The files `test_recursion_guard.py` and `test_recursion_guard_deep_nesting.py` exercise save-enforce-hook-save chains at 1-3 levels of nesting, including exception paths, confirming the `RecursionGuard` depth counter prevents infinite loops.

### Profile isolation contamination

Tests in `test_profile_isolation_boundary.py` verify that data saved under profile "A" never bleeds into searches executed under profile "B". This tests a class of bug where metadata filters are not applied consistently across all code paths.

## Continuous Integration

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs on every push to `master` and on all pull requests:

- **Python 3.11 and 3.12 matrix** — full pytest suite with `--tb=short -x --durations=20`
- **Routing test threshold** — at least 30 collected tests in `test_mempalace_routing.py` or the job fails
- **Version sync check** — verifies that `__init__.py` version matches README.md
- **Key import verification** — confirms critical imports and routing overlay structure
- **Integration stage** — installs from source and runs E2E routing verification on the installed package

A red build on any matrix row blocks the PR. All tests must pass before merge.

## Conftest Infrastructure

The test configuration in `tests/conftest.py` provides:

- **Automatic asyncio cleanup** between modules (`_cleanup_asyncio_between_modules`)
- **Coroutine teardown suppressor** — known leak patterns from mock artifacts are silently dropped so they do not pollute test output
- **Batched garbage collection** (every 50 tests instead of every test) — reduces xdist overhead by approximately 2 seconds across the full suite of 1260 tests
- **src/ path injection** — ensures workers can import `memchorus` regardless of current working directory
