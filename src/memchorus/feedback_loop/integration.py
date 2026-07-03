"""Feedback loop integration layer for MemChorus pre_llm_call hook.

Wraps Cthugha's schema/loader modules (schema_v1 + loader) and adds the
missing runtime pieces: condition evaluation, escalation/cooldown tracking,
and the injection adapter that fires inside  _on_pre_llm_call .

Design goal: if both memory recall AND a feedback loop fire on the same turn,
the user sees two labelled blocks in the injected context -- one for memory,
one for steering corrections. No silent blending, no ambiguity about which
signal came from where.
"""

from __future__ import annotations

import logging
import re as _re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Re-export the schema types so callers don't need to reach into two modules.
from memchorus.feedback_loop.schema_v1 import (  # noqa: F401
    FeedbackLoopDefinition,
    TriggerEvent,
)
from memchorus.feedback_loop.loader import load_feedback_loops


# ---------------------------------------------------------------------------
# Runtime turn context -- lightweight snapshot available at hook fire time
# ---------------------------------------------------------------------------


@dataclass
class TurnContext:
    """Turn-level data available at hook fire time."""
    user_message: str = ""
    conversation_length: int = 0
    tool_calls_this_turn: int = 0
    empty_tool_responses: int = 0
    recent_messages: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Condition evaluation engine -- lightweight checks every turn
# ---------------------------------------------------------------------------

class ConditionEvaluator:
    """Evaluate loop conditions against turn context.

    The class-level _MATCHERS dict is the single dispatch table used by both
    ``ConditionEvaluator._check_signal`` and ``FeedbackDetector._check_signal``.
    New condition types are added here only -- no other module should define its
    own matcher for a given signal.type.
    """

    # Dispatch table: signal.type -> (value, context) -> bool
    _MATCHERS: Dict[str, Any] = {}

    @classmethod
    def evaluate(cls, definition: FeedbackLoopDefinition, context: TurnContext) -> bool:
        """Return True only if ALL conditions match (AND logic)."""
        for _key, signal in definition.conditions.items():
            if not cls._check_signal(signal, context):
                return False
        # Empty conditions dict means "always fire" once the loop is enabled.
        return True

    @staticmethod
    def _resolve_field(signal: Any, field_name: str) -> Any:
        val = getattr(signal, field_name, None)
        if val is None and isinstance(signal, dict):
            val = signal.get(field_name)
        return val

    @classmethod
    def _check_signal(cls, signal: Any, context: TurnContext) -> bool:
        """Evaluate a single signal condition entry against turn context."""
        sig_type = cls._resolve_field(signal, "type") or ""
        value = cls._resolve_field(signal, "value")

        # Use dispatcher for known types
        matcher = ConditionEvaluator._MATCHERS.get(sig_type)
        if matcher is not None:
            try:
                return bool(matcher(value, context))
            except Exception as exc:
                logger.warning("Matcher error for type %r: %s -- skipping", sig_type, exc)
                return False

        if not sig_type:
            logger.warning("Empty signal type -- skipping")
            return False

        # Unknown type: skip safely
        logger.warning("Unknown signal type: %s -- skipping safely", sig_type)
        return False

    @staticmethod
    def _match_conversation_length(value: Any, ctx: TurnContext) -> bool:
        if isinstance(value, dict):
            gt_val = value.get("gt")
            if gt_val is not None and isinstance(gt_val, (int, float)):
                return ctx.conversation_length > gt_val
            gte_val = value.get("gte")
            if gte_val is not None and isinstance(gte_val, (int, float)):
                return ctx.conversation_length >= gte_val
        elif isinstance(value, (int, float)):
            return ctx.conversation_length >= value
        return False

    @staticmethod
    def _match_tool_response_empty_count(value: Any, ctx: TurnContext) -> bool:
        if isinstance(value, dict):
            gt_val = value.get("gt")
            if gt_val is not None and isinstance(gt_val, (int, float)):
                return ctx.empty_tool_responses > gt_val
            gte_val = value.get("gte")
            if gte_val is not None and isinstance(gte_val, (int, float)):
                return ctx.empty_tool_responses >= gte_val
        elif isinstance(value, (int, float)):
            return ctx.empty_tool_responses >= value
        return False

    @staticmethod
    def _match_keyword_pattern(value: Any, ctx: TurnContext) -> bool:
        pattern = str(value) if not isinstance(value, str) else value
        if pattern:
            match_text = ctx.user_message + " " + " ".join(ctx.recent_messages[-10:])
            try:
                return bool(_re.search(pattern, match_text, _re.IGNORECASE))
            except Exception:
                pass
        return False

    @staticmethod
    def _match_repetition_entropy(value: Any, ctx: TurnContext) -> bool:
        if not isinstance(value, (int, float)):
            value = 0.5
        messages = ctx.recent_messages[-10:]
        if len(messages) < 2:
            return False
        word_sets: list[set] = []
        for msg in messages:
            words = set(msg.lower().split())
            if words:
                word_sets.append(words)
        if len(word_sets) < 2:
            return False
        total_pairs = len(word_sets) * (len(word_sets) - 1) // 2
        if total_pairs == 0:
            return False
        overlap_sum = 0.0
        for i in range(len(word_sets)):
            for j in range(i + 1, len(word_sets)):
                union_ = word_sets[i] | word_sets[j]
                if union_:
                    overlap_sum += len(word_sets[i] & word_sets[j]) / len(union_)
        entropy = 1.0 - (overlap_sum / total_pairs)
        return entropy < value


# Populate _MATCHERS after class body completes (class object now exists).
ConditionEvaluator._MATCHERS["conversation_length"] = ConditionEvaluator._match_conversation_length
ConditionEvaluator._MATCHERS["tool_response_empty_count"] = ConditionEvaluator._match_tool_response_empty_count
ConditionEvaluator._MATCHERS["keyword_pattern"] = ConditionEvaluator._match_keyword_pattern
ConditionEvaluator._MATCHERS["repetition_entropy"] = ConditionEvaluator._match_repetition_entropy


# ---------------------------------------------------------------------------
# Escalation tracking -- cooldown + step progression
# ---------------------------------------------------------------------------

class EscalationTracker:
    """Per-loop state: cooldown windows and progressive severity levels."""

    _DEFAULT_LEVEL_THRESHOLD = 3  # triggers per level advancement

    def __init__(self) -> None:
        self._state: Dict[str, Dict[str, Any]] = {}

    def init_state(self, loop_name: str, trigger_threshold: int = _DEFAULT_LEVEL_THRESHOLD) -> None:
        """Initialize tracking state for a loop if not already done."""
        if loop_name not in self._state:
            self._state[loop_name] = {
                "trigger_count": 0,
                "last_fired_at": 0.0,
                "level": 1,
                "threshold_per_level": trigger_threshold,
            }

    def should_fire(self, loop_name: str, cooldown_interval: float) -> bool:
        """Return True if cooldown has expired."""
        self.init_state(loop_name)
        last = self._state[loop_name].get("last_fired_at", 0.0)
        elapsed = time.monotonic() - last
        return elapsed >= cooldown_interval

    def record_trigger(self, loop_name: str) -> int:
        """Record a trigger, advance level if threshold crossed. Returns new level."""
        self.init_state(loop_name)
        state = self._state[loop_name]
        state["trigger_count"] += 1
        state["last_fired_at"] = time.monotonic()

        count = state["trigger_count"]
        threshold = state.get("threshold_per_level", self._DEFAULT_LEVEL_THRESHOLD)
        new_level = min(3, max(1, (count - 1) // threshold + 1))
        state["level"] = new_level
        return new_level

    def get_escalation_level(self, loop_name: str) -> int:
        """Return current escalation level (1-3)."""
        self.init_state(loop_name)
        return self._state[loop_name].get("level", 1)

    def reset_loop(self, loop_name: str) -> None:
        """Reset state for a specific loop (used by tests)."""
        self._state.pop(loop_name, None)


# ---------------------------------------------------------------------------
# Integration class -- loads, evaluates, and injects correction prompts
# ---------------------------------------------------------------------------

class FeedbackLoopIntegration:
    """Plug into pre_llm_call to add feedback steering on top of memory recall."""

    def __init__(self, loop_dir: Optional[Path] = None):
        self._loop_dir = loop_dir
        self._escalation = EscalationTracker()
        self._definitions, _ = self._load_definitions()

    @classmethod
    def build(cls, loop_dir: Optional[Path] = None) -> "FeedbackLoopIntegration":
        """Factory method -- create and cache everything needed for turn evaluation."""
        return cls(loop_dir=loop_dir)

    def _load_definitions(self) -> tuple:
        """Load definitions using Cthugha's loader. Returns (definitions, warnings)."""
        directory = str(self._loop_dir) if self._loop_dir else None
        try:
            defs = load_feedback_loops(directory=directory)
            return defs, []
        except Exception as exc:
            logger.warning("Failed to load feedback loop definitions: %s", exc)
            return [], [str(exc)]

    def evaluate(self, turn_context: TurnContext,
                 trigger_event: TriggerEvent) -> List[str]:
        """Evaluate all loops matching the current trigger event. Return correction prompts."""
        results: List[str] = []

        for loop_def in self._definitions:
            if not loop_def.enabled:
                continue
            if loop_def.trigger_event != trigger_event:
                continue

            # Check ALL conditions match
            all_match = ConditionEvaluator.evaluate(loop_def, turn_context)
            if not all_match:
                continue

            # Respect cooldown window
            cooldown = loop_def.cooldown_interval if loop_def.cooldown_interval else 60.0
            if not self._escalation.should_fire(loop_def.name, cooldown):
                continue

            escalation_level = self._escalation.record_trigger(loop_def.name)
            prompt = FeedbackLoopIntegration._build_correction_prompt(
                loop_def, escalation_level
            )
            results.append(prompt)

        return results

    @staticmethod
    def _build_correction_prompt(loop_def: FeedbackLoopDefinition, level: int) -> str:
        """Format the correction prompt for the current escalation level."""
        prefix_map = {
            1: "STEERING (Level 1 hint):",
            2: "FEEDBACK (Level 2 nudge):",
            3: "CORRECTION (Level 3 -- full stop and reassess):",
        }
        prefix = prefix_map.get(level, "FEEDBACK:")
        correction = loop_def.correction_prompt or ""
        return f"[FEEDBACK:{loop_def.name}] {prefix} {correction}"

    def reload(self) -> None:
        """Force a fresh load of definitions from disk."""
        self._definitions, warns = self._load_definitions()
        if warns:
            for w in warns:
                logger.warning("Feedback loop loader warning: %s", w)


# ---------------------------------------------------------------------------
# Integration hook -- called by _on_pre_llm_call inside the plugin
# ---------------------------------------------------------------------------

_feedback_integration: Optional[FeedbackLoopIntegration] = None


def get_feedback_integration() -> Optional[FeedbackLoopIntegration]:
    """Lazy singleton accessor for feedback loop integration."""
    global _feedback_integration
    if _feedback_integration is None:
        try:
            _feedback_integration = FeedbackLoopIntegration.build()
            logger.debug("FeedbackLoopIntegration initialised")
        except Exception as exc:
            logger.warning("Feedback init failed (non-fatal): %s", exc)
            return None
    return _feedback_integration


def auto_load_custom_loops(loop_dir: Optional[str] = None) -> Dict[str, Any]:
    """Eagerly load custom feedback loop definitions from disk.

    Called during bootstrap step [3] to ensure custom loops are available
    before the first hook fires. This populates the singleton and returns
    a diagnostic summary for bootstrap logging.

    Parameters
    ----------
    loop_dir : str, optional
        Directory to scan for ``*.yaml`` / ``*.yml`` definitions.
        Defaults to ``~/.hermes/custom_loops/`` (handled by the loader).

    Returns
    -------
    dict
        ``{"loaded": <int>, "warnings": [<str>], "error": <str|None>}``
        summarising the load outcome for the caller's log reporting.
    """
    global _feedback_integration

    result: Dict[str, Any] = {
        "loaded": 0,
        "warnings": [],
        "error": None,
    }

    try:
        path = Path(loop_dir) if loop_dir else None
        integration = FeedbackLoopIntegration.build(loop_dir=path)
        _feedback_integration = integration

        result["loaded"] = len(integration._definitions)
        # Capture any warnings from the reload pass (e.g. duplicates skipped)
        _, warns = integration._load_definitions()
        result["warnings"] = warns

        logger.info(
            "auto_load_custom_loops: loaded %d feedback loop(s) from %s",
            result["loaded"],
            loop_dir or "default (~/.hermes/custom_loops/)",
        )
    except Exception as exc:
        msg = f"auto_load_custom_loops failed (non-fatal): {exc}"
        result["error"] = msg
        logger.warning(msg)

    return result


def inject_feedback_corrections(
    turn_context: TurnContext,
    trigger_event: TriggerEvent = TriggerEvent.PRE_LLM_CALL,
) -> Optional[str]:
    """Call the feedback loop integration and return a formatted context string.

    Returns None if no corrections match or integration unavailable.
    """
    integration = get_feedback_integration()
    if not integration:
        return None

    try:
        prompts = integration.evaluate(turn_context, trigger_event)
        if not prompts:
            return None

        block_lines = ["-- Feedback Loop Corrections --"]
        for prompt in prompts:
            block_lines.append(prompt)
        return "\n".join(block_lines)

    except Exception as exc:
        logger.warning("Feedback evaluation error (non-fatal): %s", exc)
        return None
