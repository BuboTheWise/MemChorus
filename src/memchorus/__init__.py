"""
Memory Orchestration Package

This package provides the core implementation of MemChorus, a memory orchestration system
that manages multiple memory sources (voices) for intelligent context management.
"""

from memchorus.memory_source import MemorySource
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource
from memchorus.orchestrator import MemoryOrchestrator

__all__ = [
    'MemorySource',
    'HermesDefaultMemorySource', 
    'MemPalaceMemorySource',
    'MemoryOrchestrator'
]

__version__ = "1.0.0"
__author__ = "BuboTheWise"
__email__ = "bubo@wisdom.systems"
