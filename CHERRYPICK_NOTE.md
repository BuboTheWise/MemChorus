# Cherry-pick Resolution Note

## Commits targeted from `feat/memchorus-feedback-auto-load-v12`

- `1d7512d` feat: configurable custom_loops_dir + LoadSummary diagnostics (t_858e7472)
- `29e7da9` fix(test): target only timestamped action log files to avoid directory listing order issues

## Resolution

Both commits were already merged into master via PR #16 as commit `58dac26`:
"feat: configurable custom_loops_dir + LoadSummary diagnostics (v1.2) (#16)".

The test fix from `29e7da9` is also present in current master with zero diff.

## Verification performed (2026-07-08, v1.3.03 on master)

### Feature: LoadSummary diagnostics
- PASS: `LoadSummary` importable from `memchorus.feedback_loop.loader` and re-exported via `memchorus.__init__`
- PASS: Returns `NamedTuple(definitions, loaded, skipped_files, warnings)`
- PASS: Gracefully handles missing directories, no-YAML files, malformed YAML

### Feature: custom_loops_dir configuration
- PASS: `MEMCHORUS_CUSTOM_LOOPS_DIR` env var overrides default
- PASS: YAML config `custom_loops_dir` field respected
- PASS: Precedence: env > yaml > hardcoded default (`~/.hermes/custom_loops/`)
- PASS: `auto_load_custom_loops(loop_dir=...)` accepts explicit directory param

### Feature: Test improvements (timestamped action logs)
- PASS: `tests/test_hermes_source_integration.py` uses `startswith('action-action-test-key-')` for file filtering

### End-to-end directory scanning
- PASS: YAML file dropped into `~/.hermes/custom_loops/` discovered and validated by loader

### Test suite
- PASS: 589 tests passed, 4 skipped (0 failures) on v1.3.03 master
