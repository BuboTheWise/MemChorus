# Third-Party Agent Onboarding — MemChorus v2.0.0

Self-contained installation guide for external agents pointing at this repository.

## What You Are Installing

MemChorus is a memory orchestration layer that sits between an AI agent and multiple underlying knowledge stores. It routes, scores, deduplicates, and caches memory operations across pluggable backends so the agent gets relevant context without wasting tokens or compute on every read.

---

## System Requirements (minimum)

| Item |
|------|
| Python 3.11+ with `pip` or alternative package manager |
| An isolated virtual environment for the install (venv, pipx, conda — your choice) |

Optional: If you have [MemPalace](https://github.com/MemPalace/mempalace) installed as an MCP server, MemChorus will connect to it automatically and gain knowledge graph storage plus vector-backed semantic search. Everything still works without MemPalace — the system falls back to local file cache transparently.

---

## Installation Recipes

### 1. Quick install (core orchestrator only)

```bash
python -m venv /path/to/your-venv
source /path/to/your-venv/bin/activate  # Linux/macOS; use 'activate' on Windows
pip install "memchorus @ git+https://github.com/BuboTheWise/MemChorus.git@master"
```

Verify the import works:
```python
python -c "from memchorus.orchestrator import MemoryOrchestrator; print('OK')"
```

### 2. Full MCP connectivity (recommended)

Requires MemPalace installed in your environment. Install via pipx or into your venv whichever you prefer:

```bash
pip install "memchorus[mcp] @ git+https://github.com/BuboTheWise/MemChorus.git@master"
```

MemPalace itself is a separate package:

```bash
pipx install mempalace        # if pipx is available, OR
pip install mempalace         # into an isolated venv, OR
hermes mcp add mempalace      # if your orchestration platform has built-in MCP config management
```

You'll need a `~/.hermes/config.yaml` (or equivalent) with MemPalace command resolution so the MCP stdio transport can find it:

```yaml
mcp_servers:
  mempalace:
    command: /path/to/python
    args: ["-m", "mempalace.mcp_server"]
    env:
      MEMPALACE_PALACE_PATH: ~/.hermes/profiles/<PROFILE>/workspace/mempalace/palace
```

**Per-profile data isolation:** Each agent profile MUST use its own `MEMPALACE_PALACE_PATH` value. Sharing a `.db` file between profiles is the primary cause of memory cross-contamination. At minimum, set separate paths for `default` and any named profiles.

### 2b. Bootstrap (recommended shortcut)

MemChorus v2.0.0+ includes a bootstrap command that generates a complete routing configuration in one step:

```bash
memchorus-init --profile <PROFILE>    # creates ~/.hermes/profiles/<PROFILE>/memchorus.yaml
memchorus-init --dry-run              # preview without writing
```

### 2c. Diagnostic verification

After installation, run the install doctor to verify your environment is healthy:

```bash
memchorus-doctor                     # exit code 0 = clean, non-zero lists failures
```

### 3. Development install

```bash
git clone https://github.com/BuboTheWise/MemChorus.git
cd MemChorus
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,mcp]"    # editable mode for active development
```

---

## Configuration (optional)

MemChorus works without any configuration file — HermesDefaultMemorySource is always available because it maps directly to local disk. To tune behaviour, drop a `~/.hermes/memchorus_config.yaml` with the sections you need:

```yaml
# Source priority and scoring
priority_order:
  - mempalace          # try MemPalace first on reads (if available)
  - hermes_default     # fall back to local files always

lifecycle:
  enabled: false                     # master toggle — default false preserves write-only mode

```

All remaining sub-keys are optional with documented defaults. See [memory-lifecycle-design.md](./memory-lifecycle-design.md) for the full retention and eviction spec if you want automatic sweep behaviour.

---

## Verifying Installation Against MemPalace Backend

Once both packages are installed, you can run the synthetic natural test included in this repo:

```python
# Requires a live MemPalace database with at least some content indexed
cd tests
python3 test_synthetic_natural_e2e.py
```

Expected output shows ten tests covering persistent MCP session liveness, save + retrieve roundtrip, semantic search against pre-existing vector store data, stability under load and profile isolation. All passing confirms production readiness.

---

## Data Isolation Model

Each agent profile should use its own MemPalace database path via `MEMPALACE_PALACE_PATH`. The configuration for each profile maps to its own isolated location:

```yaml
# Inside ~/.hermes/profiles/my_profile/config.yaml or equivalent
mcp_servers:
  mempalace:
    command: /path/to/python
    args: ["-m", "mempalace.mcp_server"]
    env:
      MEMPALACE_PALACE_PATH: ~/.hermes/profiles/my_profile/workspace/mempalace/palace
```

MemPalace stores all vectors, drawers, and knowledge graph data under that resolved path so data cannot leak between agents sharing the same host. The bootstrap command (`memchorus-init --profile <PROFILE>`) handles this correctly automatically.

## Dependencies And Version Constraints

| Dependency | Allowed Version Range | Notes |
|---|---|---|
| Pydantic | `>=2.0, <3.0` | Core orchestrator types rely on V2 schema validation |
| MCP SDK (optional `[mcp]`) | `>=1.0, <2.0` | Pins out breaking client API and transport changes | 

---

## Troubleshooting Common Issues

- **"No such file or directory: config.yaml"** → HermesDefaultMemorySource works without it. Only MemPalace needs the mcp_servers block.
- **MemPalace vector store returns zero results on initial load** → This is normal — semantic search indexes need at least one drawer saved before ChromaDB creates embeddings. Run a save operation then query again.
- **`ChromaInternalError` during saves** → Usually indicates an inherited database state with corrupt metadata segments. Restoring a clean checkpoint or starting a new profile path resolves it.

---

## License — see root directory LICENSE file
