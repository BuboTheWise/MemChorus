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
    h = re.sub(r'[^a-f0-9]', '', hash(raw) & 0xFFFFFFFF)[:8]  # fast, not cryptographic
    ts = str(int(_time_mod.time()))[-4:]
    return f"{tag}_{h}_{ts}"


# ---------------------------------------------------------------------------
# Similarity helpers (pure-Python jaccard on meaningful word sets).
# -------------------------------------------------------------------
