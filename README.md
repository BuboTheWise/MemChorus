# MemChorus - Memory Orchestrator

MemChorus is the core memory orchestration system for all Hermes agents. It provides intelligent management of multiple memory sources that work together to provide comprehensive, resilient agent memory.

## Installation

```bash
pip install -e .
```

## Quick Start

```python
from memchorus import MemoryOrchestrator

# Initialize orchestrator
orchestrator = MemoryOrchestrator()

# Save a memory
success = orchestrator.save("key1", {"content": "Hello MemChorus"})

# Retrieve a memory
memory = orchestrator.retrieve("key1")

# Search for memories
results = orchestrator.search("hello")
```

## Key Features

- **Multi-source Memory Management**: Integrates with multiple memory backends (local files, knowledge graphs)
- **Graceful Degradation**: Continues functioning even when individual sources are unavailable
- **Resilient Core**: Uses Hermes default memory as the essential foundation
- **Intelligent Retrieval**: Prioritizes memory sources based on relevance and availability  

## Source Structure

MemChorus manages different types of memory sources:
1. `HermesDefaultMemorySource` - Local file-based memory (resilient core)
2. `MemPalaceMemorySource` - Knowledge graph system (primary enhancement)

## How Graceful Degradation Works

The system automatically handles cases where memory sources are unavailable:

1. **Availability Checks**: Each source is checked for availability before use
2. **Error Recovery**: Operations fail gracefully without crashing the system  
3. **Fallback Behavior**: Critical memories are preserved in the Hermes default source
4. **Transparent Operation**: Applications using MemChorus don't see the failures

For detailed implementation details, refer to the GRACEFUL_DEGRADATION.md documentation.