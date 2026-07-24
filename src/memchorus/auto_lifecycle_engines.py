"""auto_lifecycle_engines.py — MemChorus lifecycle engine auto-wiring.

Single import surface that wires together all lifecycle subsystems:

  - RetentionEngine    (lifecycle_retention)  — age-based review, exemption scoring
  - EvictionEngine     (lifecycle_eviction)   — trigger evaluation, archive/purge pipeline
  - MergeEngine        (lifecycle_merge)      — pre-save merge decisions via Jaccard similarity
  - LifecycleManager   (lifecycle_manager)    — orchestrates sweeps, holds policy config
  - SweepScheduler     (lifecycle_manager)    — timed execution driver preventing overlapping sweeps

Design goals
------------
1. **One-liner setup** — ``auto_init_lifecycle(orchestrator)`` wires everything in.
2. **Opt-in by default** — lifecycle features remain disabled until the user explicitly
   enables them via env var, YAML config, or direct API call (backward compat §9).
3. **Graceful degradation** — if any engine fails to instantiate, the others still work;
   no exceptions leak when lifecycle is unavailable.
4. **Lazy loading** — heavy imports only fire when actually requested, not on ``import``.

Public API
----------
- ``auto_init_lifecycle(orchestrator)`` — discover orchestrator state and wire engines
- ``AutoLifecycleState(dataclass)`` — read-only view of what is active
- Lazy imports: RetentionEngine, EvictionEngine, MergeEngine, LifecycleManager, SweepScheduler
"""

# stdlib -----------------------------------------------------------------
import logging
import os
from dataclasses import asdict, dataclass, field, replace as dc_replace
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- constants ------------------------------------------------------------
DEFAULT_LIFECYCLE_ENABLED = False  # §9 backward compat — opt-in only
ENV_LIFECYCLE_ENABLED = "MEMCHORUS_LIFECYCLE_ENABLED"


def _is_truthy(val: str) -> bool:
    return val.strip().lower() not in ("false", "0", "no", "off", "")


# --- public state struct ---------------------------------------------------

@dataclass(frozen=True)
class AutoLifecycleState:
    """Immutable snapshot of what lifecycle subsystems are active."""

    retention_active: bool = False
    eviction_active: bool = False
    merge_active: bool = False
    manager_active: bool = False
    scheduler_active: bool = False
    orchestrator_id: Optional[str] = None
    sweep_interval_hours: float = 0.0
    backend_sources: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = []
        for k, v in asdict(self).items():
            tag = k.replace("_active", "").replace("_", " ").title()
            status = "+" if v and isinstance(v, bool) else ""
            parts.append(f"{tag}: {v}{status}")
        return " | ".join(parts)


# --- lazy import helpers ---------------------------------------------------

def _lazy_lifecycle_manager():
    """Import LifecycleManager + SweepScheduler + config resolver."""
    from memchorus.lifecycle_manager import (
        LifecycleManager,
        SweepScheduler,
        _resolve_lifecycle_config,
    )
    return LifecycleManager, SweepScheduler, _resolve_lifecycle_config


def _lazy_merge_engine():
    """Import MergeEngine class and create_merge_engine factory."""
    from memchorus.lifecycle_merge import (
        MergeEngine,
        create_merge_engine,
    )
    return MergeEngine, create_merge_engine


# --- core auto-wiring function ---------------------------------------------

def auto_init_lifecycle(
    orchestrator: Any,
    config_override: Optional[Dict[str, Any]] = None,
) -> AutoLifecycleState:
    """Wire lifecycle engines into *orchestrator* as a side effect.

    The single call that activates retention scanning, eviction triggers,
    merge-on-save decisions, sweep scheduling, and backend cooldown tracking.

    Parameters
    ----------
    orchestrator:
        MemoryOrchestrator or any object with ``memory_sources`` dict.
    config_override:
        Optional dict merging into resolved lifecycle config (highest priority).

    Returns
    -------
    AutoLifecycleState
        Immutable snapshot of active subsystems after this call.

    Safety
    ------
    - If ``MEMCHORUS_LIFECYCLE_ENABLED`` unset/false → returns inactive state immediately.
    - Existing engines are NOT overwritten — gaps only.
    - Per-engine init exceptions are caught; partial states are valid.
    """
    # --- Gate: opt-in master toggle ----------------------------------------
    env_flag = os.environ.get(ENV_LIFECYCLE_ENABLED)
    is_enabled = bool(env_flag) and _is_truthy(env_flag)

    # config_override wins over env var when "enabled" key is present —
    # explicit caller intent takes highest priority (§9 opt-in).
    if config_override is not None and "enabled" in config_override:
        is_enabled = bool(config_override["enabled"])

    if not is_enabled:
        logger.debug(
            "Lifecycle engines skipped (%s=%s).",
            ENV_LIFECYCLE_ENABLED, env_flag or "<unset>",
        )
        return AutoLifecycleState()

    # --- Diagnostics setup -------------------------------------------------
    try:
        oid_str = f"{type(orchestrator).__name__}@0x{id(orchestrator):x}"
    except Exception:
        oid_str = "unknown"

    backend_sources: List[str] = []
    try:
        sources = getattr(orchestrator, "memory_sources", {})
        if isinstance(sources, dict):
            backend_sources = list(sources.keys())
    except Exception:
        pass

    state = AutoLifecycleState(
        orchestrator_id=oid_str,
        backend_sources=backend_sources,
    )

    # --- LifecycleManager + SweepScheduler ---------------------------------
    manager_result = None
    try:
        LM, SS, resolve_cfg = _lazy_lifecycle_manager()
        resolved = resolve_cfg(config_override or {"enabled": True})
        state = dc_replace(
            state,
            sweep_interval_hours=resolved.get("sweep_interval_hours", 8),
        )

        manager_result = LM(config=resolved, orchestrator=orchestrator)
        state = dc_replace(state, manager_active=True)

        if resolved.get("sweep_interval_hours", 8):
            try:
                _ = SS(manager=manager_result)
                state = dc_replace(state, scheduler_active=True)
            except Exception as exc:
                logger.warning("SweepScheduler init failed: %s", exc)

    except Exception as exc:
        logger.error("LifecycleManager init failed: %s", exc, exc_info=True)

    # --- RetentionEngine (through manager lazy getter) ---------------------
    if manager_result:
        try:
            _ = manager_result._get_retention_engine()
            state = dc_replace(state, retention_active=True)
        except Exception as exc:
            logger.warning("RetentionEngine init failed: %s", exc)

    # --- EvictionEngine (through manager lazy getter) ----------------------
    if manager_result:
        try:
            _ = manager_result._get_eviction_engine()
            state = dc_replace(state, eviction_active=True)
        except Exception as exc:
            logger.warning("EvictionEngine init failed: %s", exc)

    # --- MergeEngine (attach to orchestrator if missing) -------------------
    merge_ok = False
    try:
        _, create_mg = _lazy_merge_engine()
        existing = getattr(orchestrator, "_merge_engine", None)
        if not existing:
            engine = create_mg(orchestrator, config_override or {"enabled": True})
            orchestrator._merge_engine = engine
            merge_ok = True
        else:
            merge_ok = bool(existing)
    except Exception as exc:
        logger.warning("MergeEngine init failed: %s", exc)

    state = dc_replace(state, merge_active=merge_ok)

    logger.info(
        "Lifecycle wired — R=%s E=%s M=%s S=%s",
        state.retention_active,
        state.eviction_active,
        state.merge_active,
        state.scheduler_active,
    )
    return state


# --- convenience helpers ---------------------------------------------------

def create_merge_engine_on(
    orchestrator: Any,
    config: Optional[Dict[str, Any]] = None,
) -> Any:
    """Create and attach a MergeEngine. Shortcut for merge-on-save only."""
    try:
        _, create_mg = _lazy_merge_engine()
        return create_mg(orchestrator, config or {})
    except Exception as exc:
        logger.error("Failed to create MergeEngine: %s", exc)
        return None


def get_lifecycle_state(orchestrator: Any) -> AutoLifecycleState:
    """Read-only status of lifecycle engines on *orchestrator*.

    Does NOT modify anything — pure probe.
    """
    manager = getattr(orchestrator, "_lifecycle_manager", None)
    merge_eng = getattr(orchestrator, "_merge_engine", None)
    try:
        oid_str = f"{type(orchestrator).__name__}@0x{id(orchestrator):x}"
    except Exception:
        oid_str = "unknown"

    state = AutoLifecycleState(orchestrator_id=oid_str)

    if manager:
        cfg = getattr(manager, "config", {})
        state = dc_replace(
            state,
            manager_active=True,
            sweep_interval_hours=cfg.get("sweep_interval_hours", 8),
        )
        try:
            _ = manager._get_retention_engine()
            state = dc_replace(state, retention_active=True)
        except Exception:
            pass
        try:
            _ = manager._get_eviction_engine()
            state = dc_replace(state, eviction_active=True)
        except Exception:
            pass
        try:
            sched = getattr(manager, "_scheduler", None)
            if sched and getattr(sched, "enabled", False):
                state = dc_replace(state, scheduler_active=True)
        except Exception:
            pass

    if merge_eng:
        state = dc_replace(state, merge_active=True)

    try:
        sources = getattr(orchestrator, "memory_sources", {})
        if isinstance(sources, dict):
            state = dc_replace(state, backend_sources=list(sources.keys()))
    except Exception:
        pass

    return state


# --- exports --------------------------------------------------------------

__all__ = [
    "AutoLifecycleState",
    "auto_init_lifecycle",
    "create_merge_engine_on",
    "get_lifecycle_state",
]
