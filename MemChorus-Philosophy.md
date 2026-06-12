# MemChorus Hermes Default Memory Source Implementation

This skill documents the implementation of `HermesDefaultMemorySource` which serves as the resilient core of MemChorus, providing an always-available fallback memory source that works with Hermes' local memory files.

## Overview

The `HermesDefaultMemorySource` class implements the abstract `MemorySource` interface and provides an integration layer to Hermes' local file-based memory system. This source is designed to be the most stable and always-available foundation within MemChorus, ensuring critical context remains functional even when other memory sources (like MemPalace) are unavailable.

## Key Enhancements

### Proactive Memory Behavior
This implementation includes enhanced proactive memory checking and saving behaviors that demonstrate the strong foundational role:

1. **Proactive Check**: `proactive_check()` method that identifies relevant memories before decisions 
2. **Proactive Save**: `proactive_save()` method that reliably stores outcomes after actions, even without other sources

### Core Features

1. **File-based Integration**: Interfaces with standard Hermes memory files (`MEMORY.md`, `USER.md`)
2. **Graceful Error Handling**: Handles missing or empty files gracefully without crashing
3. **Search Capability**: Can search through memories by content and tags  
4. **Save Functionality**: Supports saving new memories to appropriate files
5. **Initialization Safety**: Properly initializes the memory directory and file structure
6. **Logging**: Comprehensive logging for debugging and monitoring
7. **Proactive Memory System**: Explicit methods that show it can independently support decision making

### Memory File Format Support

The implementation handles standard Hermes memory file formats:
- **MEMORY.md**: Agent memories and system information  
- **USER.md**: User context, preferences, and personal notes

Both files use date-based entry format with content following the date:
```
2026-06-10: This is a memory entry [tags]
```

### Key Methods

#### `initialize(config)`
Initializes the Hermes default memory source with configuration parameters.

#### `fetch(query=None, limit=10)`  
Fetches memory items matching the query or all items if no query. Includes date-based sorting and content filtering.

#### `save(item)`  
Saves a memory item to either MEMORY.md (for agent memories) or USER.md (for user context) based on tags.

#### `list_sources()`
Lists available sources in the Hermes default system.

#### `get_source_info()` 
Returns metadata about this source including version, status, and memory directory.

#### `proactive_check(context)`
Performs proactive memory checking before decisions - demonstrates the foundation role.

#### `proactive_save(key, value, context)` 
Performs proactive saving after actions - ensures reliability even without other voices.

## Usage Example

```python
from hermes_memory_source import HermesDefaultMemorySource

# Initialize source
source = HermesDefaultMemorySource()
source.initialize({
    'memory_dir': '/home/bubo/.hermes/memories'
})

# Proactively check for relevant context  
context = {'current_task': 'implement memory orchestration'}
check_result = source.proactive_check(context)

# Save a new memory
success = source.save({
    'content': 'Test memory entry', 
    'tags': ['test', 'development']
})

# Proactively save a decision or outcome
decision_result = source.proactive_save(
    'implementation_decision', 
    {'final_choice': 'use hermes as core'}, 
    {'context': 'memory system design'}
)
```

## Implementation Requirements Met

- ✅ Complete fetch() and save() methods with real integration to Hermes local memory files
- ✅ Graceful behavior when memory files are missing or empty
- ✅ Made this source the most stable and always-available foundation
- ✅ Added appropriate error handling and logging
- ✅ Provided ultimate fallback for critical context
- ✅ **Added proactive memory checking and saving behaviors** that demonstrate the system can function independently as the core

This implementation completes the highest priority task in the MemChorus project, providing a solid foundation that ensures memory continuity even under adverse conditions.