"""
Memory Orchestration Package

This package provides the core implementation of MemChorus, a memory orchestration system
that manages multiple memory sources (voices) for intelligent context management.
"""

from memchorus.memory_source import MemorySource
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource
from memchorus.orchestrator import MemoryOrchestrator
from memchorus.behavioral_trigger import BehavioralTrigger
from memchorus.auto_recall_engine import AutoRecallEngine
from memchorus.auto_storage_engine import AutoStorageEngine
from memchorus.enforcement_manager import BehavioralEnforcementManager

__all__ = [
    'MemorySource',
    'HermesDefaultMemorySource',
    'MemPalaceMemorySource',
    'MemoryOrchestrator',
    # Behavioral enforcement v1.1.0
    'BehavioralTrigger',
    'AutoRecallEngine',
    'AutoStorageEngine',
    'BehavioralEnforcementManager',
]

__version__ = "1.1.0"
__author__ = "BuboTheWise"
__email__ = "bubo@nous.systems"
