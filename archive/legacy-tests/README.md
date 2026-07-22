## Legacy Test Archive

This directory contains test/benchmark helper files moved out of `tests/` because they are no longer part of the active pytest test matrix or CI pipeline.

Current version at time of archival: **v1.5.08**  
Archived on: **2026-07-22**  
Task: `t_3d11026d` — Audit and clean up legacy benchmark/test helper files

### Archived Files

#### `benchmark_multipass.py` (commit 62d77d4, PR #24)
Multi-pass decision intelligence benchmark. Standalone script (not named `test*.py`, so never collected by default pytest). Not referenced in CI pipeline `.github/workflows/ci.yml`. Was documented as a usage example in README.md and docs/BENCHMARKS.md — those docs have been updated to reflect the move.

#### `_multipass_child.py` (commit 62d77d4, PR #24)
Child subprocess helper exclusively used by `benchmark_multipass.py`. Not imported by any active test_*.py file in `tests/`. Moved together with its parent script.

### Files NOT Archived (Still Active)

The following were audited and confirmed as active members of the current test suite:

- `tests/_behavioral_child.py` — imported by `test_behavioral_integration.py` (collected by pytest, in CI)
- `tests/_persistence_child.py` — imported by `test_multi_run_persistence.py` (collected by pytest, in CI)
- `tests/_session_simulation_child.py` — imported by `test_session_simulation.py` (collected by pytest, in CI)
- `tests/benchmark_memchorus.py` — contains 7 pytest test cases (`TestBaseline`, `TestPostIntegration` classes); collected when run explicitly
