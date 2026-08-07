# MemChorus v1.6.0 — Live Integration Test Methodology

## Purpose

Guarantee that MemChorus memory lifecycle hooks genuinely capture, persist, and recall contextual memories *during real agent turns* — not just in isolated unit scripts. This document is the single repeatable reference for re-running these tests without session context.

---

## Prerequisites

| Requirement | Command to Verify |
|---|---|
| MemChorus v1.6.0 installed via pip (NOT local editable install) | `pip show memchorus 2>&1 \| grep "Version: 1.6"` — must be in `site-packages/`, NOT `src/memchorus/` |
| Zero stale `.pth` shadow files in any profile venv | `find ~/.hermes/profiles/*/venv -name '*.pth' -exec grep memchorus {} \; 2>&1 \| wc -l` → must return 0 |
| All agent profiles have matching memchorus plugin enabled | See Profile Parity Check section below |
| Fresh Hermes gateway process (no stale plugin cache) | Run `/restart` or relaunch terminal |

## Phase A — Write and Disk Verification

**Goal:** Prove that MemChorus logical save calls translate to physical JSON files on disk.

### Steps

```bash
export PYTHONPATH=""
PYTHONPATH="" python3 -c "
from memchorus import MemorySession, HermesDefaultMemorySource, Config

src = HermesDefaultMemorySource()
session = MemorySession(source=src)
saved = session.save('test_integration_key', {'message': 'Phase A proof', 'timestamp': '2026-08-07'}, relevance=0.95)
print(f'Logical save returned: {saved}')

import time, json, pathlib, glob
time.sleep(3)
files = sorted(glob.glob(str(pathlib.Path.home() / '.hermes/memories/*.json')))[-3:]
for f in files:
    stat = __import__('os').stat(f)
    age = time.time() - stat.st_mtime
    with open(f) as fh:
        content = json.load(fh)
    if 'test_integration_key' in f or content.get('key') == 'test_integration_key':
        print(f'DISK CONFIRMED: {f}  (age={age:.0f}s, size={stat.st_size}B)')
        break
else:
    from glob import glob as g
    recent = [f for f in sorted(glob(str(pathlib.Path.home() / '.hermes/memories/*.json')), key=lambda x: __import__('os').stat(x).st_mtime, reverse=True) if __import__('os').stat(f).st_mtime > time.time() - 10]
    for f in recent[:3]:
        print(f'New file (created last 10s): {f}')
"

```

**Acceptance:** JSON file created within 5 seconds of the save call, with timestamp matching execution window.

---

## Phase B — Cross-Process Retrieve + Cache Invalidation

**Goal:** Prove data survives process boundaries and is read from disk (not LRU cache).

### Steps

Run a *separate* Python subprocess that queries for what Phase A wrote:

```bash
PYTHONPATH="" python3 -c "
import memchorus
from memchorus import HermesDefaultMemorySource, MemorySession, clear_cache

src = HermesDefaultMemorySource()
session = MemorySession(source=src)
# Clear the LRU cache to force disk I/O
clear_cache()
found = session.search('Phase A proof')
if found:
    print(f'CACHE-BYPASSED RETRIEVE SUCCESS: relevance={found[0].relevance:.3f}, key={found[0].key}')
else:
    print('DATA NOT FOUND — LRU flush failed to pull from disk')
"
```

**Acceptance:** Keyword search returns the Phase A payload with relevance score >= 0.65, even after explicit cache clearing. Zero `AttributeError` on import.

---

## Phase C — Acceptance Criteria Summary

| Check | Threshold | Expected Result |
|---|---|---|
| Phase A: Logical save returns True | `saved == True` | PASS |
| Phase A: Disk file created within 5s of save | File exists, mtime within window | PASS |
| Phase B: Data survives separate process boundary | Found by keyword search in new process | PASS |
| Phase B: LRU cache cleared, data still available | Relevance >= 0.65 from disk pull | PASS |
| No editables or `.pth` shadows active | `pip show memchorus` Location is site-packages/ | PASS |

---

## Phase D — Live Hook Firing (Real Agent Turns)

**This is the actual proof that hooks work during genuine Hermes agent activity.**

### Prerequisites for this phase

1. Gateway must be freshly started (`/restart`)
2. Confirm `plugins.enabled` contains `memchorus`: `hermes plugins list | grep memchorus` shows **enabled**
3. Snapshot current memories directory: `find ~/.hermes/memories/ -name '*.json' -type f | wc -l`

### Test Procedure

1. Record file count before any agent activity
2. Ask the agent to perform at least 3 tool calls (file reads, terminal commands)
3. Wait 5 seconds after the turn completes
4. Check memories directory again: `find ~/.hermes/memories/ -name '*.json' -mmin -2 -type f | wc -l`

### Acceptance Criteria

- New JSON files appear in `~/.hermes/memories/` with timestamps within 2 seconds of the tool calls
- The next agent turn displays `[MemChorus Memory Recall]` block injected into system context

### Known Failure Points (NOT code bugs)

| Symptom | Root Cause | Fix |
|---|---|---|
| `plugins.enabled: []` in `hermes config show` | Session spawned before memchorus was installed correctly | `/restart` or relaunch terminal |
| Empty `~/.hermes/memories/` after tool calls but logical saves return True | Hooks not registered / entry point shadowed by stale `.pth` file | Remove ALL `.pth` files containing "memchorus" from profile venvs + restart |
| Memory writes go to `~/.hermes/memory/` (singular) instead of `~/.hermes/memories/` (plural) | Test script used wrong directory name — actual path is always plural | Update test expectations to match `HermesDefaultMemorySource.memory_dir` |

---

## Profile Parity Protocol

Before modifying ANY profile config, follow this exact sequence:

1. **Backup:**
```bash  
mkdir -p ~/.hermes/config_backups
TS=$(date '+%Y%m%d_%H%M%S')
cp ~/.hermes/config.yaml ~/.hermes/config_backups/${TS}_bubo_config.yaml.bak
for pf in cthugha grok-reasoner interrogation; do
  cp ~/.hermes/profiles/$pf/config.yaml ~/.hermes/config_backups/${TS}_${pf}_config.yaml.bak  
done
```

2. **Validate YAML parses (before AND after edits):**
```python
import yaml
for cf in ['<path_to_config>']:
    with open(cf) as f:
        data = yaml.safe_load(f)
    p = data.get('plugins', {})
    assert 'memchorus' in p.get('enabled', [])
    assert not isinstance(p.get('disabled'), str), 'disabled must be a list, not string'
```

3. **Required config structure per profile:**
```yaml  
plugins:  
  enabled:
    - memchorus
  
  disabled: []
```

- DO NOT set `entries` blocks manually — v1.6.0 registers via entry point (`memchorus = memchorus.hooks`), not user-config entries
- The old plugin name `hermes-memchorus` is deprecated and does not resolve; use `memchorus` everywhere

---

## Environment Snapshot Template (fill in for each test run)

```
Date: 
MemChorus version (pip show): 
Pip location:  
.pth files present (count):  
Bubo plugins.enabled: [""]
Cthugha plugins.enabled: [""]  
Grok plugins.enabled: [""]
Interrogation plugins.enabled: [""]  
Gateway PID before restart: 
Gateway PID after restart: 
Memories dir baseline count: 
```

---

## Evidence Archive Location

Test results and file snapshots live at:
- `~/.hermes/memories/` (JSON memory files)
- `/tmp/hermes-results/` (terminal output archives)
- `~/.hermes/config_backups/` (pre-modification config copies)
