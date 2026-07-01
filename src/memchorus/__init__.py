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

# Feedback loop detection + escalation (v1.1.03)
from memchorus.feedback_loop.schema_v1 import (  # noqa: F401
    ConditionSignal,
    FeedbackLoopDefinition,
    SUPPORTED_VERSIONS,
    TriggerEvent,
    validate_schema_v1,
)
from memchorus.feedback_loop.loader import load_feedback_loops  # noqa: F401
from memchorus.feedback_loop.detector import FeedbackLoopDetector  # noqa: F401

__all__ = [
    'MemorySource',
    'HermesDefaultMemorySource',
    'MemPalaceMemorySource',
    'MemoryOrchestrator',
    # Behavioral enforcement v1.1.01
    'BehavioralTrigger',
    'AutoRecallEngine',
    'AutoStorageEngine',
    'BehavioralEnforcementManager',
    # Feedback loop detection + escalation v1.1.03
    'ConditionSignal',
    'FeedbackLoopDefinition',
    'SUPPORTED_VERSIONS',
    'TriggerEvent',
    'validate_schema_v1',
    'load_feedback_loops',
    'FeedbackLoopDetector',
]

__version__ = "1.1.04"
__author__ = "BuboTheWise"
__email__ = "bubo@nous.systems"
