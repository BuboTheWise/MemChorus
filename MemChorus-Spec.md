# MemChorus - Specification

**Version**: 1.0.1

## Philosophy

MemChorus is not just a memory storage system — it is a **memory behavior layer**. Its purpose is to make memory usage as efficient, real-time, and natural as possible, similar to how humans constantly store and recall context with minimal friction.

The core questions MemChorus continuously answers are:
1. What is the most efficient way to retrieve the context needed to make a decision right now?
2. What is the most appropriate place to store this memory for future use?

## Core Design Principles

### Memory as a Living, Optimized System
- Memory operations should be optimized on both read (recall) and write (save) paths.
- On recall: intelligently select, combine, and prioritize sources based on relevance and efficiency.
- On save: decide optimal storage location, avoid duplication, and support consolidation or promotion of memories over time.
- The system should continuously improve memory placement and retrieval effectiveness.

### Foundational Layer
- Hermes default memory (local curated files) is the **lowest-level foundation** of MemChorus.
- Even with no other memory sources installed, MemChorus must still improve the behavior and utilization of the default Hermes memory.
- It is not merely a fallback — it is the core that all other voices build upon.

### Multi-Voice Architecture
- Memory is treated as a **chorus** of distinct sources ("voices").
- MemPalace is the default and primary voice.
- Hermes default memory (local curated files) is the resilient core that must remain functional even if other voices are unavailable.
- The architecture must support adding new, unknown voices in the future without core changes.

### Real-Time Integration
- Memory checking should happen proactively before significant actions.
- Important outcomes should be saved after actions.
- The overhead of memory operations should be minimized to support real-time decision making.

## v1.0 Scope

For the first version, MemChorus will be built on two existing backends:

1. **Hermes Default Memory** — Local curated memory files (MEMORY.md, USER.md, session context). This is the ultimate fallback and resilient core.
2. **MemPalace** — Persistent knowledge graph and diary system. This is the primary enhancement voice.

The implementation should provide enough backend facilities to exercise and prove out the optimization and orchestration logic.

## Key Functional Areas

### 1. Memory Source Management
- Abstract MemorySource interface for pluggable backends
- Configuration and enable/disable of sources
- Graceful degradation when sources are unavailable

### 2. Optimized Retrieval
- Relevance scoring across sources
- Intelligent source selection and combination
- Caching and performance optimization

### 3. Optimized Storage
- Smart placement decisions based on memory characteristics
- Deduplication and consolidation logic
- Support for memory promotion or migration between sources over time

### 4. Orchestration Engine
- Unified context interface for agents
- Proactive memory checking before actions
- Post-action memory saving behavior

## Non-Functional Goals

- Low overhead for real-time use
- Clear separation between core resilience (Hermes default) and enhancement (MemPalace)
- Extensible design for future memory sources
