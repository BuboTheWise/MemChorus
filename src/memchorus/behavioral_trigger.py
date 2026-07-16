"""
BehavioralTrigger: Decision-point detection for conversation flow patterns.

Detects when an agent reaches a decision point in its conversation flow and fires
registered callbacks so relevant memories can surface automatically. This is the core
proactive component of MemChorus v1.0 behavioral enforcement.

Decision point types (priority-highest first):

- ERROR_STATE          -- Agent encounters errors / failures
- PLANNING_START       -- Agent begins planning / selecting an approach
- TOOL_CALL_INTENT     -- Agent prepares to execute a tool / operation
- POST_ACTION_COMPLETE -- Agent finishes a task/tool execution

Patterns are keyword-based (case-insensitive, word-boundary matching). No
external dependencies beyond the Python standard library.
"""

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# 1. Decision point enumeration and priority order
# ---------------------------------------------------------------------------


class DecisionPoint(Enum):
    """Types of decision points detected in agent conversation text."""

    ERROR_STATE = auto()                      # Agent encountered errors / failures
    PLANNING_START = auto()                   # Agent begins planning or selecting an approach
    TOOL_CALL_INTENT = auto()                 # Agent prepares to execute a tool/operation
    POST_ACTION_COMPLETE = auto()             # Agent finishes a task/tool execution
    CONTEXTUAL_SYNTHESIS_COMPLETION = auto()  # Agent processes docs and produces understanding

    @classmethod
    def priority(cls, dp: "DecisionPoint") -> int:
        """Return lower number = higher priority. ERROR_STATE > PLANNING_START …"""
        order = {
            cls.ERROR_STATE:                    0,
            cls.PLANNING_START:                 1,
            cls.TOOL_CALL_INTENT:               2,
            cls.POST_ACTION_COMPLETE:           3,
            cls.CONTEXTUAL_SYNTHESIS_COMPLETION: 4,
        }
        return order[dp]


# ---------------------------------------------------------------------------
# 2. Data class for returned decision points
# ---------------------------------------------------------------------------


@dataclass
class DetectedPoint:
    """A single detected decision point with confidence and metadata."""

    type: DecisionPoint
    confidence: float          # 0.0 – 1.0
    matched_keyword: str       # the keyword (lower-cased) that triggered detection
    text_span: Optional[str]   # the matching span from the input text, if available


# ---------------------------------------------------------------------------
# 3. Keyword / compiled regex patterns for each decision point type
# ---------------------------------------------------------------------------

# Each tuple: (pattern_str, priority_class). The patterns are listed in order —
# we assign them to priority classes as we walk the list.

_PRIORITY_KEYWORDS = [
    # --- ERROR_STATE (priority 0) ----------------------------------------
    ("error",          DecisionPoint.ERROR_STATE),
    ("failed",         DecisionPoint.ERROR_STATE),
    ("exception",      DecisionPoint.ERROR_STATE),
    ("traceback",      DecisionPoint.ERROR_STATE),
    ("went wrong",     DecisionPoint.ERROR_STATE),
    # B-3 fix: imperative error/action patterns that agents actually use
    ("fix ",           DecisionPoint.ERROR_STATE),            # trailing space avoids 'fixed' false positive
    ("failed to",      DecisionPoint.ERROR_STATE),
    ("bug",            DecisionPoint.ERROR_STATE),
    ("regression",     DecisionPoint.ERROR_STATE),
    ("resolve",        DecisionPoint.ERROR_STATE),

    # --- PLANNING_START (priority 1) -------------------------------------
    ("i need to implement",   DecisionPoint.PLANNING_START),
    ("the plan is",           DecisionPoint.PLANNING_START),
    ("first step",            DecisionPoint.PLANNING_START),
    ("approach selection",    DecisionPoint.PLANNING_START),
    ("strategy",              DecisionPoint.PLANNING_START),
    # B-1 fix: broader coverage for planning intent in typical agent speech
    ("i need to plan",        DecisionPoint.PLANNING_START),
    ("my plan",               DecisionPoint.PLANNING_START),
    ("let me plan",           DecisionPoint.PLANNING_START),
    ("planning to",           DecisionPoint.PLANNING_START),
    # B-3 fix: imperative planning patterns
    ("implement ",            DecisionPoint.PLANNING_START),   # trailing space avoids 'implementation' false fire
    ("next step",             DecisionPoint.PLANNING_START),
    ("build ",                DecisionPoint.PLANNING_START),

    # --- TOOL_CALL_INTENT (priority 2) -----------------------------------
    ("next i will call",      DecisionPoint.TOOL_CALL_INTENT),
    ("i'll use",              DecisionPoint.TOOL_CALL_INTENT),
    ("running the command",   DecisionPoint.TOOL_CALL_INTENT),
    ("executing",             DecisionPoint.TOOL_CALL_INTENT),
    # B-1 fix: broader coverage for tool-call intent in typical agent speech
    ("let me run",            DecisionPoint.TOOL_CALL_INTENT),
    ("tool call",             DecisionPoint.TOOL_CALL_INTENT),
    ("i will use",            DecisionPoint.TOOL_CALL_INTENT),
    ("call the",              DecisionPoint.TOOL_CALL_INTENT),
    # B-3 fix: imperative action patterns common in agent messages
    ("review ",               DecisionPoint.TOOL_CALL_INTENT),
    ("test ",                 DecisionPoint.TOOL_CALL_INTENT),
    ("verify",                DecisionPoint.TOOL_CALL_INTENT),
    ("troubleshoot",          DecisionPoint.TOOL_CALL_INTENT),

    # --- POST_ACTION_COMPLETE (priority 3) ----------------------------------
    ("completed",       DecisionPoint.POST_ACTION_COMPLETE),
    ("finished",        DecisionPoint.POST_ACTION_COMPLETE),
    ("done with",       DecisionPoint.POST_ACTION_COMPLETE),
    ("output received", DecisionPoint.POST_ACTION_COMPLETE),
    ("result is",       DecisionPoint.POST_ACTION_COMPLETE),

    # --- CONTEXTUAL_SYNTHESIS_COMPLETION (priority 4) -----------------------
    ("learned that",            DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION),
    ("discovered important",    DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION),
    ("found evidence showing",  DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION),
    ("after analyzing",         DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION),
    ("key finding",             DecisionPoint.CONTEXTUAL_SYNTHESIS_COMPLETION),
]


class _PatternStore:
    """Pre-compile all keyword patterns into regex, grouped by DecisionPoint."""

    def __init__(self):
        # Group keywords by priority class
        self._groups: Dict[DecisionPoint, List[tuple]] = {dp: [] for dp in DecisionPoint}  # type: ignore[union-init, arg-type, assignment]
        for pattern_str, dp_class in _PRIORITY_KEYWORDS:
            # Handle multi-word patterns by wrapping each word with \b individually
            words = pattern_str.lower().split()
            if len(words) == 1:
                regex_str = rf"\b{re.escape(words[0])}\b"
            else:
                parts = [rf"\b{re.escape(w)}\b" for w in words]
                # .*? allows flexible spacing between words with non-greedy matching
                regex_str = ".*?".join(parts)
            compiled = re.compile(regex_str, re.IGNORECASE | re.UNICODE)
            self._groups[dp_class].append((compiled, pattern_str))

    def get_patterns(self, dp: DecisionPoint) -> List[tuple]:
        """Return list of (compiled_regex, original_keyword) for *dp*."""
        return self._groups.get(dp, [])


_PATTERN_STORE = _PatternStore()           # module-level singleton

# ---------------------------------------------------------------------------
# H-3: Validate all priority keywords were assigned at module load time
#      to catch future misconfigures early rather than silently dropping them
# ---------------------------------------------------------------------------
_assigned_keywords: set = set()
for patterns in _PATTERN_STORE._groups.values():
    for _, keyword in patterns:
        _assigned_keywords.add(keyword)

_expected_keywords: set = {kw for kw, _ in _PRIORITY_KEYWORDS if isinstance(kw, str)}
if not _expected_keywords.issubset(_assigned_keywords):
    raise RuntimeError(
        f"BehavioralTrigger configuration error: keywords missing from DecisionPoint groups: "
        f"{_expected_keywords - _assigned_keywords}"
    )


class BehavioralTrigger:
    """Detects decision points in agent conversation flow and fires memory hooks.

    Use ``detect(text)` to analyse free-text and get a list of DetectedPoint
    instances, sorted by decision-point priority (errors first).

    Register callbacks with ``on_decision_point(callback_func)`` — they are
    called in priority order whenever their associated DecisionPoint is matched.
    """

    def __init__(self) -> None:
        self._callbacks: Dict[DecisionPoint, List[Callable[[DetectedPoint], None]]] = {
            dp: [] for dp in DecisionPoint  # type: ignore[attr-defined, union-init, arg-type, assignment]
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, text: str) -> List[DetectedPoint]:
        """Analyse *text* and return all matched decision points (priority-sorted).

        Each DetectedPoint carries the ``type``, a ``confidence`` score derived from
        pattern matching strength, and the ``matched_keyword`` that triggered detection.
        Empty list if no patterns match."""
        results: List[DetectedPoint] = []
        seen_keywords: set = set()     # avoid duplicates

        for dp in DecisionPoint:  # type: ignore[attr-defined]
            patterns = _PATTERN_STORE.get_patterns(dp)
            for compiled_regex, keyword in patterns:
                if keyword in seen_keywords:
                    continue
                match = compiled_regex.search(text)
                if match is not None:
                    confidence = self._confidence_score(compiled_regex, text)
                    detected = DetectedPoint(
                        type=dp,
                        confidence=confidence,
                        matched_keyword=keyword,
                        text_span=match.group(),
                    )
                    results.append(detected)
                    seen_keywords.add(keyword)

        # Sort by priority (lower number = higher priority) for consistent ordering
        results.sort(key=lambda dp: dp.type.priority(dp.type))  # type: ignore[arg-type, union-attr]

        return results

    def on_decision_point(self, callback_func: Callable[[DetectedPoint], None]) -> None:
        """Register *callback_func* to be called for **all** DecisionPoint types.

        The callback receives the DetectedPoint instance as its sole argument."""
        for dp in DecisionPoint:  # type: ignore[attr-defined]
            self._callbacks[dp].append(callback_func)  # type: ignore[index]

    def on(self, decision_point: DecisionPoint, callback_func: Callable[[DetectedPoint], None]) -> None:
        """Register *callback_func* for a **specific** DecisionPoint.

        Args:
            decision_point: The DecisionPoint type to listen for.
            callback_func: Function that receives DetectedPoint when matched."""
        self._callbacks[decision_point].append(callback_func)  # type: ignore[index]

    def fire(self, text: str) -> List[DetectedPoint]:
        """Detect decision points in *text* AND fire registered callbacks.

        Returns the same list as ``detect()``, then invokes each matching
        callback with its DetectedPoint in priority order.

        Returns:
            List[DetectedPoint]: The detected points (same as ``detect(text)`")."""
        points = self.detect(text)
        for dp in DecisionPoint:  # type: ignore[attr-defined]
            patterns = _PATTERN_STORE.get_patterns(dp)
            if not patterns:
                continue
            # Find all DetectedPoints of this type
            matched_points = [p for p in points if p.type == dp]  # type: ignore[arg-type, union-attr]
            for callback in self._callbacks[dp]:  # type: ignore[index]
                for point in matched_points:
                    try:
                        callback(point)
                    except Exception:
                        pass  # don't let a bad callback kill the rest
        return points

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _confidence_score(self, compiled_regex: re.Pattern, text: str) -> float:
        """Estimate confidence for *compiled_regex* matching in *text*.

        Returns a value between 0.5 and 1.0. Base confidence is 0.7; every
        additional match boosts by +0.1, capped at 1.0."""
        base = 0.7
        count = len(compiled_regex.findall(text))
        boost = min(count - 1, 3) * 0.1    # cap at +0.3 for the first 4+ matches
        return min(base + boost, 1.0)
