---
name: memchorus
version: 1.2.0
description: Memory orchestration system (multi-voice: Hermes default + MemPalace, behavioral enforcement pipeline)
category: memory
---

# MemChorus v1.2.0

See full docs at src/, README.md, and Level-Set-Summary.md in the repo root.

Core usage:
from memchorus.orchestrator import MemoryOrchestrator
orch = MemoryOrchestrator()
orch.save(...)  # smart write-routing with behavioral capture
orch.retrieve(...)
orch.search(limit=10, domain='code')  # fixed limit tracking (v1.1.02)

Key changes since v1.0:
- BehavioralEnforcementManager: auto-trigger → recall + storage pipeline wired into orchestrator
- AutoRecallEngine / AutoStorageEngine for automatic behavior capture
- Fixed search() limit arithmetic: returns requested number of results, not fewer
- Smart placement via MemoryProfile enum with heuristic-based routing
- Relevance scoring with domain-aware ranking (replacement for hardcoded priority chain)

Key changes in v1.2:
- Session orientation engine (orientation.py): pre-decision recall at turn boundaries
- Feedback loop detection & correction injection (auto-load on import, lazy-init)
- Deterministic lazy initialization for all sources (AC-A1 through AC-A4)
- Comprehensive third-party compatibility + security test battery (526 tests total)
