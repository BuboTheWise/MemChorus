---
name: memchorus-development
version: 1.0.0
description: Complete workflow for developing MemChorus memory orchestration skill
---

# MemChorus Development Workflow

## Overview

This skill provides the complete development workflow for creating the MemChorus memory orchestration skill, following established Hermes project conventions and integrating with existing memory systems.

## Process

### Phase 1: Project Setup
1. Create proper directory structure following ORGANIZATION.md guidelines
2. Establish documentation files (README, specification, requirements)
3. Set up development environment

### Phase 2: Core Implementation  
1. Implement MemoryOrchestrator class for coordination layer
2. Create MemorySource abstract base class for extensibility
3. Develop specific source implementations (Hermes default + MemPalace)
4. Implement optimization logic for recall and save paths

### Phase 3: Testing & Validation
1. Write comprehensive unit tests 
2. Validate API compliance
3. Test integration patterns
4. Execute example usage scenarios

### Phase 4: Documentation
1. Complete user documentation
2. Add development notes and testing approach
3. Create usage examples

## Key Requirements

- Support multiple memory sources ("voices")
- Integrate with Hermes default memory system  
- Integrate with MemPalace knowledge graph
- Follow existing project conventions in HERMES workspace
- Use Python for implementation
- Maintain extensibility for future sources

## Implementation Details

The core components include:
- MemoryOrchestrator: Main coordination class
- MemorySource: Abstract base for memory interfaces  
- HermesDefaultMemorySource: Integration with local memory system
- MemPalaceMemorySource: Integration with MCP server
- Unified API with standard methods

## Quality Assurance

All development follows established patterns and conventions:
- Proper Python coding standards
- Comprehensive documentation  
- Thorough testing with validation scripts
- No external dependencies beyond standard library

## v1.0 Implementation Status

Successfully completed v1.0 implementation with:

### Core Components
1. **MemorySource Abstract Base Class** - Defines standard interface for all memory sources
2. **HermesDefaultMemorySource** - Integration with local curated memory files (resilient core)
3. **MemPalaceMemorySource** - Integration with MemPalace knowledge graph system (primary voice)
4. **MemoryOrchestrator** - Main coordination layer that manages multiple sources

### Features Implemented
- ✅ Memory retrieval from multiple sources with priority order
- ✅ Memory saving to multiple sources for redundancy
- ✅ Search functionality across all available sources
- ✅ Source availability checking and graceful degradation
- ✅ Integration with Hermes ecosystem conventions
- ✅ Proper package structure for skill distribution

### Testing
- ✅ All modules import correctly
- ✅ Basic functionality validated through example usage  
- ✅ Component instances created successfully
- ✅ Full API compliance verified