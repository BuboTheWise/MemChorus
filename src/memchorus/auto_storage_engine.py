"""
auto_storage_engine -- automatic post-action outcome capture module.

Provides ``AutoStorageEngine`` that intercepts text after task/tool completion,
detects significance (LEARNING/MISTAKE/DECISION/RESULT), filters trivial
content, deduplicates, and writes structured payloads via a MemoryOrchestrator.
"""

from __future__ import annotations

import logging
import re
import time as _time_mod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and constants
# ---------------------------------------------------------------------------


class SignificanceCategory(str, Enum):
    """Significance class detected from text keywords."""

    LEARNING = "LEARNING"
    MISTAKE = "MISTAKE"
    DECISION = "DECISION"
    RESULT = "RESULT"


ALL_CATEGORIES: List[SignificanceCategory] = [
    SignificanceCategory.LEARNING,
    SignificanceCategory.MISTAKE,
    SignificanceCategory.DECISION,
    SignificanceCategory.RESULT,
]

# Mapping of category -> search strings (case-insensitive)
_SIG_KEYWORDS: Dict[SignificanceCategory, List[Tuple[str, str]]] = {
    SignificanceCategory.LEARNING: [
        ("learned", r'\blearned\b'),
        ("realized", r'\brealized\b'),
        ("understood", r'\bunderstood\b'),
        ("found that", r'found\s+that'),
    ],
    SignificanceCategory.MISTAKE: [
        ("went wrong", r'went\s+wrong'),
        ("wrong approach", r'wrong\s+approach'),
        ("should have", r'should\s+have\b'),
        ("mistake was", r'mistake\s+was\b'),
        ("incorrectly", r'\bincorrectly\b'),
    ],
    SignificanceCategory.DECISION: [
        ("decided", r'\bdecided\b'),
        ("chose", r'\bchose\b'),
        ("go with", r'go\s+with'),
        ("settled on", r'settled\s+on'),
    ],
    SignificanceCategory.RESULT: [
        ("result", r'\bresult\b'),
        ("outcome", r'\boutcome\b'),
        ("achieved", r'\bachieved\b'),
        ("success", r'\bsuccess(?!ful)?\b'),
    ],
}


# Minimal stop-set for similarity comparison (pure-English frequent words).
_STOP_WORDS: Set[str] = frozenset(
    "a an the was were be been being have has had do does did may might can"
    " could will would shall should of in on at to for with by from or if i "
    "me my we our us it its that this these those which who whom but also than "
    "how when where why what not no so yet each every neither".split()
)

# Patterns used by _is_trivial() -- precompiled once.
_TRIVIAL_PATTERNS: List[re.Pattern] = [
    re.compile(r'\b(omg|ok|yep)\s*$', re.I),
    re.compile(r'^omg\b', re.I),
]

# Single-word confirmations that are trivial when there's little else to say.
_TRIVIAL_WORDS: frozenset[str] = frozenset({"ok", "done", "yep", "yeah", "omg"})


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class CaptureResult:
    """Return value of ``AutoStorageEngine.capture_outcome()``."""

    saved: bool
    key: str
    significance: str  # enum-value string e.g. "LEARNING"
    reason: Optional[str] = None
    outcome_type: str = "automatic"
    importance_score: float = 0.0


# ---------------------------------------------------------------------------
# Significance detection
# ---------------------------------------------------------------------------


def _detect_significance(text: str) -> List[SignificanceCategory]:
    """Scan *text* for keywords across all categories; return matched list."""
    lower: str = text.lower()
    out: List[SignificanceCategory] = []
    for cat, entries in _SIG_KEYWORDS.items():
        for label, pattern in entries:
            if re.search(pattern, lower):
                out.append(cat)
                break  # one match per category is enough
    return out


# ---------------------------------------------------------------------------
# Importance scoring
# ---------------------------------------------------------------------------


def _score_importance(text: str, categories: List[SignificanceCategory]) -> float:
    """Rough 0-1 importance score.  More matching categories & longer text = higher."""
    if not categories:
        return 0.0
    # Base = fraction of matched categories out of total.
    base = len(categories) / len(ALL_CATEGORIES)
    # Length bonus up to 0.3 (diminishing at 50 tokens).
    words = text.split()
    length_bonus = min(len(words) / 150.0, 0.3)
    return min(base + length_bonus, 1.0)


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def _gen_key(text: str, categories: List[SignificanceCategory]) -> str:
    """Content-hash key with category tags and short timestamp."""
    tag = "|".join(c.value for c in (categories or [SignificanceCategory.LEARNING]))
    raw = f"{tag}:{text.strip()[:200]}"
    h = format(hash(raw) & 0xFFFFFFFF, 'x')[:8]  # fast, not cryptographic
    ts = str(int(_time_mod.time()))[-4:]
    return f"{tag}_{h}_{ts}"


# ---------------------------------------------------------------------------
# Similarity helpers (pure-Python Jaccard on meaningful-word bags).
# ---------------------------------------------------------------------------


def _text_to_bag(text: str) -> Set[str]:
    """Lowercased, deduplicated bag of ≥2-character alpha-words excluding stop words."""
    return {w.lower() for w in re.findall(r'[a-z]{2,}', text) if w not in _STOP_WORDS}


def _jaccard_similarity(a: str, b: str) -> float:
    """Return Jaccard similarity of meaningful-word bags between *a* and *b*.

    Returns 0.0 when either input produces an empty bag.
    """
    bag_a = _text_to_bag(a)
    bag_b = _text_to_bag(b)
    if not bag_a or not bag_b:
        return 0.0
    intersection = len(bag_a & bag_b)
    union = len(bag_a | bag_b)
    return intersection / union


# ---------------------------------------------------------------------------
# AutoStorageEngine
# ---------------------------------------------------------------------------


class AutoStorageEngine:
    """Automatically captures significant outcomes via a MemoryOrchestrator.

    Constructor::

        AutoStorageEngine(orchestrator, dedup_window_seconds=30.0, dedup_similarity_threshold=0.6)

    Public API:
        capture_outcome(text, outcome_type='automatic') -> dict
    """

    def __init__(
        self,
        orchestrator: Any,  # MemoryOrchestrator or None
        dedup_window_seconds: float = 30.0,
        dedup_similarity_threshold: float = 0.6,
    ) -> None:
        self.orchestrator = orchestrator
        self.dedup_window_seconds = dedup_window_seconds
        self.dedup_similarity_threshold = dedup_similarity_threshold

        # Internal dedup store: list of (text, key, timestamp)
        self._dedup_cache: List[Tuple[str, str, float]] = []

    def capture_outcome(
        self, text: str, outcome_type: str = "automatic"
    ) -> Dict[str, Any]:
        """Capture a significant outcome from post-action text.

        Returns a dict with keys: saved, key, significance, outcome_type,
        reason (optional), importance_score.
        """
        # --- Step 1: filter trivial content ---
        if self._is_trivial(text):
            return {
                "saved": False,
                "key": "",
                "significance": "",
                "reason": "below_significance_threshold",
                "outcome_type": outcome_type,
                "importance_score": 0.0,
            }

        # --- Step 2: detect significance ---
        categories = _detect_significance(text)
        if categories:
            chosen_category = categories[0]  # highest priority first
            category_str = chosen_category.value
        else:
            chosen_category = SignificanceCategory.RESULT  # fallback default
            category_str = "RESULT"

        importance = _score_importance(text, categories)

        # --- Step 3: generate key ---
        category_list = [chosen_category] if chosen_category else []
        key = _gen_key(text, category_list)

        # --- Step 4: dedup check ---
        merged_result = self._check_dedup(text)
        if merged_result is not None:
            return {
                "saved": True,
                "key": merged_result["key"],
                "significance": category_str,
                "reason": "merged_into_existing",
                "outcome_type": outcome_type,
                "importance_score": importance,
            }

        # --- Step 5: save to orchestrator ---
        payload = {
            "text": text,
            "categories": [c.value for c in categories] or ["RESULT"],
            "category": category_str,
            "outcome_type": outcome_type,
            "timestamp": _time_mod.time(),
            "importance_score": importance,
        }

        try:
            # B-2 fix (t_b9205369): route through recommended_sources() so that
            # enabled gating, priority tiering, and write restrictions are honoured.
            # Map every SignificanceCategory to a write_type token the orchestrator
            # understands. LEARNING / MISTAKE -> "memory" ensures they route to
            # memory-specialised sources rather than falling through to generic.
            write_type = {
                "LEARNING":     "memory",
                "MISTAKE":      "memory",
                "MEMORY":       "memory",
                "DECISION":     "decision",
                "RESULT":       "general",
                "RELATIONSHIP": "graph",
            }.get(category_str.upper(), "general")

            candidate_sources = self.orchestrator.recommended_sources(write_type=write_type)  # type: ignore[union-attr]

            success = False
            for src_name in candidate_sources:
                result = self.orchestrator.save(key, payload, source_name=src_name)  # type: ignore[union-attr]
                if result:
                    success = True
                    break

        except Exception as exc:
            logger.warning("AutoStorageEngine: orchestrator save failed: %s", exc)
            success = False

        # Always add to dedup cache (even if orchestrator fail) so future dedup works
        self._dedup_cache.append((text, key, _time_mod.time()))

        return {
            "saved": success,
            "key": key,
            "significance": category_str,
            "outcome_type": outcome_type,
            "importance_score": importance,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_dedup(self, text: str) -> Optional[Dict[str, str]]:
        """Return existing entry if *text* is too similar to recent content.

        Returns ``{"key": <existing_key>}`` when dedup matches, else None.
        """
        now = _time_mod.time()

        # Remove expired entries first
        self._dedup_cache = [  # type: ignore[assignment]
            (t, k, ts)
            for t, k, ts in self._dedup_cache
            if now - ts <= self.dedup_window_seconds
        ]

        # Check similarity against remaining entries
        for stored_text, stored_key, _ts in self._dedup_cache:
            ratio = _jaccard_similarity(text, stored_text)  # type: ignore[union-attr]
            if ratio >= self.dedup_similarity_threshold:
                return {"key": stored_key}

        return None

    def _is_trivial(self, text: str) -> bool:
        """Return True when *text* is short or contains only trivial word confirmations."""
        # Short text (< 20 characters)
        if len(text.strip()) < 20:
            return True

        text_lower = text.lower()

        # Pre-compiled confirmation patterns (ok/yep/omg at string boundary)
        for pattern in _TRIVIAL_PATTERNS:
            if pattern.search(text):
                return True

        # Standalone trivial words when paired with little to no substantive content
        for word in _TRIVIAL_WORDS:
            if re.search(rf"\b{word}\b", text_lower):
                # Remove stop-words and the matched trivial word from meaningful set
                meaningful = {w for w in re.findall("[a-z]{2,}", text_lower)
                              if w not in _STOP_WORDS and w != word.lower()}
                if len(meaningful) <= 2:
                    return True

        return False

