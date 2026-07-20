"""Memory Orchestration Package — lazy-loaded submodules."""

__version__ = "1.5.05"
__author__ = "BuboTheWise"
__email__ = "bubo@nous.systems"

import sys  # loaded early for __getattr__ / sys.modules access

# Lazy bootstrap guard (set to True by __getattr__ after first trigger).
_bootstrap_done: bool = False

# _instance as a real module-level default so `from memchorus import _instance`
# does NOT raise ImportError before bootstrap runs. After bootstrap completes,
# sys.modules[__name__]._instance is overwritten with the actual orchestrator.
_instance = None  # type: ignore[type-arg]

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
    # Lifecycle management — Phase 1 (§6.2 / §8)
    "AuditLogger": ("memchorus.lifecycle_manager", "AuditLogger"),
    "LifecycleManager": ("memchorus.lifecycle_manager", "LifecycleManager"),
    "SweepScheduler": ("memchorus.lifecycle_manager", "SweepScheduler"),
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
    # Lifecycle management
    "AuditLogger",
    "LifecycleManager",
    "SweepScheduler",
]


def _trigger_lazy_bootstrap():
    """Execute auto-bootstrap once (threading-safe within this module)."""
    global _bootstrap_done  # noqa: PLW0603
    if _bootstrap_done:
        return
    from memchorus.auto_bootstrap import _bootstrap as _orig_bootstrap
    result = _orig_bootstrap()
    # AC-1 compliance (Bug 1): If bootstrap returned None due to genuine failure,
    # fall back to a minimal working orchestrator so hooks still have a reference.
    # Do NOT override user intent — if auto-bootstrap was intentionally disabled,
    # leave result as None so hooks degrade gracefully and skip their work.
    _auto_enabled_raw = os.environ.get("MEMCHORUS_AUTO_ENABLED")
    _is_explicitly_disabled = _resolve_boolean(_auto_enabled_raw) is False \
        if _auto_enabled_raw is not None else False
    if result is None and not _is_explicitly_disabled:
        import logging
        _fb_logger = logging.getLogger(__name__)
        try:
            from memchorus.orchestrator import MemoryOrchestrator
            _fb_logger.warning(
                "%s bootstrap returned None — falling back to degraded orchestrator",
                __name__,
            )
            result = MemoryOrchestrator()
        except Exception as exc:
            _fb_logger.error("Degraded fallback failed: %s", exc)
    sys.modules[__name__]._instance = result  # type: ignore[attr-defined]
    _bootstrap_done = True


_import_os = __import__("os")
os = _import_os


def _resolve_boolean(raw):
    """Normalise any truthy / falsy source to a strict Python boolean."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(raw)


def __getattr__(name: str) -> object:
    """Lazy-init descriptor — fires bootstrap on first attribute access only.

    Pure ``import memchorus`` alone does **not** trigger bootstrap or load heavy
    dependencies (AC-A1 through AC-A4). Bootstrap fires only when the caller
    actually accesses a symbol from this module.
    """
    global _bootstrap_done  # noqa: PLW0603

    # Step 1 — run bootstrap exactly once before any resolution
    if not _bootstrap_done:
        from memchorus.auto_bootstrap import _bootstrap as _orig_bootstrap
        result = _orig_bootstrap()
        # AC-1 compliance (Bug 1): _instance must be non-None after register(ctx) completes.
        # If full bootstrap genuinely failed, fall back to a degraded orchestrator
        # with default config so hooks still operate rather than returning None silently.
        # Do NOT override user intent — if auto-bootstrap was intentionally disabled via
        # MEMCHORUS_AUTO_ENABLED=false, leave result as None so hooks skip their work.
        _was_disabled = os.environ.get("MEMCHORUS_AUTO_ENABLED") is not None
        if _was_disabled:
            val = os.environ["MEMCHORUS_AUTO_ENABLED"].strip().lower()
            _was_disabled = val in ("false", "0", "no", "off", "")
        if result is None and not _was_disabled:
            import logging
            _fallback_logger = logging.getLogger(__name__)
            try:
                from memchorus.orchestrator import MemoryOrchestrator
                _fallback_logger.warning(
                    "%s bootstrap returned None — creating degraded fallback orchestrator "
                    "(enforcement disabled; storage still possible via hermes_default)",
                    __name__,
                )
                result = MemoryOrchestrator()  # defaults: empty sources → hooks return None gracefully but _instance exists
            except Exception as exc:
                _fallback_logger.error(
                    "Degraded fallback orchestrator creation failed: %s — memchorus will be inactive", exc
                )
                pass  # _instance remains unset so __getattr__ raises AttributeError on access
        sys.modules[__name__]._instance = result  # type: ignore[attr-defined]
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
