"""Feedback loop detection engine for MemChorus.

Evaluates feedback-loop *conditions* against a snapshot of the current turn
context and returns structured match results.  Supports condition types:

- ``conversation_length``    : > N turns since session start
- ``repetition_entropy``     : n-gram repetition scoring (low entropy = high repetition)
- ``keyword_pattern``        : regex against user message + recent history
- ``empty_tool_response_count`` : consecutive empty tool responses threshold

Pattern reference: see ``memchorus.behavioral_trigger`` for keyword-scanning style
and ``memchorus.enforcement_manager`` for the trigger->chain wiring.
"""

from __future__ import annotations

import logging
import re as _re_mod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Condition types
# ---------------------------------------------------------------------------

CONDITION_TYPES = frozenset([
    "conversation_length",
    "repetition_entropy",
    "keyword_pattern",
    "empty_tool_response_count",
])


# ---------------------------------------------------------------------------
# 2. Data classes
# ---------------------------------------------------------------------------


@dataclass
class TurnContext:
    """Lightweight snapshot of the current turn, available at hook-fire time."""
    user_message: str = ""
    conversation_length: int = 0
    tool_calls_this_turn: int = 0
    recent_messages: List[str] = field(default_factory=list)


@dataclass
class MatchedCondition:
    """A single condition that was evaluated (matched or not)."""
    name: str               # signal key from loop definition
    condition_type: str     # e.g. "conversation_length"
    value_threshold: Any   # the configured threshold
    measured_value: Any     # the context-derived measurement
    matched: bool = True


@dataclass
class DetectionResult:
    """Structured result of running a detection engine over turn context."""
    loop_name: str
    severity: str           # "low" | "medium" | "high" based on match count
    matched_conditions: List[MatchedCondition] = field(default_factory=list)
    correction_prompt_filled: bool = False


# ---------------------------------------------------------------------------
# 3. Condition evaluators (one per condition type)
# ---------------------------------------------------------------------------

def _compute_word_entropy(messages: List[str]) -> float:
    """Calculate word-set-overlap entropy across messages.

    Returns a value in [0, 1):
      - 0 = all identical content (maximum repetition)
      - near-1 = highly diverse (no repetition detected)
    """
    if len(messages) < 2:
        return 1.0  # no entropy to compute with single message

    word_sets = [set(msg.lower().split()) for msg in messages if msg.strip()]
    if not word_sets or len(word_sets) < 2:
        return 0.5  # ambiguous baseline

    total_pairs = len(word_sets) * (len(word_sets) - 1) // 2
    if total_pairs == 0:
        return 0.5

    overlap_sum = 0.0
    for i in range(len(word_sets)):
        for j in range(i + 1, len(word_sets)):
            s1, s2 = word_sets[i], word_sets[j]
            union = s1 | s2
            if union:
                overlap_sum += len(s1 & s2) / len(union)

    avg_overlap = overlap_sum / total_pairs
    return 1.0 - avg_overlap


def _evaluate_conversation_length(signal: Any, context: TurnContext) -> MatchedCondition | None:
    """Evaluate conversation_length condition.

    Supported value shapes:
      - int/float ``50``   -> count >= 50
      - dict {gt: N} or {"gte": N}  -> uses gt/gte semantics
    """
    if not isinstance(signal, (dict, int, float)):
        return None

    # Extract threshold value
    if isinstance(signal, dict):
        for k in ("gt", ">"):
            if k in signal and isinstance(signal[k], (int, float)):
                threshold = int(signal[k])
                matched = context.conversation_length > threshold
                return MatchedCondition(
                    name="conversation_length", condition_type="conversation_length",
                    value_threshold=threshold, measured_value=context.conversation_length,
                    matched=matched,
                )
        for k in ("gte", ">="):
            if k in signal and isinstance(signal[k], (int, float)):
                threshold = int(signal[k])
                matched = context.conversation_length >= threshold
                return MatchedCondition(
                    name="conversation_length", condition_type="conversation_length",
                    value_threshold=threshold, measured_value=context.conversation_length,
                    matched=matched,
                )
        if "value" in signal and isinstance(signal["value"], (int, float)):
            threshold = int(signal["value"])
            matched = context.conversation_length >= threshold
            return MatchedCondition(
                name="conversation_length", condition_type="conversation_length",
                value_threshold=threshold, measured_value=context.conversation_length,
                matched=matched,
            )
    elif isinstance(signal, (int, float)):
        threshold = int(signal)
        matched = context.conversation_length >= threshold
        return MatchedCondition(
            name="conversation_length", condition_type="conversation_length",
            value_threshold=threshold, measured_value=context.conversation_length,
            matched=matched,
        )
    return None


def _evaluate_repetition_entropy(signal: Any, context: TurnContext) -> MatchedCondition | None:
    """Evaluate repetition_entropy condition.

    Low entropy = high repetition.  Condition is met when entropy_score < threshold.
    """
    threshold: float
    if isinstance(signal, dict):
        raw = signal.get("threshold", signal.get("value"))
        threshold = float(raw) if isinstance(raw, (int, float)) else 0.5  # sensible default
    elif isinstance(signal, (int, float)):
        threshold = float(signal)
    else:
        return None

    messages = context.recent_messages[-10:]
    entropy_score = _compute_word_entropy(messages)
    matched = entropy_score < threshold  # low entropy = high repetition

    return MatchedCondition(
        name="repetition_entropy", condition_type="repetition_entropy",
        value_threshold=threshold, measured_value=round(entropy_score, 4),
        matched=matched,
    )


def _evaluate_keyword_pattern(signal: Any, context: TurnContext) -> MatchedCondition | None:
    """Evaluate keyword_pattern condition.

    Matches a regex pattern against user message + recent history.
    """
    if not isinstance(signal, (dict, str)):
        return None

    raw = signal.get("pattern", signal.get("value")) if isinstance(signal, dict) else signal
    if not isinstance(raw, str) or not raw:
        return None

    umsg = context.user_message.strip()
    recent = " ".join(context.recent_messages[-10:])
    search_text = f"{umsg} {recent}".strip()

    if not search_text:
        return MatchedCondition(
            name="keyword_pattern", condition_type="keyword_pattern",
            value_threshold=raw, measured_value="", matched=False,
        )

    try:
        matched = bool(_re_mod.search(raw, search_text, _re_mod.IGNORECASE))
    except _re_mod.error as exc:
        logger.warning("Invalid regex in keyword_pattern condition: %r -- %s", raw, exc)
        return None

    return MatchedCondition(
        name="keyword_pattern", condition_type="keyword_pattern",
        value_threshold=raw, measured_value=search_text[:200], matched=matched,
    )


def _evaluate_empty_tool_response_count(signal: Any, context: TurnContext) -> MatchedCondition | None:
    """Evaluate empty_tool_response_count condition.

    Fires when the count of empty tool responses >= threshold.
    """
    if not isinstance(signal, (dict, int, float)):
        return None

    if isinstance(signal, dict):
        for k in ("count", "threshold"):
            if k in signal and isinstance(signal[k], (int, float)):
                threshold = int(signal[k])
                measured = context.tool_calls_this_turn  # proxy
                return MatchedCondition(
                    name="empty_tool_response_count", condition_type="empty_tool_response_count",
                    value_threshold=threshold, measured_value=measured,
                    matched=measured >= threshold,
                )
        if "value" in signal and isinstance(signal["value"], (int, float)):
            threshold = int(signal["value"])
            measured = context.tool_calls_this_turn
            return MatchedCondition(
                name="empty_tool_response_count", condition_type="empty_tool_response_count",
                value_threshold=threshold, measured_value=measured,
                matched=measured >= threshold,
            )
    elif isinstance(signal, (int, float)):
        threshold = int(signal)
        measured = context.tool_calls_this_turn
        return MatchedCondition(
            name="empty_tool_response_count", condition_type="empty_tool_response_count",
            value_threshold=threshold, measured_value=measured,
            matched=measured >= threshold,
        )
    return None


# Mapper from type-string -> evaluator function (or None if not applicable).
_COND_EVALUATORS: Dict[str, Any] = {
    "conversation_length": _evaluate_conversation_length,
    "repetition_entropy": _evaluate_repetition_entropy,
    "keyword_pattern": _evaluate_keyword_pattern,
    "empty_tool_response_count": _evaluate_empty_tool_response_count,
}


# ---------------------------------------------------------------------------
# 4. Public detection engine class
# ---------------------------------------------------------------------------

class FeedbackLoopDetector:
    """Detection engine that scans feedback-loop conditions against context.

    Each condition type is evaluated independently; the detection result has a
    list of MatchedCondition objects (with their individual matched=False/True),
    plus an aggregated ``severity`` derived from how many individually fired, and
    a boolean indicating whether any correction prompt should be injected.

    This deliberately mirrors the pattern used by ``BehavioralTrigger.detect()``
    (keyword scanning -> ranked results) while producing structured data suitable
    for both escalation tracking and diagnostic logging.
    """

    def __init__(self) -> None:
        pass  # expand state later if needed

    # -- Public API ----------------------------------------------------------

    def detect(
        self,
        loop_name: str,
        conditions: Dict[str, Any],  # ConditionSignal objects or plain dicts
        context: TurnContext,
    ) -> DetectionResult:
        """Evaluate *conditions* against *context*.

        Each condition key in the dict is a signal (possibly a ConditionSignal
        or plain dict).  Every individual signal's matched state is recorded; the
        overall severity depends on how many individually fired.

        Returns:
            DetectionResult with severity and per-signal MatchedCondition entries.
        """
        results: List[MatchedCondition] = []

        for signal_key, raw_signal in conditions.items():
            evaluated: Optional[MatchedCondition] = None

            # Unwrap ConditionSignal objects to plain dict first.
            if hasattr(raw_signal, "to_dict") and callable(getattr(raw_signal, "to_dict")):
                raw_signal = raw_signal.to_dict()  # type: ignore[union-attr]
            elif hasattr(raw_signal, "__dict__"):
                raw_signal = dict(raw_signal.__dict__)

            # Extract nested "value" field so evaluators receive the
            # threshold structure (e.g. {"gt": 30}) rather than a
            # ConditionSignal wrapper dict that contains it.
            if isinstance(raw_signal, dict) and "type" in raw_signal and "value" in raw_signal:
                nested_value = raw_signal["value"]
                if isinstance(nested_value, dict):
                    raw_signal = {**raw_signal, **nested_value}

            signal_type = ""
            if isinstance(raw_signal, dict):
                signal_type = raw_signal.get("type", "")
            elif isinstance(signal_key, str) and signal_key in CONDITION_TYPES:
                # Signal key itself is the type (schema_v1 uses signal keys like "spiral_risk_level").
                pass

            # Evaluate by explicit condition-type field first, then try matching
            # against known evaluators by signal_type string.
            if isinstance(raw_signal, dict) and "type" in raw_signal:
                eval_fn = _COND_EVALUATORS.get(raw_signal["type"])  # type: ignore[index]
                if eval_fn is not None:
                    evaluated = eval_fn(raw_signal, context)

            # Fallback: no explicit 'type' field -- infer from context.
            if evaluated is None:
                evaluated = self._infer_evaluate(signal_key, raw_signal, context)

            if evaluated is None:
                evaluated = MatchedCondition(
                    name=signal_key, condition_type="unknown",
                    value_threshold=raw_signal, measured_value=None, matched=False,
                )
                evaluated.__dict__["condition_type"] = str(raw_signal.get("type", "unknown"))  # type: ignore[assignment]

            results.append(evaluated)

        severity = self._compute_severity(results)
        any_matched = any(r.matched for r in results)
        # correction_prompt_filled is True only when at least one condition fires
        # AND the engine can produce a correction (non-empty conditions).
        correction_filled = any_matched and len(conditions) > 0

        return DetectionResult(
            loop_name=loop_name,
            severity=severity,
            matched_conditions=results,
            correction_prompt_filled=correction_filled,
        )

    def detect_all(
        self,
        loops: List[Any],
        context: TurnContext,
    ) -> List[DetectionResult]:
        """Run detection on a list of loop definitions and return non-empty results only.

        Args:
            loops: Iterable of objects with .name (str), .conditions (dict),
                   .enabled (bool) attributes -- typical FeedbackLoopDefinition instances.
            context: TurnContext snapshot.

        Returns:
            Detected results for loops where at least one condition matched.
        """
        results: List[DetectionResult] = []
        for loop in loops:
            if not getattr(loop, "enabled", True):
                continue
            conditions = getattr(loop, "conditions", {})
            detection = self.detect(getattr(loop, "name", "unknown"), conditions, context)

            if any(m.matched for m in detection.matched_conditions):
                results.append(detection)
        return results

    # -- Internal methods ----------------------------------------------------

    def _infer_evaluate(
        self,
        signal_key: str,
        raw_signal: Any,
        context: TurnContext,
    ) -> Optional[MatchedCondition]:
        """Infer condition type from key name and evaluate.

        If the caller did not include a 'type' field but the signal key itself
        matches one of our known types, try evaluating it.
        Also tries heuristic guessing from value shape.
        """
        # Key is literally a condition type name -> try all evaluators
        for tname, eval_fn in _COND_EVALUATORS.items():
            if tname in signal_key:
                return eval_fn(raw_signal, context)

        # Heuristic guessers (value shape):
        if isinstance(raw_signal, int):
            # Likely a numeric threshold -- try empty_tool first
            ev = _evaluate_empty_tool_response_count(raw_signal, context)  # type: ignore[arg-type]
            return ev or MatchedCondition(
                name=signal_key, condition_type="numeric",
                value_threshold=raw_signal, measured_value=context.tool_calls_this_turn,
                matched=context.tool_calls_this_turn >= raw_signal,
            )

        if isinstance(raw_signal, str):
            # Likely a keyword pattern string
            val: Any = {"pattern": raw_signal}
            return _evaluate_keyword_pattern(val, context)  # type: ignore[arg-type]

        return None  # unrecognised signal shape -- caller should log warning

    @staticmethod
    def _compute_severity(results: List[MatchedCondition]) -> str:
        """Classify severity based on how many conditions individually fired."""
        hit_count = sum(1 for r in results if r.matched)
        if hit_count >= 3:
            return "high"
        elif hit_count == 2:
            return "medium"
        else:
            return "low"
