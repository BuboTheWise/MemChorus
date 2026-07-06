---
name: memchorus
version: 1.3.0
description: Memory orchestration system (multi-voice: Hermes default + MemPalace, behavioral enforcement, lifecycle management)
category: memory
---

# MemChorus v1.3.0

See full docs at src/, README.md, and Level-Set-Summary.md in the repo root.

Core usage:
from memchorus.orchestrator import MemoryOrchestrator
orch = MemoryOrchestrator()
orch.save(...)  # smart write-routing with behavioral capture + lifecycle hooks
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

Key changes in v1.3:
- **Full lifecycle management layer** (§8 Phase 1): LifecycleManager, SweepScheduler, AuditLogger — all opt-in via orchestrator config (`lifecycle_config.enabled`), disabled by default for backward compat (§9)
- **Per-profile retention periods** (lifecycle_retention.py): ephemeral/operational/knowledge profiles with configurable TTL windows; permanent profiles (preference/relationship_graph) never expire
- **Content-assessment-driven eviction engine** (lifecycle_eviction.py): two-phase soft-delete archive before hard-deletion, importance score penalties, duplicate detection, graceful callback failures
- **Periodic automated sweeps**: SweepScheduler runs retention reviews + eviction pipelines on configurable intervals (default 8h)
- **Plugin auto-registration entry point** (hooks.py `register()`): Hermes gateway discovers MemChorus via `hermes_agent.plugins` entry_points hook, ensuring hooks fire for all profiles without manual bootstrap
- **Hook API corrections**: `retrieve()` → `search()`, `save_auto()` → `save()` with deterministic hash keys — fixes silent no-ops in pre-decision recall and post-tool storage
- **RetentionEngine fixes**: `_score_history` reference leak (empty-dict falsy evaluation), permanent profile handling (`None` TTL no longer falls through to ephemeral default)
- Test coverage: 577 tests passing (+4 skipped) across all modules including new lifecycle test suites (228 total lifecycle tests)
