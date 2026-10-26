# MemChorus Documentation Index

This index provides an overview of the MemChorus project documentation available in this vault.

## Core Documents

### Philosophy
- [Philosophy](./Philosophy.md) - Core principles and vision behind MemChorus 

### Requirements
- [Requirements](./Requirements.md) - Detailed functional and non-functional requirements

### Specification
- [Specification](./Spec.md) - Complete specification for v1.0 implementation

### Reconciliation Report
- [Reconciliation Report](./Reconciliation-Report.md) - Documentation of the project restructuring process

### Optimization
- [Optimization](./Optimization.md) - Performance considerations and optimization strategies

## Implementation Details

For implementation-specific information, see the MemChorus source code in the official repository at `$HOME/.hermes/workspace/Code/MemChorus/`

## Integration with Hermes Ecosystem

The MemChorus system is designed to integrate seamlessly with the Hermes AI agent framework. The primary use case involves importing and configuring memory sources as shown in the examples below:

```python
from memchorus.orchestrator import MemoryOrchestrator
orch = MemoryOrchestrator()
orch.save(...)
orch.retrieve(...)
```

For more information on integrating MemChorus into Hermes agents, refer to the relevant SKILL.md files and integration test examples in the source repository.