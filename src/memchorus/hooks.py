"""
MemChorus lifecycle hooks for Hermes plugin integration.

This module provides the MemChorusHooks class that Hermes Gateway discovers via
setup.cfg entry_points and calls at key moments in the agent execution loop:
pre_llm_call, post_tool_call, on_session_start.

On import of memchorus package, global bootstrap fires if enabled.
These hooks wire into that bootstrap'd orchestrator instance to provide
automatic memory recall + feedback loop evaluation without requiring the
calling agent to do anything beyond `import memchorus`.

Environment control: set MEMCHORUS_AUTO_ENABLED=false to disable all hooks.
"""

import hashlib
import importlib  # for dynamic entry_point discovery
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy-global bootstrap helper
# ---------------------------------------------------------------------------

def _trigger_memchorus_bootstrap() -> None:
    """Force lazy-init bootstrap by accessing a symbol from the package's
    __getattr__ dispatch table.

    The package defines `_instance = None` as a module-level default so that
    ``from memchorus import _instance`` doesn't crash before bootstrap runs —
    but that also means simply reading ``memchorus._instance`` returns the stale
    default without ever calling ``__getattr__``. Accessing any lazy symbol
    (e.g. ``BehavioralTrigger``) *does* route through ``__getattr__``, which
    kicks off auto_bootstrap and overwrites sys.modules[memchorus]._instance
    with the real orchestrator before returning.

    Calling this once is cheap; subsequent accesses benefit from the internal
    _bootstrap_done guard inside __getattr__.
    """
    import sys
    mod = sys.modules.get("memchorus")
    if mod is not None and not getattr(mod, "_bootstrap_done", True):
        # Touch a lazy symbol to fire bootstrap (safe — already imported above)
        try:
            _ = mod.BehavioralTrigger  # noqa: F841
        except Exception:            # pragma: no cover - fallback is harmless
            pass


def _get_orchestrator() -> Optional[Any]:
    """Return the global MemoryOrchestrator singleton, ensuring bootstrap fires first."""
    try:
        _trigger_memchorus_bootstrap()
        return __import__('memchorus', fromlist=['_instance'])._instance
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.debug("_get_orchestrator failed (no auto_bootstrap yet): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Hook class — discovered by Hermes via entry_points["hermes.plugins.lifecycle"]
# ---------------------------------------------------------------------------

class MemChorusHooks:
    """Lifecycle hooks that fire at key decision points in the agent loop.

    Methods are called by Hermes Gateway at runtime:
      - on_pre_llm_call(context)   before every LLM API call
      - on_post_tool_call(tool_data)   after every tool execution
      - on_session_start(session_id)   once per new Hermes session start)

    Each method queries the global orchestrator and injects relevant context or
    saves newly significant outcomes automatically. BehavioralTrigger.detect() is
    used to shape recall/saving based on detected decision points.
    """

    def __init__(self) -> None:
        # Instantiate BehavioralTrigger for decision-point detection in hooks.
        # We import lazily here so the class remains usable even when
        # behavioral_trigger isn't available (graceful degradation).
        try:
            from memchorus.behavioral_trigger import BehavioralTrigger, DecisionPoint  # noqa: F401
            self._btrigger = BehavioralTrigger()
        except Exception as exc:
            logger.debug("BehavioralTrigger unavailable: %s — hooks will operate in fallback mode", exc)
            self._btrigger = None

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        logger.info("MemChorus on_pre_llm_call ENTRY — kwargs keys: %s", list(kwargs.keys())[:5])
        """Fire before an LLM call to auto-recall relevant memories + evaluate feedback loops.

        Returns a dict with injected context (if available) or None if disabled/empty.
        Both memory recall and feedback corrections travel through the same injection path
        as labelled blocks — soft nudges, never hard overrides.
        """
        orchestrator = _get_orchestrator()
        if orchestrator is None:
            return None

        try:
            # 1. Call auto-recall engine via orchestrator's search pipeline
            input_text = kwargs.get("input_text") or kwargs.get("messages", "")
            if not input_text:
                return None

            # Detect behavioral decision points to shape the search strategy.
            detected_points = []
            if self._btrigger is not None:
                input_str = str(input_text)[:4096]  # cap for performance
                detected_points = self._btrigger.detect(input_str)

            # Determine search limit based on decision point priority:
            # PLANNING_START / CONTEXTUAL_SYNTHESIS -> broader recall (limit=5)
            # TOOL_CALL_INTENT / ERROR_STATE -> focused recall (limit=3)
            # Default: limit=3
            search_limit = 3
            if detected_points:
                from memchorus.behavioral_trigger import DecisionPoint as _DP
                for dp in detected_points:
                    if dp.type in (_DP.PLANNING_START, _DP.CONTEXTUAL_SYNTHESIS_COMPLETION):
                        search_limit = 5
                        break

            # Use search() (not retrieve()) for pre-decision recall — retrieve(key)
            # only does exact-key lookup and doesn't accept a limit param.
            context_items = orchestrator.search(input_text, limit=search_limit)

            injected_blocks: List[str] = []

            if context_items:
                injected_blocks.append(
                    "[MemChorus Memory Recall]\n"
                    f"{_format_context_block(context_items)}\n"
                    "[/MemChorus Memory Recall]"
                )

            # 2. Evaluate feedback loop corrections (same injection path, separate label)
            try:
                from memchorus.feedback_loop.integration import (
                    TurnContext as FeedbackTurnContext,
                    TriggerEvent,
                    inject_feedback_corrections,
                )

                turn_ctx = FeedbackTurnContext(
                    user_message=str(input_text)[:1024],
                    conversation_length=kwargs.get("conversation_length", 0),
                    tool_calls_this_turn=kwargs.get("tool_calls_this_turn", 0),
                    empty_tool_responses=kwargs.get("empty_tool_responses", 0),
                    recent_messages=list(kwargs.get("recent_messages", [])),
                )

                feedback_text = inject_feedback_corrections(
                    turn_context=turn_ctx,
                    trigger_event=TriggerEvent.PRE_LLM_CALL,
                )

                if feedback_text:
                    injected_blocks.append(feedback_text)
            except Exception as fexc:  # graceful degradation for feedback loops
                logger.warning("Feedback loop evaluation skipped: %s", fexc)

            if not injected_blocks:
                return None

            result: Dict[str, Any] = {
                "source": "memchorus_pre_llm_call",
                "injected_context": "\n\n".join(injected_blocks),
            }
            return result

        except Exception as exc:  # pragma: no cover - graceful degradation
            logger.warning("on_pre_llm_call failed — returning None (hooks remain active). %s", exc)
            return None

    def on_post_tool_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        logger.info("MemChorus on_post_tool_call ENTRY — kwargs keys: %s", list(kwargs.keys())[:5])
        """Fire after tool execution to auto-capture significant outcomes.

        If the tool output contains important results, save them automatically
        for future recall without the agent needing to remember to store it later.

        Returns dict with storage confirmation or None if nothing captured.
        """
        orchestrator = _get_orchestrator()
        if orchestrator is None:
            return None

        try:
            tool_output = kwargs.get("tool_output")
            if not tool_output:
                return None

            output_str = str(tool_output)

            # Guard: skip query echo artifacts — recall query templates that
            # leaked through the tool pipeline and would pollute memory storage.
            from memchorus.auto_storage_engine import _is_query_echo
            if _is_query_echo(output_str):
                logger.debug("hooks: skipping query echo artifact in tool output")
                return None

            # BehavioralTrigger gate: only auto-save when decision points detected.
            # This prevents noise-flooding (Bug 4 fix) and makes the behavioral
            # significance detector actually functional.
            # Fallback: if output is substantial (>=200 chars with signal entropy),
            # save it regardless — structured tool results have no natural-language
            # decision-point cues but can still be meaningful.
            detected = []
            if self._btrigger is not None:
                detected = self._btrigger.detect(output_str)

            from memchorus.auto_storage_engine import _has_minimum_signal
            has_signal = _has_minimum_signal(output_str)

            if not detected and not has_signal:
                logger.debug(
                    "hooks: no behavioral decision points AND no signal entropy in tool output — skipping auto-save"
                )
                return None

            if detected:
                logger.info("hooks: BehavioralTrigger detected %d decision point(s)", len(detected))
            else:
                logger.debug("hooks: fallback — substantial signal content (%d chars, entropy OK) saved", len(output_str))

            # Derive a deterministic key from the tool output hash for smart placement.
            content_hash = hashlib.md5(output_str.encode()).hexdigest()[:16]
            auto_key = f"auto_tool_{content_hash}"
            saved = orchestrator.save(auto_key, output_str)
            if not saved:
                return None

            result: Dict[str, Any] = {
                "source": "memchorus_auto_storage",
                "saved_ids": [auto_key],
            }
            return result

        except Exception as exc:  # pragma: no cover - graceful degradation
            logger.warning("on_post_tool_call failed — returning None. %s", exc)
            return None

    def on_session_start(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Fire once when a new Hermes session begins (or task is picked up) to auto-orient the agent.

        Reads HERMES_KANBAN_TASK or WORKSPACE from env, queries relevant memories,
        and returns them so the session starts with project context already available.
        """
        orchestrator = _get_orchestrator()
        if orchestrator is None:
            return None

        try:
            # Try to load orientation engine (may not exist yet — optional import)
            orient_module = importlib.import_module("memchorus.orientation")
        except ImportError:
            # Orientation subsystem not installed yet — no big deal, just skip it)
            logger.debug("Orientation module not available — skipping auto-orientation.")
            return None

        try:
            kanban_task = os.environ.get("HERMES_KANBAN_TASK")

            # Delegate the full orientation sequence to the module — it handles
            # cache checks, project detection, query construction, and silent
            # degradation all at once.
            all_items = orient_module.orientation_search(
                env_task=kanban_task,
                orchestrator=orchestrator,
                limit=5,
                cache_ttl_seconds=getattr(orient_module, "DEFAULT_CACHE_TTL_SECONDS", 60.0),
            )

            if not all_items:
                return None  # empty → silent skip (AC-O3)

            result: Dict[str, Any] = {
                "source": "memchorus_auto_orientation",
                "project_context": _format_context_block(all_items),
            }
            return result

        except Exception as exc:  # pragma: no cover - graceful degradation
            logger.warning("on_session_start orientation failed. %s", exc)
            return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# GAP P0-4 FIX (2026-07-19): Enforce character budget per entry + total block
_MAX_CONTENT_CHARS = 300   # max chars per single memory entry  
_MAX_BLOCK_CHARS = 800     # hard ceiling — tightened from 2000 to prevent hook bloat (t_32e7877a)
_HERMES_MEMCHORUS_CHAR_LIMIT = None

# Per-profile override: reads config.yaml memchorus.hook_char_limit before global default
def _resolve_char_limit() -> int:
    """Return per-profile char budget if set, else global default."""
    if _HERMES_MEMCHORUS_CHAR_LIMIT is not None:
        return _HERMES_MEMCHORUS_CHAR_LIMIT
    try:
        profile = os.environ.get("HERMES_PROFILE", "default")
        cfg_path = str(_Path.home() / ".hermes" / "profiles" / profile / "config.yaml")
        p = _Path(cfg_path)
        if p.exists():
            data = _yml.safe_load(p.read_text()) or {}
            limit = data.get("memchorus", {}).get("hook_char_limit", None)
            if isinstance(limit, int):
                return max(200, min(limit, 10000))
    except Exception:
        pass
    return _MAX_BLOCK_CHARS

def _format_context_block(items: List[Dict[str, Any]]) -> str:
    """Turn orchestrator results into a Markdown-ready context block for agent consumption.
    
    Enforces character budget so huge auto-tool dumps don't destroy the prompt window.
    Truncated entries get '...' appended; excess items are silently dropped.
    """
    if not items:
        return ""

    lines: List[str] = []
    seen_keys = set()
    block_char_budget = _MAX_BLOCK_CHARS
    
    for item in items[:5]:
        key = item.get("key") or str(item)
        if key in seen_keys:
            continue
        raw_content = (item.get('content') or '').rstrip()

        # Per-entry budget enforcement
        if len(raw_content) > _MAX_CONTENT_CHARS:
            raw_content = raw_content[:_MAX_CONTENT_CHARS].rsplit(' ', 1)[0] + "..."  
        
        line = f"- **{key}** — {raw_content}"
        lines.append(line)
    
    joined = "\n".join(lines)
    
    # Hard total block ceiling (fallback safety net)
    if len(joined) > _MAX_BLOCK_CHARS:
        joined = joined[:_MAX_BLOCK_CHARS].rsplit('\n', 1)[0] + "\n... (truncated, budget exceeded)"
    
    return f"[MemChorus injected context]\n{joined}\n[/MemChorus injected block]"


# Plugin configuration loader — reads plugin.yaml save_triggers before bootstrap
import yaml as _yml  # Optional: skip if PyYAML not installed
from pathlib import Path as _Path

__PLUGIN_YAML_PATH = str(_Path.home() / ".hermes" / "plugins" / "hermes-memchorus" / "plugin.yaml")


def _load_plugin_config() -> dict:
    """Read plugin.yaml from the default Hermes plugin path. Returns {} on failure."""
    try:
        p = _Path(__PLUGIN_YAML_PATH)
        if not p.exists():
            return {}
        raw = p.read_text()
        cfg = _yml.safe_load(raw) or {}
        if not isinstance(cfg, dict):
            return {}
        return cfg
    except Exception:
        # PyYAML missing or file unreadable — caller decides if that's fatal
        return {}


# ---------------------------------------------------------------------------
# Hermes plugin entry point -- called by Hermes gateway at startup
# ---------------------------------------------------------------------------

_instance_holder: List[Any] = [None]  # mutable container for the registered instance


def register(ctx: Any) -> None:
    """Hermes plugin registration callback.

    Called by the Hermes gateway when the plugin is discovered via entry points
    or directory scanning. Registers lifecycle hook callbacks with PluginContext.
    """
    # Merge user-provided save_triggers BEFORE any BehavioralTrigger instance exists
    plugin_cfg = _load_plugin_config()
    user_triggers = plugin_cfg.get("save_triggers", [])  # type: ignore[union-attr]
    if user_triggers and hasattr(ctx, 'plugin_config'):
        try:
            from memchorus.behavioral_trigger import configure_save_triggers  # type: ignore[attr-defined]
            configure_save_triggers(user_triggers)
        except Exception as exc:
            logger.warning("Failed to apply save_triggers: %s", exc)

    # Trigger lazy bootstrap of orchestrator singleton BEFORE registering hooks.
    # This ensures _instance exists when hooks fire — without it every hook
    # silently returns None (see t_a0d7e8c8). Bubo hit this lazily via MCP tool
    # access, but Cthugha never touches memchorus directly so bootstrap was
    # deferred forever.
    __import__('memchorus', fromlist=['_trigger_lazy_bootstrap'])._trigger_lazy_bootstrap()

    hooks = MemChorusHooks()
    _instance_holder[0] = hooks  # keep a reference so GC doesn't collect

    # Register all three lifecycle hooks
    ctx.register_hook("pre_llm_call", hooks.on_pre_llm_call)
    ctx.register_hook("post_tool_call", hooks.on_post_tool_call)
    ctx.register_hook("on_session_start", hooks.on_session_start)

    logger.info("MemChorus v%s registered hooks: pre_llm_call, post_tool_call, on_session_start",
                __import__('memchorus').__version__)
