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

import importlib  # for dynamic entry_point discovery
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy-global bootstrap helper
# ---------------------------------------------------------------------------

def _get_orchestrator() -> Optional[Any]:
    """Return the global MemoryOrchestrator singleton if it was created by
    auto_bootstrap (lazy init on first memchorus import after that module loads)."""
    try:
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
    saves newly significant outcomes automatically.
    """

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Fire before an LLM call to auto-recall relevant memories + evaluate feedback loops.

        Returns a dict with injected context (if available) or None if disabled/empty.
        """
        orchestrator = _get_orchestrator()
        if orchestrator is None:
            return None

        try:
            # 1. Call auto-recall engine via orchestrator's search pipeline
            input_text = kwargs.get("input_text") or kwargs.get("messages", "")
            if not input_text:
                return None

            # Ask orchestrator to retrieve at decision point (pre-decision recall)
            context_items = orchestrator.retrieve(input_text, limit=3)

            if not context_items:
                return None

            result: Dict[str, Any] = {
                "source": "memchorus_auto_recall",
                "injected_context": _format_context_block(context_items),
            }
            return result

        except Exception as exc:  # pragma: no cover - graceful degradation
            logger.warning("on_pre_llm_call failed — returning None (hooks remain active). %s", exc)
            return None

    def on_post_tool_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
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

            # Ask orchestrator to smart-place this output based on content profile
            saved_ids = orchestrator.save_auto(tool_output)
            if not saved_ids:
                return None

            result: Dict[str, Any] = {
                "source": "memchorus_auto_storage",
                "saved_ids": list(saved_ids),
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
            project = os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_WORKSPACE")
            if not project:
                return None

            orientation_queries = orient_module._build_orientation_query()
            if not orientation_queries:
                return None

            all_items: List[Dict[str, Any]] = []
            for qdef in orientation_queries:
                try:
                    items = orchestrator.orientation_search(qdef)
                    if items:
                        all_items.extend(items)
                except Exception as exc:  # pragma: no cover - per-query degradation
                    logger.warning("Orientation query failed — skipping this one. %s", exc)

            if not all_items:
                return None

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

def _format_context_block(items: List[Dict[str, Any]]) -> str:
    """Turn orchestrator results into a Markdown-ready context block for agent consumption."""
    if not items:
        return ""

    lines: List[str] = []
    seen_keys = set()  # quick de-dup helper
    for item in items[:5]:
        key = item.get("key") or str(item)
        if key in seen_keys:
            continue
        lines.append(f"- **{key}** — {item.get('content') or ''}")

    joined = "\n".join(lines)
    return f"[MemChorus injected context]\n{joined}\n[/MemChorus injected block]"
