# MemChorus

Memory orchestration for Hermes agents (Bubo + Cthugha).

**Authoritative documentation**: See /home/bubo/.hermes/workspace/Bubo_Wisdom/Projects/MemChorus/

**Current status (post level-set)**: 
- Importable
- HermesDefaultMemorySource (resilient core)
- MemPalaceMemorySource (simulation/fallback for v1.0)
- MemoryOrchestrator with priority + graceful degradation

Use `from memchorus.orchestrator import MemoryOrchestrator`
