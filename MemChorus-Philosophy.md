# MemChorus - Memory Orchestration Skill

## Overview

MemChorus is a modular, extensible memory orchestration layer that coordinates multiple memory sources ("voices") to provide coherent, efficient, and privacy-focused memory management for AI agents.

The system follows the "memory as a living system" philosophy, where memory is continuously checked proactively before significant actions and saved automatically after important outcomes or decisions.

## Core Functionality

### Proactive Memory Checking
Before any significant action, MemChorus should:
- Check relevant memory sources for useful context
- Retrieve memories that might inform decision-making  
- Leverage existing knowledge to enhance new tasks

This behavioral pattern ensures the system doesn't re-invent solutions and can build upon previous experiences.

### Post-Action Saving Behavior
After important outcomes or decisions, MemChorus should:
- Save newly acquired knowledge to appropriate memory sources
- Maintain consistency in how information is stored across different systems
- Follow a "memory as a living system" approach where knowledge continuously accumulates

## Example Usage

The `example_usage.py` script demonstrates this behavior pattern:

1. **Proactive Check**: Before starting a planning task, system checks for existing context about "planning meeting"
2. **Decision Execution**: After discussing project timeline with team
3. **Outcome Storage**: Saves the decision outcome and context considerations to memory

## Implementation Details

### Memory Sources
The current implementation supports:
- `HermesDefaultMemorySource`: Local file-based storage (MEMORY.md, USER.md)
- `MemPalaceMemorySource`: Knowledge graph and diary system integration

### MemoryOrchestrator Methods
- `get_context(query, sources, relevance_threshold, prioritize_results)`: Retrieve relevant memories from specified or all sources
- `save_context(context, source, fallback_to_hermes)`: Save context to specified or default memory source with fallback behavior

## Memory as a Living System

This philosophy emphasizes that:
1. Memory is continuously checked and updated in response to new situations
2. All valuable information should be captured and preserved 
3. There's no clear distinction between "memory retrieval" and "awareness" - they happen simultaneously
4. The system should evolve organically as new insights are acquired

This is in contrast to systems with static, isolated memories that don't interact with each other or continuously evolve.

## Integration Notes

The implementation demonstrates how:
1. The orchestrator can be used for both proactive searching and saving operations
2. Different memory sources can work together seamlessly  
3. Context-sensitive decisions are possible as different knowledge layers contribute to the same task
4. Both memory retrieval and storage operate according to the "living system" principles

## Development Approach

This demonstrates a clear evolution path:
- Initial setup through `initialize_all()`
- Proactive context checking via `get_context()` before significant actions  
- Outcome saving via `save_context()` after important decisions
- System-wide coherence where all components work together in a unified memory ecosystem