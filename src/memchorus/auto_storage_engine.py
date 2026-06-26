"""
AutoStorageEngine: Automatic post-action outcome capture engine.

Captures significant outcomes (learnings, mistakes, decisions, results) after task
completion or tool execution. Implements the "Mandatory post-action storage" requirement
and Behavioral Guarantee #2 from MemChorus-Spec.md:

  "Post-action storage happens automatically: Learnings, mistakes, and significant
   outcomes are captured immediately rather than relying on the agent remembering
   to save later."

This is the write-side counterpart to AutoRecallEngine.

Dependencies: stdlib only (hashlib, time, dataclasses, enum, typing, re).
Does not modify orchestrator, trigger, or recall engine files.
"""

import hashlib
import re
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 1. Significance categories -- the four ways auto-storage classifies content
# ---------------------------------------------------------------------------


class SignificanceCategory(Enum):
    """What kind of significant content was detected in the text."""

    LEARNING = auto()   # Insights gained
    MISTAKE = auto()    # Things that went wrong
    DECISION = auto()   # Important decisions made
    RESULT = auto()     # Significant outcomes / results


# ---------------------------------------------------------------------------
# 2. Keyword / phrase patterns per category (case-insensitive, word-boundary)
# ---------------------------------------------------------------------------

_CATEGORIES_AND_PATTERNS: List[tuple] = [
    (SignificanceCategory.LEARNING, [
        "learned", "realized", "understood", "found that",
    ]),
    (SignificanceCategory.MISTAKE, [
        "went wrong", "wrong approach", "should have", "mistake was",
        "i misinterpreted", "incorrectly", "error in",
    ]),
    (SignificanceCategory.DECISION, [
        "decided", "chose", "go with", "settled on",
        "opted for", "selected", "picked",
    ]),
    (SignificanceCategory.RESULT, [
        "result", "outcome", "achieved", "success",
        "completed", "finished", "verified",
    ]),
]

# Pre-compile regex patterns per category
_compiled: Dict[SignificanceCategory, List[tuple]] = {}
for cat, keywords in _CATEGORIES_AND_PATTERNS:
    _compiled[cat] = [
        (re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE | re.UNICODE), kw)
        for kw in keywords
    ]

# ---------------------------------------------------------------------------
# 3. Data class: the signature returned for each capture attempt
# ---------------------------------------------------------------------------


@dataclass
class CaptureResult:
    """Result of a capture_outcome call."""

    saved: bool
    key: Optional[str] = None
    significance: Optional[SignificanceCategory] = None
    outcome_type: str = "automatic"
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# 4. AutoStorageEngine -- the main class
# ---------------------------------------------------------------------------


_CLASSIFICATION_THRESHOLD = 0.3   # minimum match count to qualify as category


class AutoStorageEngine:
    """Automatically captures significant outcomes after task/tool completion.

    Constructor takes a MemoryOrchestrator instance.
    Core method ``capture_outcome(text, outcome_type)`` analyses the text,
    filters trivial content, checks deduplication, and calls orchestrator.save()
    when appropriate.
    """

    def __init__(self, orchestrator: Any,
                 dedup_window_seconds: float = 30.0,
                 dedup_similarity_threshold: float = 0.6) -> None:
        """Initialize the storage engine.

        Args:
            orchestrator: MemoryOrchestrator instance (or any object with save/retrieve).
            dedup_window_seconds: Truncation period for duplicate detection (default 30 s).
            dedup_similarity_threshold: Jaccard similarity to trigger merge (default 0.6).
        """
        self.orchestrator = orchestrator
        self.dedup_window_seconds = dedup_window_seconds
        self.dedup_similarity_threshold = dedup_similarity_threshold

        # Sliding window of recent captures: [(timestamp, key, text), ...]
        self._recent_captures: List[tuple] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture_outcome(self, text: str, outcome_type: str = "automatic") -> Dict[str, Any]:
        """Analyse *text* for significant content and capture it.

        Returns a dict with keys: saved, key, significance, outcome_type, reason.
        """
        filtered_text = text.strip()

        # --- Length gate (AC-5) ---
        if len(filtered_text) < 20 or self._is_trivial(filtered_text):
            return {
                "saved": False,
                "key": None,
                "significance": None,
                "outcome_type": outcome_type,
                "reason": "below_significance_threshold",
            }

        # --- Significance detection (AC-4) ---
        category = self._detect_significance(filtered_text) or SignificanceCategory.RESULT

        key = self._generate_key(filtered_text, category)

        # --- Deduplication check (AC-6) ---
        dedup_key = self._check_dedup(key, filtered_text)
        if dedup_key is not None:
            return {
                "saved": True,
                "key": dedup_key,
                "significance": category.name.lower(),
                "outcome_type": outcome_type,
                "reason": "merged_into_existing",
            }

        # --- Persist (AC-2/3) ---
        payload = {
            "text": filtered_text,
            "outcome_type": outcome_type,
            "significance": str(category.name),
            "timestamp": time.time(),
            "category_name": self._safe_category_name(category),
        }

        saved = False
        try:
            if hasattr(self.orchestrator, "save"):
                saved = self.orchestrator.save(key, payload)
        except (AttributeError, TypeError):
            pass  # graceful failure — AC-7

        return {
            "saved": saved,
            "key": key,   # Always return the attempted key so callers can inspect what was tried
            "significance": category.name.lower(),
            "outcome_type": outcome_type,
            "reason": None if saved else "orchestrator_unavailable",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_trivial(text: str) -> bool:
        """Return True if text is meaningless — single word, or all trivial confirmations."""
        words = text.lower().split()
        # Fewer than 2 words → trivial
        if len(words) <= 1:
            return True
        # All words are in the trivial set → trivial
        _TRIVIAL_WORDS = frozenset({
            "ok", "done", "yep", "yeah", "yes", "no", "sure",
            "cool", "fine", "great", "kk",
        })
        if all(w in _TRIVIAL_WORDS for w in words):
            return True
        return False

    def _detect_significance(self, text: str) -> Optional[SignificanceCategory]:
        """Return the best-matching SignificanceCategory, or None."""
        best_cat: Optional[SignificanceCategory] = None
        best_score = 0.0
        for cat, patterns in _compiled.items():
            for regex, keyword in patterns:
                count = len(regex.findall(text))
                if count > best_score * _CLASSIFICATION_THRESHOLD:
                    best_score = count
                    best_cat = cat
        return best_cat

    @staticmethod
    def _generate_key(text: str, category: SignificanceCategory) -> str:
        """Create a content-hash key with category prefix."""
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        now = time.strftime("%Y%m%d")
        return "auto_{cat}_{dt}_{h}".format(
            cat=category.name.lower(), dt=now, h=h,
        )

    def _check_dedup(self, key: str, text: str) -> Optional[str]:
        """Return existing key if content is >60% similar to a recent capture."""
        now = time.time()
        cutoff = now - self.dedup_window_seconds

        # Prune old entries first
        self._recent_captures = [
            (t, k, c) for t, k, c in self._recent_captures if t > cutoff
        ]

        tokens_new = set(text.lower().split())
        for _timestamp, existing_key, existing_text in self._recent_captures:
            if _timestamp < cutoff:
                continue
            tokens_existing = set(existing_text.lower().split())
            if not tokens_existing or not tokens_new:
                continue
            jaccard_len = len(tokens_new & tokens_existing) / len(tokens_new | tokens_existing)
            if jaccard_len > self.dedup_similarity_threshold:
                return existing_key

        # Not a duplicate — record it
        self._recent_captures.append((now, key, text))
        return None

    @staticmethod
    def _safe_category_name(category: SignificanceCategory) -> str:
        for cat, _patterns in _CATEGORIES_AND_PATTERNS:
            if cat == category:
                return category.name
        return "RESULT"
