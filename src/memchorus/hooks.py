"""
MemChorus lifecycle hooks for Hermes plugin integration.

This module provides the MemChorusHooks class that Hermes Gateway discovers via
setup.py entry_points ("hermes_agent.plugins" group) and calls at key moments
pre_llm_call, post_tool_call, on_session_start.

On import of memchorus package, global bootstrap fires if enabled.
These hooks wire into that bootstrap'd orchestrator instance to provide
automatic memory recall + feedback loop evaluation without requiring the
calling agent to do anything beyond `import memchorus`.

Environment control: set MEMCHORUS_AUTO_ENABLED=false to disable all hooks.
"""

import hashlib
import importlib  # for dynamic entry_point discovery
import json
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
            # Use _build_search_terms() for progressive fallback — even when
            # user_message and conversation_history are empty, we can still
            # recall from task context (task_id, model, platform, session_id).
            input_text = _build_search_terms(kwargs)
            if not input_text:
                return None

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
            # Hermes model_tools._emit_post_tool_call_hook() passes result=
            tool_output = kwargs.get("result")
            if not tool_output:
                return None

            # Convert structured outputs to readable text.
            # dict/list results should become JSON (not Python repr via str())
            # so downstream significance detection, entropy checks, and recall
            # see clean data instead of garbled "{'k': 'v'}" strings.
            if isinstance(tool_output, (dict, list)):
                output_str = json.dumps(tool_output)
            else:
                output_str = str(tool_output)

            # Guard: skip query echo artifacts — recall query templates that
            # leaked through the tool pipeline and would pollute memory storage.
            from memchorus.auto_storage_engine import _is_query_echo
            if _is_query_echo(output_str):
                logger.debug("hooks: skipping query echo artifact in tool output")
                return None

            # Guard: skip placeholder artifacts — synthetic filler text like
            # "session context t_17cfe174 current task" that upstream injects
            # when no real tool_output is available (MC-004).
            from memchorus.auto_storage_engine import _is_placeholder_artifact
            if _is_placeholder_artifact(output_str):
                logger.debug("hooks: skipping placeholder artifact in tool output")
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

            # Route through AutoStorageEngine's capture_outcome pipeline for
            # proper significance detection, scoring, provenance markers, and
            # dedup — not a bare orchestrator.save().
            auto_storage = None
            try:
                from memchorus.auto_storage_engine import AutoStorageEngine
                if auto_storage is None:
                    auto_storage = AutoStorageEngine(orchestrator=orchestrator)
                storage_result = auto_storage.capture_outcome(output_str, outcome_type="automatic")
                if not storage_result.get("saved"):
                    reason = storage_result.get("reason", "unknown")
                    logger.debug("hooks: capture_outcome rejected: %s", reason)
                    return None

                result: Dict[str, Any] = {
                    "source": "memchorus_auto_storage",
                    "saved_ids": [storage_result.get("key", "")],
                    "significance": storage_result.get("significance", ""),
                    "importance_score": storage_result.get("importance_score", 0.0),
                }
                logger.info("hooks: auto-saved content (%s, importance %.2f)", result["significance"], result["importance_score"])
                return result
            except Exception as sexc:
                logger.warning("hooks: capture_outcome/Engine failed — falling back to direct save. %s", sexc)

            # Fallback to direct orchestrator.save if engine fails
            content_hash = hashlib.md5(output_str.encode()).hexdigest()[:16]
            auto_key = f"result_{content_hash}"
            payload = {
                "text": output_str,
                "categories": ["AUTO", "RESULT"],
                "outcome_type": "automatic",
                "importance_score": 0.0,
                "_auto_provenance": True,
            }
            saved = orchestrator.save(auto_key, payload)
            if not saved:
                return None

            result = {
                "source": "memchorus_auto_storage_fallback",
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

def _extract_text_from_message(message: Any) -> str:
    """Extract text content from a Message dict/object for search purposes."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return message.get("content", "") or message.get("text", "") or ""
    # Fallback for object with .content or .text attributes
    try:
        content = getattr(message, "content", None) or getattr(message, "text", None)
        if isinstance(content, str):
            return content
    except Exception:
        pass
    return ""


def _build_search_terms(kwargs: Dict[str, Any]) -> str:
    """Build search query from kwargs with progressive fallbacks.

    Even when user_message and conversation_history are empty/missing,
    we can construct a meaningful recall query from task context, model,
    platform, etc. This prevents pre-LLM recall from silently returning
    None simply because the primary text sources are falsy.

    Priority chain:
        1. user_message (primary input)
        2. conversation_history (last messages as fallback)
        3. task_id / model / platform context for project-aligned recall
        4. Empty string — caller should return None to skip
    """
    # Primary: user message
    user_msg = kwargs.get("user_message")
    if isinstance(user_msg, str) and user_msg.strip():
        return user_msg
    if isinstance(user_msg, dict):
        text = _extract_text_from_message(user_msg)
        if text:
            return text
    # Catch-all for object-style messages with .content / .text attributes
    if not isinstance(user_msg, (str, dict, type(None))):
        text = _extract_text_from_message(user_msg)
        if text:
            return text

    # Fallback 1: conversation history (use last message content)
    history = kwargs.get("conversation_history") or []
    if isinstance(history, list) and history:
        messages = [_extract_text_from_message(m) for m in history]
        available = [m for m in messages if m]
        if available:
            return " ".join(available)[-4096:]  # cap length

    # Fallback 2: Build context-based query from metadata
    parts: List[str] = []
    task_id = kwargs.get("task_id")
    if task_id:
        parts.append(str(task_id))
    model = kwargs.get("model")
    if model:
        parts.append(str(model))
    platform = kwargs.get("platform")
    if platform and str(platform).strip():
        parts.append(str(platform))
    session_id = kwargs.get("session_id")
    if session_id:
        parts.append(str(session_id)[:16])

    return " ".join(parts)


# Per-profile override: reads config.yaml memchorus.hook_char_limit before global default
def _resolve_char_limit() -> int:
    """Return per-profile char budget if set, else global default."""
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

def _has_feedback_priority(item: Dict[str, Any]) -> bool:
    """Check if an item carries feedback-correction priority.

    Feedback corrections are more actionable than raw recall and should be
    preserved first when the context budget is tight.
    """
    key = str(item.get("key") or "").lower()
    return ("feedback" in key or "correction" in key or
            item.get("_is_feedback", False))


def _format_context_block(items: List[Dict[str, Any]]) -> str:
    """Turn orchestrator results into a Markdown-ready context block for agent consumption.

    Enforces character budget so huge auto-tool dumps don't destroy the prompt window.
    Truncation respects line boundaries — partial lines are dropped rather than cut,
    ensuring markdown formatting stays intact. When budget is tight, feedback-correction
    items take priority over raw recall results (higher-priority items kept last).
    """
    if not items:
        return ""

    # --- Priority sort: feedback corrections before raw recall ---------------
    # So that when the block ceiling forces item removal (below), lower-value
    # recall entries are dropped first.
    priority_items = [i for i in items if _has_feedback_priority(i)]
    normal_items   = [i for i in items if not _has_feedback_priority(i)]
    ordered        = priority_items + normal_items

    lines: List[str] = []
    seen_keys: set = set()

    for item in ordered[:5]:
        key = item.get("key") or str(item)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        content_raw = item.get("content") or ""
        # Defensive: some memory sources return nested dicts instead of strings
        if not isinstance(content_raw, str):
            content_raw = str(content_raw)
        raw_content = content_raw.rstrip()

        # --- Per-entry budget enforcement (line-boundary aware) --------------
        if len(raw_content) > _MAX_CONTENT_CHARS:
            content_lines = raw_content.split("\n")
            if len(content_lines) == 1:
                # Single-line content — safe to cut at any point
                raw_content = raw_content[:_MAX_CONTENT_CHARS] + "..."
            else:
                # Multi-line — only keep complete lines up to budget
                kept: List[str] = []
                running = 0
                for line in content_lines:
                    if running + len(line) + 1 > _MAX_CONTENT_CHARS:
                        break
                    kept.append(line)
                    running += len(line) + 1
                if kept:
                    raw_content = "\n".join(kept) + "..."
                else:
                    # First line alone exceeds budget — partial cut as fallback
                    raw_content = content_lines[0][:_MAX_CONTENT_CHARS] + "..."

        line = f"- **{key}** — {raw_content}"
        lines.append(line)

    joined = "\n".join(lines)
    truncated = False

    # --- Hard total block ceiling (drops complete entries, not partial lines)-
    # Constants that appear in every output regardless of truncation
    _header_len  = len("[MemChorus injected context]\n")
    _footer_len  = len("\n[/MemChorus injected block]")

    while len(joined) + _header_len + _footer_len > _MAX_BLOCK_CHARS:
        if not lines:
            break
        lines.pop()
        joined = "\n".join(lines)
        truncated = True

    # Append trailer only if we actually dropped entries
    if truncated:
        joined += "\n... (truncated, budget exceeded)"

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
