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
    # Auto-bootstrap v1.2
    '_instance',
]

__version__ = "1.2.0"
__author__ = "BuboTheWise"
__email__ = "bubo@nous.systems"

# Lazy bootstrap guard (set to True by __getattr__ after first trigger).
# NOT in __all__ — it's an internal signal, not a user-facing API symbol.
_bootstrap_done: bool = False


def _trigger_lazy_bootstrap():
    """Execute auto-bootstrap once (threading-safe within this module)."""
    global _bootstrap_done  # noqa: PLW0603
    if _bootstrap_done:
        return
    from memchorus.auto_bootstrap import _bootstrap as _orig_bootstrap
    result = _orig_bootstrap()
    # Store in module dict so future attribute accesses resolve directly.
    sys.modules[__name__]._instance = result  # type: ignore[attr-defined]
    _bootstrap_done = True  # noqa: PLW0641


def __getattr__(name: str) -> object:
    """Lazy init descriptor: fires bootstrap on first attribute access."""
    global _bootstrap_done  # noqa: PLW0603
    # Bootstrap trigger — must run before any resolution.
    if not _bootstrap_done:
        sys.modules[__name__]._instance = None  # type: ignore[attr-defined]
        from memchorus.auto_bootstrap import _bootstrap as _orig_bootstrap
        result = _orig_bootstrap()
        if result is not None:
            sys.modules[__name__]._instance = result  # type: ignore[attr-defined]
        _bootstrap_done = True

    # Resolve the requested name from this module's namespace.
    mod = sys.modules[__name__]
    return object.__getattribute__(mod, name)


def __dir__() -> list[str]:
    """Include private and bootstrap symbols in dir() output."""
    names = sorted(globals().keys())
    if "_instance" not in names:
        names.append("_instance")
    return names

import sys  # noqa: E402 (loaded late to avoid circular imports)

