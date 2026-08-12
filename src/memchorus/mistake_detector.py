"""Mistake detector — classifies user correction signals from turn text.

Scans user message content for patterns indicating recalled memory was wrong,
stale or redundant then feeds useful/noise flags back to HitRateTracker.

Part of auto-tuning framework v1.8.0. See docs/AUTOTUNING.md.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CorrectionType(Enum):
    REPETITION = "repetition"        # "I already told you that" / redundant recall content given
    OUTDATED = "outdated"           # correcting stale fact ("that changed", "actually it's now")
    POSITIVE = "positive"           # no correction detected — content flowed naturally


@dataclass
class DetectionResult:
    """One classified correction signal with metadata."""

    correction_type: CorrectionType
    match_text: str
    confidence: float  # rough heuristic weight 0-1

    @property
    def is_negative(self) -> bool:
        return self.correction_type != CorrectionType.POSITIVE


# Configurable pattern list — add/remove entries here per domain vocabulary.
_CORRECTION_PATTERNS: List[Tuple[str, re.Pattern[str], CorrectionType]] = [
    (
        "repetition_general",
        re.compile(
            r"(?:i\s+already\s+told|you\s+[kh]now\s+(?:this|about)|i\s+told\s+"
            r"you\s+(?:before|that|this)|don't\s+forget.*?again)"
            , re.IGNORECASE,
        ),
        CorrectionType.REPETITION,
    ),
    (
        "repetition_redundant",
        re.compile(
            r"(?:that\s+'s\s+already|no\s+says\s+(?:already|previously)|"
            r"duplicate|don't.*?repeat|stop.*?repeating)"
            , re.IGNORECASE,
        ),
        CorrectionType.REPETITION,
    ),
    (
        "outdated_changed",
        re.compile(
            r"(?:now\s+called|changed\s+to|renamed\s+from|actually.*?\bis\b"
            r"\s+(?:no?w)?\s+(?:not|different))"
            , re.IGNORECASE,
        ),
        CorrectionType.OUTDATED,
    ),
    (
        "outdated_correction",
        re.compile(
            r"(?:actually\s+it(?:'s| is)\s+|the\s+(?:real|correct)?\s+"
            r"(?:name|value|date|path))"
            , re.IGNORECASE,
        ),
        CorrectionType.OUTDATED,
    ),
]


class MistakeDetector:
    """Detects user correction signals via pattern matching on turn text.

    Heuristic-only with acceptable false-positive rate since bounded adjustment
    range (±40% cap) protects against runaway feedback anyway. Designed for
    sub-microsecond scan time so it never touches the hot path budget ceiling.
    """

    _instance: Optional["MistakeDetector"] = None

    def __init__(self) -> None:
        self.total_noise_flags = 0
        self.total_useful_flags = 0
        self._recent_results: List[DetectionResult] = []

    @classmethod
    def get_instance(cls) -> "MistakeDetector":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # -- core detection --------------------------------------------------

    def scan_user_text(self, user_text: str) -> List[DetectionResult]:
        """Scan user message for correction patterns. Returns matching signals."""
        results: List[DetectionResult] = []

        start = time.monotonic_ns()
        for _name, pattern, ctype in _CORRECTION_PATTERNS:
            match = pattern.search(user_text)
            if match:
                result = DetectionResult(
                    correction_type=ctype,
                    match_text=match.group(),
                    confidence=0.7,  # fixed heuristic — good enough for bounded feedback
                )
                results.append(result)

        elapsed_us = (time.monotonic_ns() - start) / 1000
        logger.debug("scan %d chars in %.2fμs → %d matches", len(user_text), elapsed_us, len(results))
        return results

    def classify_and_flag(self, user_text: str) -> Tuple[int, int]:
        """Classify text, update global flag counters, and return (noise_added, useful_added).

        Meant to be called from on_turn_end hook after response assembled.
        """
        detected = self.scan_user_text(user_text)

        noise_added = 0
        useful_added = 0

        for result in detected:
            if result.is_negative:
                noise_added += 1
                self.total_noise_flags += 1
                logger.info("noise flag %s: %r", result.correction_type.value, result.match_text[:60])
            else:
                useful_added += 1
                self.total_useful_flags += 1

        # Clear recent buffer to avoid double-counting on back-to-back turns.
        self._recent_results.clear()
        return noise_added, useful_added

    def record_positive_signal(self) -> None:
        """Call when turn completed normally with no user pushback detected."""
        self.total_useful_flags += 1
