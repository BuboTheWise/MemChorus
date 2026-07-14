# MemChorus Benchmark Suite

## Multi-Pass Decision Intelligence Test

Proves that accumulated memory demonstrably improves real-time decision quality across repeated use. This is a polygraph-style physical proof test — not mocked unit tests, but real semantic search against fresh Python interpreters with subprocess isolation.

### Executable

    python3 tests/benchmark_multipass.py --report ~/.hermes/memchorus_report.json

### Methodology

Five passes run through separate isolated Python processes (`PYTHONPATH=""` clears module cache):

| Pass | State | Purpose |
|------|-------|---------|
| 1 Cold start | Zero knowledge seeded | Baseline failure proving empty state has no answers |
| 2 Warm | Level 1 deployment facts loaded | Demonstrates first improvement after real content added |
| 3 Warmer | Level 1 + Level 2 infrastructure facts | Shows continued curve improvement with more context |
| 4 Saturated | All nine domain facts loaded | Peak recall demonstrating full knowledge value |
| 5 Persistence | Fresh process loads previously saved disk files | Proves memory survives complete interpreter teardown |

### Physical Proof Guarantee

- Each child is an independent `subprocess.run()` call — no import caching or shared state
- Disk file timestamps and SHA-256 hashes printed for every JSON document persisted
- Machine-readable report saved as timestamped JSON with its own integrity hash at the bottom
- No mocked fixtures — real domain facts (Python version policies, Kubernetes regions, CI runner versions)

### Expected Metrics

A healthy v1.5.0 install should produce:
- Recall improvement between fifty and ninety percentage points from cold start to saturated state
- Persistence pass maintaining eighty-plus percent of saturated recall proving disk writes actually work
- Latency under twenty milliseconds per query across five passes

### Why This Matters

This is the core verification pattern for MemChorus truth claims. If search works once against mocked data, that means nothing. If it improves measurably as real knowledge accumulates and survives process boundaries — that proves the system actually functions as designed rather than just passing tests.

See `live-verification-enforcement` skill for the full methodology specification behind this test structure.
