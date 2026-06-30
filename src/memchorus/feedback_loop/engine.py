"""Evaluation engine for declarative feedback loop conditions.

Provides ``FeedbackDetector`` -- the class that inspects turn-level context
against a list of loaded ``FeedbackLoopDefinition`` instances and returns a
list of correction-prompt strings (or ``[]`` when nothing fires).  This module
is intentionally thin: it re-uses condition evaluators from this package's
``integration`` submodule and escalation tracking from the ``escalation`` module.

Condition types supported::

    conversation_length          threshold check on session length
    tool_response_empty_count    number of empty/minimal tool-response calls
    repetition_entropy           word-set-overlap entropy across recent messages

Usage (plug into ``pre_llm_call`` hook)::

    detector = FeedbackDetector(definitions, escalation_tracker)
    prompts = detector.evaluate(turn_context)   # -> List[str]
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from memchorus.feedback_loop.escalation import EscalationTracker
from memchorus.feedback_loop.integration import (  # noqa: F401
    TurnContext,
    ConditionEvaluator,
)
from memchorus.feedback_loop.schema_v1 import FeedbackLoopDefinition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standalone condition classes for testing outside the engine dispatch table
# ---------------------------------------------------------------------------


class _ConversationLengthCondition:
    """Matches conversation_length thresholds using gt/gte/lt/lte or >= fallback."""

    @staticmethod
    def matches(value, current_length):
        if isinstance(value, dict):
            gt_val = value.get("gt")
            if gt_val is not None and isinstance(gt_val, (int, float)):
                return current_length > gt_val
            gte_val = value.get("gte")
            if gte_val is not None and isinstance(gte_val, (int, float)):
                return current_length >= gte_val
            lt_val = value.get("lt")
            if lt_val is not None and isinstance(lt_val, (int, float)):
                return current_length < lt_val
            lte_val = value.get("lte")
            if lte_val is not None and isinstance(lte_val, (int, float)):
                return current_length <= lte_val
        elif isinstance(value, (int, float)):
            return current_length >= value
        return False


class _ToolResponseEmptyCountCondition:
    """Matches tool_response_empty_count thresholds using gt/gte/lt/lte or >= fallback."""

    @staticmethod
    def matches(value, current_count):
        if isinstance(value, dict):
            gt_val = value.get("gt")
            if gt_val is not None and isinstance(gt_val, (int, float)):
                return current_count > gt_val
            gte_val = value.get("gte")
            if gte_val is not None and isinstance(gte_val, (int, float)):
                return current_count >= gte_val
            lt_val = value.get("lt")
            if lt_val is not None and isinstance(lt_val, (int, float)):
                return current_count < lt_val
            lte_val = value.get("lte")
            if lte_val is not None and isinstance(lte_val, (int, float)):
                return current_count <= lte_val
        elif isinstance(value, (int, float)):
            return current_count >= value
        return False


class _RepetitionEntropyCondition:
    """Computes word-set-overlap entropy across recent messages.

    Returns 0.0 for identical content, 1.0 for completely disjoint vocabularies.
    Fires when entropy drops below a threshold (i.e. high repetition).
    """

    @staticmethod
    def _compute(messages):
        # Fewer than 2 messages -- nothing to compare against
        if len(messages) < 2:
            return 1.0
        word_sets = [set(msg.lower().split()) for msg in messages if msg.strip()]
        if len(word_sets) < 2:
            return 1.0
        total_pairs = len(word_sets) * (len(word_sets) - 1) // 2
        if total_pairs == 0:
            return 1.0
        overlap_sum = 0.0
        for i in range(len(word_sets)):
            for j in range(i + 1, len(word_sets)):
                union_ = word_sets[i] | word_sets[j]
                if union_:
                    overlap_sum += len(word_sets[i] & word_sets[j]) / len(union_)
        # entropy = 1 - average_overlap; high repetition => low entropy
        return 1.0 - (overlap_sum / total_pairs)

    @staticmethod
    def matches(threshold, messages):
        score = _RepetitionEntropyCondition._compute(messages)
        return score < threshold


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class FeedbackDetector:
    """Evaluate feedback-loop conditions against turn context.

    Parameters
    ----------
    definitions : List[FeedbackLoopDefinition]
        Loaded loop definitions (from the loader).  Only ``enabled=True``
        entries are considered.
    escalation : EscalationTracker
        Shared escalation tracker for cooldown + level management.
    """

    def __init__(
        self,
        definitions: List[FeedbackLoopDefinition],
        escalation: EscalationTracker,
    ) -> None:
        self._definitions = definitions
        self._escalation = escalation

    def evaluate(self, turn_context: TurnContext) -> List[str]:
        """Run all *enabled* loop defs through their conditions.

        Returns a list of correction-prompt strings when conditions match and
        cooldown allows; ``[]`` otherwise.
        """
        results: List[str] = []

        for loop_def in self._definitions:
            if not getattr(loop_def, "enabled", True):
                continue

            # --- conditions (AND logic) -------------------------------------
            conditions = getattr(loop_def, "conditions", {})
            if conditions is not None and len(conditions) > 0 and not self._all_conditions_match(conditions, turn_context):
                continue

            # --- cooldown check ---------------------------------------------
            cooldown_interval = loop_def.cooldown_interval or 60
            if not self._escalation.check_cooldown(loop_def.name, cooldown_interval):
                continue

            # --- record trigger + get level ---------------------------------
            level = self._escalation.record_trigger(loop_def.name)

            # --- build correction prompt ------------------------------------
            base_prompt = getattr(loop_def, "correction_prompt", "") or ""
            prompt = self._format_prompt(loop_def.name, base_prompt, level)
            results.append(prompt)

        return results

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _all_conditions_match(
        self, conditions: Dict[str, Any], ctx: TurnContext
    ) -> bool:
        """Return True when all signals in *conditions* match."""
        for signal in conditions.values():
            if not self._check_signal(signal, ctx):
                return False
        return True

    def _check_signal(self, signal: Any, ctx: TurnContext) -> bool:
        """Evaluate a single ConditionSignal (or raw dict) against ``ctx``."""
        sig_type = getattr(signal, "type", "") or ""
        if not sig_type and isinstance(signal, dict):
            sig_type = signal.get("type", "") or ""

        value_raw = getattr(signal, "value", None)
        if value_raw is None and isinstance(signal, dict):
            value_raw = signal.get("value")

        matcher = ConditionEvaluator._MATCHERS.get(sig_type)
        if matcher is None:
            logger.warning("Unknown feedback condition signal type: %r -- skipping", sig_type)
            return False

        try:
            return bool(matcher(value_raw, ctx))
        except Exception as exc:
            logger.warning("Condition evaluation error for type %r: %s -- skipping", sig_type, exc)
            return False

    @staticmethod
    def _format_prompt(loop_name: str, base: str, level: int) -> str:
        prefix_map = {
            1: "STEERING (Level 1 hint):",
            2: "FEEDBACK (Level 2 nudge):",
            3: "CORRECTION (Level 3 -- full stop and reassess):",
        }
        prefix = prefix_map.get(max(1, min(level, 3)), "FEEDBACK:")
        return f"[FEEDBACK:{loop_name}] {prefix} {base}"
