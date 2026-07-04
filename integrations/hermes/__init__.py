"""MemChorus <-> Hermes Agent integration plugin.

Registers lifecycle hooks into the Hermes agent loop so that MemChorus
pre-decision recall and autonomous storage capture fire at the right
moments without any manual agent intervention:

- **pre_llm_call**: Fires before every API call. Runs pre-decision recall
  via ``BehavioralEnforcementManager.enforce()``, then injects relevant
  memory into the turn context so the model sees it before reasoning begins.
  Also evaluates feedback loop corrections from custom YAML definitions.

- **post_tool_call**: Fires after each tool execution. Captures meaningful
  outcomes for autonomous storage via the enforcement pipeline.

Feedback loop extension: if ~/.hermes/custom_loops/ contains valid loop
definitions, they are loaded once on first fire and evaluated alongside
memory recall. Corrections merge into the same ``context`` string --
soft nudges, never hard overrides. Malformed YAML entries log warnings
and are silently skipped.

This is a standalone plugin -- install by placing this directory under the
Hermes plugins path (e.g. ~/.hermes/plugins/memchorus-integration/) or
by enabling ``plugins.enabled`` in config.yaml when installed system-wide.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Feedback loop integration -- injected alongside memory recall in pre_llm_call.
# Lazy import to avoid hard failures if the feedback_loop module is absent.
_feedback_mod_available = False


# ---------------------------------------------------------------------------
# Lazy bootstrap: only import MemChorus on first hook fire so a missing
# install doesn't break Hermes startup at all.
# ---------------------------------------------------------------------------

_orchestrator: "Optional[Any]" = None  # MemoryOrchestrator instance (singleton)
_init_done: bool = False


def _bootstrap() -> Optional["MemoryOrchestrator"]:  # noqa: F821
    """Import and initialise the orchestrator exactly once."""
    global _orchestrator, _init_done

    if _init_done:
        return _orchestrator

    try:
        from memchorus.orchestrator import MemoryOrchestrator  # noqa: F811
        from memchorus.mempalace_memory_source import MemPalaceMemorySource  # noqa: F811
        from memchorus.hermes_memory_source import HermesDefaultMemorySource  # noqa: F811
    except ImportError as exc:
        logger.warning("MemChorus lazy bootstrap failed (package not installed?): %s", exc)
        return None
    except Exception as exc:
        logger.warning("MemChorus eager init error (non-fatal): %s", exc)
        _init_done = True  # prevent infinite retry loops
        return None

    try:
        # Config dict -- not keyword args! The orchestrator __init__ already
        # registers 'hermes_default' + 'mempalace' sources, so we don't
        # duplicate that work here.
        config = {
            "enforce_on_read": True,
            "enforce_on_write": True,
            "half_life_days": 30.0,
        }
        orch = MemoryOrchestrator(config=config)

        sources = list(orch.memory_sources.keys())
        logger.info("MemChorus hooked: orchestrator ready with sources %s", sources)

        # Degrade gracefully if no available sources rather than breaking.
        if not orch.is_available():
            logger.warning(
                "MemChorus orchestrator has no available sources."
                " Hooks will fire but recall/storage is a no-op until"
                " MemPalace or another source is connected."
            )

        _orchestrator = orch
        _init_done = True
        return orch

    except Exception as exc:
        logger.error("MemChorus bootstrap failed (non-fatal): %s", exc)
        _init_done = True  # prevent infinite retries on broken config
        return None


# ---------------------------------------------------------------------------
# Feedback loop integration -- loads once on demand via lazy import
# ---------------------------------------------------------------------------

def _try_load_feedback_module() -> bool:
    """Import feedback_loop integration from the live-installed memchorus package.

    Returns True if the module loaded successfully. The globals
    `inject_feedback_corrections`, `TurnContext` and `TriggerEvent` become
    available only while ``_feedback_mod_available`` is True, so callers must
    check that flag before using them.
    """
    global _feedback_mod_available
    if not _feedback_mod_available:
        try:
            from memchorus.feedback_loop.integration import (  # noqa: F401, E402
                inject_feedback_corrections,
                TurnContext,
                TriggerEvent,
            )
            _feedback_mod_available = True
        except Exception as exc:
            logger.debug("Feedback loop module not available (non-fatal): %s", exc)

    return _feedback_mod_available


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def _on_pre_llm_call(*, user_message: str = "", **_kwargs: Any) -> Optional[Dict[str, Any]]:
    """Fire pre-decision recall and inject context before the LLM call.

    Hermes expects a dict with ``context`` key or a string -- it appends this
    to the user message (see turn_context.py). An empty result is silently dropped.

    Additionally returns ``indicators`` so the gateway can render TUI icon
    markers when MemChorus performs recall/storage on behalf of this plugin
    without polluting conversation history with synthetic tool calls.
    """
    orch = _bootstrap()
    if orch is None:
        return None

    context_parts: List[str] = []

    # --- Memory recall ---
    try:
        em = orch._get_enforcement_manager()
        if em is None:
            return None

        result = em.enforce(user_message)
        if result.recall_context:
            seen_keys = set()
            for hit in result.recall_context[:5]:
                key = hit.get("key", "")
                content = hit.get("content", "")
                if key and content and key not in seen_keys:
                    context_parts.append(f"[MEMORY:{key}] {str(content)[:300]}")
                    seen_keys.add(key)

    except Exception as exc:
        logger.warning("pre_llm_call hook error (non-fatal): %s", exc)

    # --- Feedback loop corrections ---
    feedback_block = None
    if _try_load_feedback_module():
        try:
            from memchorus.feedback_loop.integration import (  # noqa: E402, RUF100
                inject_feedback_corrections,
                TurnContext,
                TriggerEvent,
            )
            turn_ctx = TurnContext(user_message=user_message)
            feedback_block = inject_feedback_corrections(
                turn_ctx, trigger_event=TriggerEvent.PRE_LLM_CALL
            )
        except Exception as exc:
            logger.warning("Feedback injection failed (non-fatal): %s", exc)

    if not context_parts and not feedback_block:
        return None

    # --- Build return dict ---
    injected_lines: List[str] = []
    indicators: List[Dict[str, str]] = []

    if context_parts:
        injected_lines.append("-- Pre-decision Memory Recall --")
        injected_lines.extend(context_parts)
        indicators.append(
            {
                "name": "memory_search",
                "label": "MemChorus recall",
            }
        )

    # Merge memory block and feedback loop blocks into a single injection string
    if feedback_block:
        injected_lines.append(feedback_block)
        indicators.append(
            {
                "name": "feedback_steering",
                "label": "Feedback loop active",
            }
        )

    return {
        "context": "\n".join(injected_lines),
        "indicators": indicators,
    }


def _on_post_tool_call(*, tool_name: str = "", tool_result: Any = None, **_kwargs: Any) -> Optional[Dict[str, Any]]:
    """Capture meaningful outcomes after every tool execution.

    Observer hook -- returns indicators so the gateway can show a storage icon
    when MemChorus autonomously captures memory after a tool call completes.
    """
    orch = _bootstrap()
    if orch is None:
        return None

    try:
        em = orch._get_enforcement_manager()
        if em is None:
            return None

        text_to_analyse = f"[TOOL:{tool_name}]"
        if tool_result and isinstance(tool_result, dict):
            output = tool_result.get("output", "") or tool_result.get("content", "")
            if output:
                text_to_analyse += f" {str(output)[:500]}"

        # Post-action capture fires the enforcement pipeline
        result = em.enforce(text_to_analyse)

        # Announce storage activity to the TUI via indicators
        stored = getattr(result, "stored_keys", [])
        if stored:
            return {
                "indicators": [
                    {
                        "name": "memory_store",
                        "label": f"MemChorus stored ({len(stored)} item{'s' if len(stored) != 1 else ''})"
                    }
                ],
            }

    except Exception as exc:
        logger.warning("post_tool_call hook error (non-fatal): %s", exc)

    return None


# ---------------------------------------------------------------------------
# Plugin entry point -- called by the Hermes plugin loader on startup.
# ---------------------------------------------------------------------------

def register(ctx) -> None:  # noqa: ANN001
    """Register MemChorus hooks with the Hermes plugin manager context."""
    logger.info("MemChorus integration: register() called")

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    logger.debug("Registered pre_llm_call hook")

    ctx.register_hook("post_tool_call", _on_post_tool_call)
    logger.debug("Registered post_tool_call hook")

    # Bootstrap once at registration -- not on every turn.
    _bootstrap()
