# MemChorus Memory Optimization Logic

This document describes the optimization logic implemented in MemChorus, covering recall relevance scoring, source selection, save placement decisions, and deduplication mechanisms.

## Overview

MemChorus implements a sophisticated memory orchestration system that optimizes both retrieval (recall) and storage (save) operations. The system intelligently decides where to store memories based on their characteristics and ensures that relevant information is efficiently retrieved when needed.

## Recall Relevance Scoring

### Implementation
Relevance scoring in MemChorus follows a multi-factor approach:

1. **Keyword Matching**: Basic term frequency matching between query and memory content
2. **Exact Phrase Matching**: Boosts scores when the complete query appears within content 
3. **Content Analysis**: Calculates match rate based on keyword occurrences relative to total word count
4. **Source Weighting**: Uses combined source priority and relevance score in final ranking

### Scoring Formula
The system calculates relevance using this formula:
```
relevance_score = min(1.0, base_score + (item.relevance_score * 0.3))  
```

Where:
- `base_score` = keyword match rate (normalized)
- `item.relevance_score` = score from source (if available)

### Priority Handling
Memory results are prioritized based on source priority defined in the orchestrator, where sources with higher priority are preferred.

## Source Selection

### Decision Process
The orchestrator makes intelligent decisions about which memory sources to query:

1. **Core Fallback**: Hermes default memory is always included as a core fallback source
2. **Configured Sources**: Uses sources specified by user or defaults to all available sources
3. **Graceful Degradation**: Continues operation if some sources are unavailable

### Source Prioritization
Sources are ordered by priority:
1. `hermes_builtin` - Primary fallback and core reliable memory
2. `mempalace` - Enhanced knowledge graph memory

Results from prioritized sources take precedence when relevance scores are equal.

## Save Placement Decisions

### Multi-Source Saving
When saving memories, MemChorus supports both:

1. **All Sources**: Save to all configured sources with fallback behavior
2. **Specific Source**: Save to a specified source with automatic fallback to Hermes if primary fails

### Fallback Behavior
If saving to a specified memory source fails:
1. System attempts to save to the Hermes default memory as a core backup
2. This ensures data persistence even when enhanced memories are unavailable

### Decision Logic
The system determines optimal storage placement based on:
1. Memory type (user context vs agent memories)
2. Source availability and reliability
3. Persistence requirements

## Deduplication

### Strategy
MemChorus implements a basic deduplication system by:

1. **Content Analysis**: Identifies potentially duplicative content during retrieval
2. **Source-Based Filtering**: Filters out items from low-priority sources when higher-priority alternatives exist
3. **Relevance Thresholding**: Eliminates low-relevance results from consideration

### Optimization Considerations
While MemChorus has the infrastructure for more advanced deduplication, the current implementation focuses on:
- Efficient retrieval of non-duplicate information
- Prioritizing sources in a hierarchical fashion
- Filtering based on relevance to user query

## Performance Optimization

### Caching Mechanisms
The orchestrator supports performance optimization through:
1. **Result Caching**: Stored queries may be cached for faster recall
2. **Batch Operations**: Grouping similar memory operations
3. **Asynchronous Fetching**: Concurrent source access where appropriate

### Memory Management
The system optimizes resource usage by:
1. Limiting fetch results to prevent overwhelming outputs
2. Filtering low-relevance items early in the process
3. Prioritizing reliable sources for core operations

## Extensibility

MemChorus is designed to support additional memory optimization strategies through its modular architecture:
- New memory source types can be added without modifying core logic
- Relevance scoring algorithms can be extended or replaced 
- Source-specific optimizations can be implemented per memory type

This design allows the system to evolve with changing requirements while maintaining efficient, optimized memory operations.