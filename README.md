# MemChorus

**A modular, extensible memory orchestration layer for Hermes AI agents**

MemChorus treats memory as a **chorus** — many distinct voices (memory sources) that can be heard together while still allowing individual voices to be isolated when needed.

## Architecture Overview

```
+-------------------+
|     Agent         |
|  (Hermes)         |
+-------------------+
         |
         v
+-------------------+
|  MemChorus        |
|  Orchestration    |
|  Engine           |
+-------------------+
         |
   +------+------+
   |             |
   v             v
+--------+   +-----------+
| Hermes |   | MemPalace |
| Memory |   | Knowledge |
| Source |   | Graph     |
+--------+   +-----------+
```

## Key Features

- **Multi-source coordination** - Seamlessly integrate multiple memory systems
- **Unified API** - Standardized interface for all memory operations  
- **Privacy-focused** - Respect data boundaries between memory sources
- **Extensible design** - Easy addition of new memory "voices"
- **Efficient orchestration** - Smart relevance scoring and filtering

## Current Implementation Status

This is a development phase implementation focused on:

1. **Hermes Default Memory** - Local curated memory files  
2. **MemPalace** - Persistent knowledge graph and diary system

## Getting Started

### Prerequisites
- Python 3.8+
- Hermes AI agent installed and configured

### Installation
```bash
# Create a working directory
mkdir memchorus-project
cd memchorus-project

# Clone or copy implementation files to this directory
# (For development, just place memchorus.py in your project directory)

# Required: Ensure Hermes memory systems are properly configured
```

### Usage Example  
```python
from memchorus import MemoryOrchestrator, HermesDefaultMemorySource, MemPalaceMemorySource

# Initialize orchestrator
orchestrator = MemoryOrchestrator()

# Add memory sources  
hermes_source = HermesDefaultMemorySource()
mempalace_source = MemPalaceMemorySource()

orchestrator.add_source(hermes_source)
orchestrator.add_source(mempalace_source)

# Initialize all sources
orchestrator.initialize_all()

# Get relevant context
context = orchestrator.get_context("user's project work")

# Save memory item
orchestrator.save_context({"content": "Important update", "tags": ["project"]})
```

## Memory Sources

### Hermes Default Memory 
- Local file-based storage system  
- Curated with user's important information
- Optimized for agent use cases

### MemPalace
- Knowledge graph based on persistent data
- Diary system with natural language entries
- Complex relationship mapping and semantic search  

## API Reference

### MemoryOrchestrator Class

```python
class MemoryOrchestrator:
    def get_context(self, query: str, sources: List[str] = None, 
                   relevance_threshold: float = 0.5) -> List[Dict]
    def save_context(self, context: Dict, source: str = None) -> bool  
    def list_sources(self) -> List[Dict]
    def source_info(self, source_name: str) -> Dict
    def initialize_all(self, config: Dict = None) -> bool
```

### MemorySource Interface
```python
class MemorySource(ABC):
    def fetch(self, query: str = None, limit: int = 10) -> List[Dict]
    def save(self, item: Dict) -> bool
    def list_sources() -> List[Dict] 
    def get_source_info() -> Dict
```

## Development Workflow

This implementation follows the complete development workflow:

1. Local implementation in development environment
2. Testing and verification
3. Code review by Bubo (default reviewer)
4. Integration into Hermes ecosystem  

## Future Enhancements

- Additional memory sources (vector stores, mesh networks)
- Advanced relevance scoring algorithms 
- Cross-source fusion capabilities
- Enhanced privacy controls and encryption
- Performance optimizations for large-scale usage  

## License

This project is currently under development. License information will be added upon release.