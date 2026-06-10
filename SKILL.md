---
name: memchorus
version: 0.1.0
description: MemChorus - Memory Orchestration Skill for Hermes agents
category: devops
---

# MemChorus - Memory Orchestration Skill

## Overview

MemChorus is a memory orchestration layer that coordinates multiple memory sources ("voices") to provide coherent, efficient, and privacy-focused memory management for AI agents.

## Philosophy

MemChorus treats memory as a **chorus** — many distinct voices (memory sources) that can be heard together while still allowing individual voices to be isolated when needed.

Instead of a single monolithic memory system, MemChorus acts as the conductor: it coordinates multiple memory backends ("voices"), surfaces relevant context in real time, and keeps the overall system efficient and coherent.

## Core Components

### 1. Memory Voices System
- **Hermes Default Memory**: Local curated memory files for the Hermes system
- **MemPalace**: Persistent knowledge graph and diary system with structured data storage

### 2. Orchestration Engine
- Context gathering from multiple sources
- Relevance scoring and filtering
- Unified memory interface that abstracts underlying storage

### 3. Integration Protocol
- Standardized APIs for memory backends
- Configuration management
- Security protocols for cross-source operations

## Implementation Details

This implementation provides a foundational structure for the memory orchestration system with support for:
1. Hermes default memory integration 
2. MemPalace knowledge graph integration

The skill interfaces with existing Hermes tools and integrates using standard patterns described in the project organization guidelines.

## Usage Example

```python
from memchorus import MemoryOrchestrator, HermesDefaultMemorySource, MemPalaceMemorySource

# Create orchestrator
orchestrator = MemoryOrchestrator()

# Add memory sources
hermes_source = HermesDefaultMemorySource()
mempalace_source = MemPalaceMemorySource()

orchestrator.add_source(hermes_source)
orchestrator.add_source(mempalace_source)

# Initialize all sources
orchestrator.initialize_all()

# Fetch relevant memories
context = orchestrator.get_context("user's recent project work")

# Save a memory
orchestrator.save_context({"content": "Important project update", "tags": ["project"]})
```

## Integration with Hermes

### Memory Provider Interface

This skill implements the standard MemoryProvider interface that Hermes agents use for memory management. The key implementation components:

- **initialize()**: Sets up connections to memory sources
- **system_prompt_block()**: Returns system information for the agent's prompt 
- **prefetch(query)**: Retrieves relevant memories before each turn
- **sync_turn(user, asst)**: Saves conversation history to memory sources

### Configuration  

The skill supports configuration through environment variables and configuration files. Memory source specific settings are passed during initialization.

## Future Enhancements  
- Additional memory sources (vector stores, mesh networks)
- Advanced relevance scoring algorithms  
- Cross-source fusion capabilities
- Enhanced privacy controls