"""Memory Orchestration Package — lazy-loaded submodules."""

__version__ = "1.2.0"
__author__ = "BuboTheWise"
__email__ = "bubo@nous.systems"

import sys  # loaded early for __getattr__ / sys.modules access

# Lazy bootstrap guard (set to True by __getattr__ after first trigger).
_bootstrap_done: bool = False

# Cache for lazily-loaded symbols so subsequent attribute hits are instant.
_attr_cache: dict[str, object] = {}

# Symbol -> (submodule dotpath, symbol name) mapping.
_LAZY_SYMBOLS: dict[str, tuple[str, str]] = {
    "MemorySource": ("memchorus.memory_source", "MemorySource"),
    "HermesDefaultMemorySource": ("memchorus.hermes_memory_source", "HermesDefaultMemorySource"),
    "MemPalaceMemorySource": ("memchorus.mempalace_memory_source", "MemPalaceMemorySource"),
    "MemoryOrchestrator": ("memchorus.orchestrator", "MemoryOrchestrator"),
    "BehavioralTrigger": ("memchorus.behavioral_trigger", "BehavioralTrigger"),
    "AutoRecallEngine": ("memchorus.auto_recall_engine", "AutoRecallEngine"),
    "AutoStorageEngine": ("memchorus.auto_storage_engine", "AutoStorageEngine"),
    "BehavioralEnforcementManager": ("memchorus.enforcement_manager", "BehavioralEnforcementManager"),
    # Feedback loop detection + escalation v1.1.03
    "ConditionSignal": ("memchorus.feedback_loop.schema_v1", "ConditionSignal"),
    "FeedbackLoopDefinition": ("memchorus.feedback_loop.schema_v1", "FeedbackLoopDefinition"),
    "SUPPORTED_VERSIONS": ("memchorus.feedback_loop.schema_v1", "SUPPORTED_VERSIONS"),
    "TriggerEvent": ("memchorus.feedback_loop.schema_v1", "TriggerEvent"),
    "validate_schema_v1": ("memchorus.feedback_loop.schema_v1", "validate_schema_v1"),
    "load_feedback_loops": ("memchorus.feedback_loop.loader", "load_feedback_loops"),
    "LoadSummary": ("memchorus.feedback_loop.loader", "LoadSummary"),
    "FeedbackLoopDetector": ("memchorus.feedback_loop.detector", "FeedbackLoopDetector"),
}

__all__ = [
    "MemorySource",
    "HermesDefaultMemorySource",
    "MemPalaceMemorySource",
    "MemoryOrchestrator",
    "BehavioralTrigger",
    "AutoRecallEngine",
    "AutoStorageEngine",
    "BehavioralEnforcementManager",
    "ConditionSignal",
    "FeedbackLoopDefinition",
    "SUPPORTED_VERSIONS",
    "TriggerEvent",
    "validate_schema_v1",
    "load_feedback_loops",
    "LoadSummary",
    "FeedbackLoopDetector",
]


def _trigger_lazy_bootstrap():
    """Execute auto-bootstrap once (threading-safe within this module)."""
    global _bootstrap_done  # noqa: PLW0603
    if _bootstrap_done:
        return
    from memchorus.auto_bootstrap import _bootstrap as _orig_bootstrap
    result = _orig_bootstrap()
    sys.modules[__name__]._instance = result  # type: ignore[attr-defined]
    _bootstrap_done = True


def __getattr__(name: str) -> object:
    """Lazy-init descriptor — fires bootstrap on first attribute access only.

    Pure ``import memchorus`` alone does **not** trigger bootstrap or load heavy
    dependencies (AC-A1 through AC-A4). Bootstrap fires only when the caller
    actually accesses a symbol from this module.
    """
    global _bootstrap_done  # noqa: PLW0603

    # Handle _instance specially: before bootstrap it genuinely does not exist,
# so accessing it should raise AttributeError rather than triggering bootstrap.
# After bootstrap, it lives on the module namespace and __getattribute__ finds it.
    if name == "_instance":
        mod = sys.modules[__name__]
        try:
            return object.__getattribute__(mod, name)
        except AttributeError:
            raise AttributeError(f"module 'memchorus' has no attribute '{name}' "
                                 f"(bootstrap not yet triggered)")

    # Step 1 — run bootstrap exactly once before any resolution
    if not _bootstrap_done:
        from memchorus.auto_bootstrap import _bootstrap as _orig_bootstrap
        result = _orig_bootstrap()
        sys.modules[__name__]._instance = result  # type: ignore[attr-defined]  # can be None when disabled or errored
        _bootstrap_done = True

    # Step 2 — resolve the requested name from lazy table or module globals
    if name in _LAZY_SYMBOLS:
        submod_path, sym = _LAZY_SYMBOLS[name]
        if name not in _attr_cache:
            import importlib
            submod = importlib.import_module(submod_path)
            _attr_cache[name] = getattr(submod, sym)
        return _attr_cache[name]

    # Fallback — standard module attribute lookup (for e.g. __name__, __version__)
    mod = sys.modules[__name__]
    return object.__getattribute__(mod, name)


def __dir__() -> list[str]:
    """Include lazy-loaded symbols in dir() output."""
    names = sorted(globals().keys())
    for sym in _LAZY_SYMBOLS:
        if sym not in names:
            names.append(sym)
    if "_instance" not in names:
        names.append("_instance")
    return names
