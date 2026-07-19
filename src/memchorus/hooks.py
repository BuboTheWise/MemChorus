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
            # Hermes passes "user_message" (str) and "conversation_history" (list[dict]) —
            # NOT "input_text" or "messages". Verified in turn_context.py:527-536.
            input_text = kwargs.get("user_message", "") or _build_search_text_from_history(
                kwargs.get("conversation_history")
            )
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
                    conversation_length=len(kwargs.get("conversation_history", [])),
                    tool_calls_this_turn=kwargs.get("tool_calls_this_turn", 0),
                    empty_tool_responses=kwargs.get("empty_tool_responses", 0),
                    recent_messages=list(
                        kwargs.get("conversation_history", [])[-5:]
                    ),
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
                # Hermes turn_context.py checks r.get("context") — not "injected_context".
                # Wrong key = silent injection skip.
                "context": "\n\n".join(injected_blocks),
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
            # Bug fix 2026-07-17: Hermes passes "result" not "tool_output".
            # See docs/integration-contract-spec.md for the verified contract.
            tool_output = kwargs.get("result")
            if not tool_output:
                return None

            output_str = str(tool_output)

            # Guard: skip query echo artifacts — recall query templates that
            # leaked through the tool pipeline and would pollute memory storage.
            from memchorus.auto_storage_engine import _is_query_echo
            if _is_query_echo(output_str):
                logger.debug("hooks: skipping query echo artifact in tool output")
                return None

            # BehavioralTrigger gate with length-based fallback: auto-save when
            # decision points are detected OR when the output exceeds a modest
            # size threshold regardless of behavioral markers. Uses 150 chars
            # as cutoff — enough to filter trivial noise ("OK", empty results)
            # while still capturing meaningful diagnostic/analysis output.
            skip_by_behavior = False
            if self._btrigger is not None:
                detected = self._btrigger.detect(output_str)
                if not detected and len(output_str) < 150:
                    logger.debug("hooks: no behavioral decision points in short tool output (%d chars) — skipping auto-save", len(output_str))
                    skip_by_behavior = True

            if skip_by_behavior:
                return None

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

def _build_search_text_from_history(history):
    """Build search text from conversation history (list[dict]).

    Extract the last few user/tool messages for search context when
    user_message is empty (e.g., system-generated turns).
    """
    if not history:
        return ""
    recent = [m.get("content", "") or ""
              for m in reversed(history)
              if m.get("role") != "assistant"
              ][-3:]
    return "\n".join(recent)


def _format_context_block(items: List[Dict[str, Any]]) -> str:
    """Turn orchestrator results into a Markdown-ready context block for agent consumption."""
    if not items:
        return ""

    MAX_CONTENT_CHARS = 300  # hard cap per-hit to prevent KB-scale recall bloat

    lines: List[str] = []
    seen_keys = set()  # quick de-dup helper
    for item in items[:5]:
        key = item.get("key") or str(item)
        if key in seen_keys:
            continue
        content_raw = str(item.get("content") or "")
        # Truncate oversized content to prevent recall from bloating context
        if len(content_raw) > MAX_CONTENT_CHARS:
            content_raw = content_raw[:MAX_CONTENT_CHARS] + "..."
        lines.append(f"- **{key}** — {content_raw}")

    joined = "\n".join(lines)
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
