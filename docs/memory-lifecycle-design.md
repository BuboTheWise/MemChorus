# Memory Lifecycle Management — Design Specification

**MemChorus Level:** L-5 | **Status:** Draft for Review | **Author:** Bubo
**Created:** 2026-06-29 | **Target Version:** v1.1.x (post-alpha)

---

## 1. Problem Statement

MemChorus currently operates a write-only memory model. Once a memory is saved — whether it arrived via `MemoryOrchestrator.save()`, `AutoStorageEngine.capture_outcome()`, or direct backend calls — it persists indefinitely. The only "decay" mechanism is the relevance scoring recency knob (`half_life_days = 30.0`) which lowers retrieval rankings over time but never removes data from storage.

This produces three concrete problems:

1. **Unbounded Growth:** Low-value memories (test entries, transient session observations, auto-captured trivial outcomes) accumulate with no cleanup path. MemPalace drawer counts grow monotonically; Hermes MEMORY.md files bloat beyond useful token context.
2. **Stale Context Pollution:** Memories about code states or configurations that changed remain indexed and retrieved alongside current facts, degrading retrieval signal-to-noise ratio.
3. **No Merging Path:** Near-duplicate memories across drawers or backends waste storage tokens and confuse search deduplication, because a merge-at-write path does not exist.

This document designs the lifecycle management layer that closes these gaps.

---

## 2. Design Principles

### 2.1 Content-Assessment Over Passive Retention

**Primary filter signal is importance/meaningfulness**, not just age. A seven-day-old memory about a critical production decision is more valuable than a week of trivial test artifacts. Age-based policies act as secondary filters or review triggers, not as the sole criterion.

### 2.2 Bounded Growth by Default

The system should have deterministic upper bounds on memory volume per backend. Without explicit user opt-out, drawers and files do not grow forever. This principle drives retention limits, eviction thresholds, and merge-on-write behavior.

### 2.3 Soft-Delete Before Hard-Delete

Nothing disappears permanently on the first pass. Evicted memories move to an archive phase where they remain searchable at reduced priority. Only after surviving a final grace period are memories hard-deleted. User-initiated purge operations skip the archive phase when explicitly requested.

### 2.4 Profile-Aware Policy Application

Each `MemoryProfile` type carries its own lifecycle rules. A user preference is treated differently from an ephemeral session observation, which is treated differently from a structural knowledge artifact. One size does not fit all memory types.

### 2.5 Auditability

Every purge, merge, or archival action produces a structured audit log entry. The agent can review what was removed, why, and when — important for debugging retrieval gaps and for user trust.

---

## 3. Retention Policy

### 3.1 Profile-Based Retention Defaults

| MemoryProfile | Retention Period | Rationale |
|---|---|---|
| `EPHEMERAL` | 7 days | Session observations, transient context, one-shot decisions that lose value quickly |
| `CONTEXT_SENSITIVE_PREF` | 30 days | Context-dependent preferences decay with their environments |
| `USER_PREFERENCE` | Never (permanent) | User preferences persist until explicitly changed by the user |
| `LONG_LIVED_KNOWLEDGE` | 180 days, then review | Structural knowledge artifacts get extended retention; flagged for review after six months |
| `RELATIONSHIP_GRAPH` | Never (permanent) | Entity relationships are foundational and rarely stale |
| `LARGE_DATA_BLOCK` | 30 days | Benchmarks, test results, session dumps — high volume with shelf-life |
| `AUTO` | Inherited from inference | When auto-inferred at write time, the resolved profile's retention applies |

### 3.2 Retention Enforcement Mechanism

Retention is enforced via **scheduled review cycles**, not immediate deletion:

1. On every lifecycle sweep (see Section 6), memories older than their retention period are flagged for review
2. Flagged memories are scored by `RelevanceScorer` using their current relevance score
3. If the memory's importance ranking places it below the configured threshold (Section 4), it moves to archive
4. Memories that survive the review cycle have their retention timer reset

This means a highly relevant memory can persist indefinitely if it continues to score well during reviews — even past its nominal retention period.

### 3.3 Retention Exemptions

- Memories with `importance_score >= 0.85` are exempt from age-based review until they drop below the threshold naturally (via recency decay)
- Memories tagged with a user-set permanent flag (`_pinned: true`) bypass all retention rules entirely
- Archive itself has a separate retention period — archived memories hard-delete after their grace period regardless of exemptions

---

## 4. Eviction and Purge Policy

### 4.1 Eviction Triggers (AND Logic)

A memory becomes eviction-eligible when **all** of its applicable triggers fire simultaneously:

| Trigger | Condition | Default Value |
|---|---|---|
| Importance threshold | `relevance_score < importance_min` | `0.15` |
| Age threshold | `age > profile_retention_period` | per Section 3.1 |
| Duplicate density | Same semantic cluster has >3 near-identical entries | configurable (Section 5) |
| Drawer empty check | Parent drawer contains only evicted items | structural cleanup |

A memory failing only one trigger is NOT eligible for eviction. This prevents good memories from being purged solely because they are old or solely because their importance score drifted (e.g., a rarely-retrieved but high-value reference).

### 4.2 Two-Phase Eviction: Archive Then Purge

```
ACTIVE -> ARCHIVED -> PURGED
   |         |          |
   v         v          v
 normal    reduced    hard
 retrieval priority    delete
```

**Archive Phase (Soft-Delete):**
- Memory moves to an archive drawer/wing within its respective backend
- Still accessible via search but with a severity penalty applied to scoring (`archive_penality = -0.7`)
- Archive retention period: 30 days default, configurable via `archive_grace_days`
- During archive, the memory's original importance score is preserved for audit purposes

**Purge Phase (Hard-Delete):**
- Memory removed from all indexes and storage structures
- Audit log entry recorded with reason code, timestamp, previous score, and originating drawer
- No recovery path after purge — this is irreversible

### 4.3 Purge Reason Codes

| Code | Meaning |
|---|---|
| `AGE` | Exceeded retention period and failed review scoring |
| `IMPORTANCE` | Score below minimum threshold for sufficient duration (2 consecutive sweeps) |
| `DUPE_MERGE` | Subsumed by a higher-quality duplicate during merge-at-write |
| `USER_PURGE` | Explicit user-requested deletion |
| `STRUCTURAL` | Drawer/wing cleanup — container was empty or malformed |

---

## 5. Merging and Deduplication at Write Time

### 5.1 Near-Duplicate Detection

When `MemoryOrchestrator.save(key, value)` is called, the system checks existing memories before committing:

1. **Exact key match:** If the same key exists in any active memory across registered sources, proceed to merge resolution (Section 5.2)
2. **Semantic similarity check:** Content similarity evaluated using text overlap against recent saves (last 48 hours) with threshold `similarity_min = 0.75` default
3. **Cross-source check:** If the same semantic content exists in both Hermes files and MemPalace, flag for consolidation

### 5.2 Merge Resolution Strategy

The resolution depends on the MemoryProfile type:

| Profile | Merge Strategy |
|---|---|
| `USER_PREFERENCE` | **Overwrite by timestamp** — latest value replaces previous; old entry archived with reason code `DUPE_MERGE` |
| `LONG_LIVED_KNOWLEDGE` | **Append and flag for review** — new content added; flagged entities marked for manual merge on next sweep |
| `EPHEMERAL` | **Overwrite** — ephemeral memories rarely need preservation; latest replaces |
| `RELATIONSHIP_GRAPH` | **Union merge** — if a KG fact matches (subject/predicate same), update the validity_from/validity_to window rather than creating duplicate triples |
| `LARGE_DATA_BLOCK` | **Replace** — large blocks should be updated-in-place, not accumulated |
| `AUTO` | Inferred profile's strategy is used |

### 5.3 Merge Audit Trail

Every merge operation records:
- Source memory IDs involved
- Resolution action taken (overwrite, append, union)
- Profile that drove the decision
- Resulting memory content hash for verification

---

## 6. Operational Details

### 6.1 Sweep Frequency and Execution Model

Lifecycle sweeps run as **scheduled background tasks**, not inline with save/retrieve:

| Sweep Type | Frequency | Triggers | Actions |
|---|---|----> every startup, every N hours | Retention checks, eviction, merges |
| On-Write fast path | Every save() | Single memory write | Near-duplicate detection only |

Default sweep interval: **every 8 hours** (`sweep_interval_hours = 8`). Configurable. The reasoning is that sweeps must not block normal operation, but they need frequent enough cadence to prevent significant accumulation between cycles.

### 6.2 Configuration Interface

All lifecycle knobs are exposed via the orchestrator config dictionary and corresponding YAML/environment variables:

```yaml
# ~/.hermes/memchorus_config.yaml (new file) — or passed via MemoryOrchestrator(config=...)

lifecycle:
  enabled: true                          # master toggle; disable to revert to write-only mode

  sweep_interval_hours: 8                # full lifecycle sweep frequency

  retention_days:                        # per-profile retention override defaults
    ephemeral: 7
    context_sensitive_pref: 30
    long_lived_knowledge: 180
    large_data_block: 30
    user_preference: null               # null = never expire
    relationship_graph: null

  eviction:
    importance_min: 0.15                # below this score for 2 consecutive sweeps -> archive
    duplicate_cluster_max: 3            # more than N near-identical entries triggers merge review
    similarity_min: 0.75               # semantic overlap threshold for "near-duplicate"
    
  archive:
    grace_days: 30                      # days in archive before hard-delete
    score_penalty: -0.7                # scoring penalty applied to archived memories

  merge_at_write:
    enabled: true                       # enable pre-save deduplication check
    
  audit:
    enabled: true                       # log all purge/merge/archive events
    log_path: ~/.hermes/memchorus_audit.jsonl  # structured NDJSON log file
    max_entries: 10000                  # rotate before this many entries
```

**Environment variable equivalents:** `MEMCHORUS_LIFECYCLE_ENABLED`, `MEMCHORUS_SWEEP_INTERVAL`, etc., for containerised deployments where YAML paths are less practical.

### 6.3 Graceful Degradation

If the lifecycle sweep fails or its target backend is unreachable:

1. **Sweep skips that backend** and continues with others — no cascade failure
2. The failed sweep is logged to audit trail with `reason: backend_unreachable`
3. Sweep retries on the next interval — no exponential backoff needed since sweeps are periodic already
4. If three consecutive sweeps against the same backend fail, it enters **cooldown** for 24 hours before retrying
5. The orchestrator continues full save/retrieve operation regardless of sweep health — lifecycle is a maintenance layer, not a critical path

### 6.4 Audit Trail Format

```jsonl
{"ts": "2026-06-29T12:00:00Z", "action": "archive", "memory_id": "drawer_abc123", "source": "mempalace", "reason": "AGE_AND_IMPORTANCE", "prev_score": 0.08, "profile": "ephemeral", "drawer": "wing_project/room_code"}
{"ts": "2026-06-29T12:00:01Z", "action": "purge", "memory_id": "archived_def456", "source": "hermes_default", "reason": "USER_PURGE", "prev_score": 0.42, "profile": "large_data_block"}
{"ts": "2026-06-29T12:00:02Z", "action": "merge_overwrite", "memory_id": "drawer_ghi789", "replaced": "drawer_jkl012", "source": "mempalace", "reason": "DUPE_MERGE", "profile": "user_preference"}
```

---

## 7. Component Architecture (New)

The lifecycle layer introduces two new classes alongside existing components:

```
MemoryOrchestrator
    |
    +--> LifecycleManager (NEW)          -- orchestrates sweeps, manages policy application
            |
            +--> RetentionEngine (NEW)   -- handles per-profile age tracking, review scoring
            +--> EvictionEngine (NEW)    -- evaluates triggers, manages archive -> purge pipeline
            +--> MergeEngine (NEW)       -- detect near-duplication at write time
            +--> SweepScheduler          -- periodic execution of full lifecycle sweep

Existing components (unchanged interface):
    RelevanceScorer                     -- already provides importance scoring used by lifecycle
    AutoStorageEngine                   -- save path hooks into MergeEngine before commit
    MemoryOrchestrator                  -- registers LifecycleManager, exposes config passthrough
```

### 7.1 Integration Points

- **MemoryOrchestrator.__init__():** Registers `LifecycleManager` if `lifecycle.enabled` is true; passes existing `_scorer` and audit logger
- **MemoryOrchestrator.save():** Hooks MergeEngine.pre_save_check() before committing to backend
- **SweepScheduler:** Runs every `sweep_interval_hours`, calls RetentionEngine.review_all(), EvictionEngine.evaluate_all()
- **Backend operations:** Archive drawer writes and purge deletes use the same MemorySource ABC methods (save/retrieve/search) — no new backend methods needed

---

## 8. Phased Implementation Plan

### Phase 1: Foundation
- Config schema additions to `MemoryOrchestrator.__init__()` accepting lifecycle config
- `LifecycleManager` class skeleton with sweep scheduler
- Audit logging infrastructure (JSONL writer, rotation)

### Phase 2: Retention + Eviction
- `RetentionEngine` — per-profile age tracking, review scoring integration
- `EvictionEngine` — trigger evaluation, two-phase archive/purge pipeline
- Sweep implementation running retention check -> eviction evaluation cycle

### Phase 3: Merge-at-Write
- `MergeEngine` — pre-save deduplication via semantic similarity
- Profile-specific merge strategies (overwrite, append, union)
- Integration hook on `MemoryOrchestrator.save()` path

### Phase 4: Tuning + Hardening
- Default value calibration based on empirical usage patterns
- Grace degradation testing with backends offline during sweeps
- Documentation + integration test suite

---

## 9. Backward Compatibility

- **opt-in only:** `lifecycle.enabled` defaults to `False` in v1.1.x, enabling existing users to continue write-only operation without change. Default flips to `True` in v2.0 or when the feature exits beta.
- **existing memories unaffected:** Memories written before lifecycle was enabled are preserved at current state; they enter review on their next natural sweep rather than being retroactively flagged
- **config additive:** All new config keys have reasonable defaults; omitting them preserves write-only behavior

---

## 10. Open Questions for Future Iteration

1. Should the importance threshold (`importance_min`) differ per MemoryProfile? Current design uses a single value, but ephemeral memories might tolerate lower thresholds than long-lived knowledge
2. Does archive search need to be opt-in via a separate API flag, or is it always-on with scoring penalties sufficient as guardrail?
3. Should hard-deleted memories have a user-triggered recovery path (trash bin concept), or is pure audit logging sufficient for restoration purposes?
