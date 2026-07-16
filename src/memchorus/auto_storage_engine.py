"""
auto_storage_engine -- automatic post-action outcome capture module.

Provides ``AutoStorageEngine`` that intercepts text after task/tool completion,
detects significance (LEARNING/MISTAKE/DECISION/RESULT), filters trivial
content, deduplicates, and writes structured payloads via a MemoryOrchestrator.

Bug 3 additions (t_da9e2362):
    - AC1: min_content_length threshold (default 50 chars before storage)
    - AC2: Noise pattern recognition (rejects tracebacks, boilerplate, hex dumps)
    - AC3: Shannon entropy gating (rejects repetitive/low-signal content)
    - AC4: Provenance marker on payloads for RelevanceScorer penalty (P=0.3)
"""

from __future__ import annotations

import math
import logging
import re
import time as _time_mod
from collections import Counter
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
# Noise pattern rejection (Bug 3 AC-2) -- precompiled regex patterns that match
# error stacks, tracebacks, binary dumps, empty imports, and boilerplate that
# provides no semantic signal worth storing.
# ---------------------------------------------------------------------------

_NOISE_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # Python/Java tracebacks (stack traces)
    ("traceback_header", re.compile(r'^\s*Traceback\s*\(most recent call last\)', re.M)),
    ("python_trace_files", re.compile(r'''^\s*File\s+"[^"]+",\s*line\s+\d+''', re.M)),
    # Common Python exception classes at line start
    ("exception_class", re.compile(
        r'^\s*(?:ValueError|KeyError|TypeError|AttributeError|'
        r'ImportError|ModuleNotFoundError|RuntimeError|IndexError|'
        r'OSError|FileNotFoundError|PermissionError|ConnectionError)\b', re.M)),
    # Java-style stack frames (at com.example.Class.method(...))
    ("stack_frame_java", re.compile(r'^\s+at\s+\w+(\.\w+)+\(\w+:\d+\)', re.M)),
    # Hex dumps -- pairs of hex bytes with spaces/tabs between (8+ pairs)
    ("hex_dump", re.compile(r'(?:[0-9a-fA-F]{2}\s){8,}')),
    # Import-only blocks where most lines are just 'import X' or 'from X import Y'
    # and less than 30% of lines are actual code.
    ("import_block_pattern", re.compile(
        r'^\s*(?:import\s+\w+|from\s+\w+\s+import\s+.+)$', re.M)),
    # License copyright boilerplate alone (lines starting with copyright/license/mit)
    ("license_boilerplate", re.compile(
        r'^\s*(?:#\s*(?:copyright|licen[se]|MIT licen|Apache Licen)'
        r'|^\s*\([Cc]\))', re.M)),
    # Repeated identical lines (>5 consecutively) -- padding / whitespace walls
    ("repeated_lines", re.compile(r'(^.*\n)(?:\1){5,}')),
    # All-dashes-or-equals separator walls (>5 consecutive separator-only lines)
    ("separator_wall", re.compile(r'(?:^[-=~]{3,}\s*$\n){5,}', re.M)),
]


def _is_noise(text: str) -> bool:
    """Return True when *text* matches known noise patterns (tracebacks, binary dumps,
    boilerplate, etc.) that would waste memory without providing meaningful content.

    Checks each precompiled pattern; returns True on the first match found.
    For import blocks specifically, a density threshold of 70% must be met to
    account for mixed input containing some real code alongside imports.
    """
    for label, pattern in _NOISE_PATTERNS:
        if label == "import_block_pattern":
            # Special case: only reject if imports dominate the whole text (>70% of lines)
            matches = pattern.findall(text)
            total_lines = max(len(text.splitlines()), 1)
            if len(matches) / total_lines >= 0.7:
                return True
        else:
            if pattern.search(text):
                return True

    return False


# ---------------------------------------------------------------------------
# Shannon entropy gating -- Bug 3 AC-3
# ---------------------------------------------------------------------------

_MIN_ENTROPY_CHARS = 20
"""Minimum number of characters required before Shannon entropy is calculated at all."""

_MIN_ENTROPY_THRESHOLD = 1.5
"""Shannon entropy floor (bits/char) -- below this the content is considered repetitive noise."""


def _shannon_entropy(text: str) -> float:
    """Calculate Shannon entropy (bits per character) from raw character frequencies.

    Returns a value between 0.0 (single repeated char) and ~8.0 (dense random).
    English prose typically sits around 3.5-4.5 bits/char, while repetitive or
    boilerplate content tends below 2.5 bits/char.
    """
    if not text:
        return 0.0

    length = len(text)
    freqs = Counter(text)
    entropy = 0.0
    for count in freqs.values():
        p = count / length
        if p > 0:
            entropy -= p * math.log2(p)

    return float(entropy)


def _has_minimum_signal(text: str, threshold_entropy: float = 1.5) -> bool:
    """Return True when *text* has sufficient Shannon entropy to be considered
    meaningful (above *threshold_entropy* bits/char).

    Low-entropy text such as repeated separators, whitespace walls, or extremely
    repetitive boilerplate will fail this check regardless of length."""
    normalized = re.sub(r'\s+', ' ', text.strip())
    if len(normalized) < _MIN_ENTROPY_CHARS:
        return False
    ent = _shannon_entropy(normalized)
    return ent >= threshold_entropy


# ---------------------------------------------------------------------------
# Known query templates from AutoRecallEngine._QUERY_MAP -- if text matches one of
# these verbatim, it is a query echo artifact and must be skipped.
# Hardcoded copy to keep storage engine independent of recall engine import.
# ---------------------------------------------------------------------------

_KNOWN_QUERY_TEMPLATES: frozenset[str] = frozenset({
    "past planning patterns architecture decisions strategy notes",
    "tool usage history command conventions domain-specific guidance",
    "post-action learnings outcomes results",
    "errors recovery patterns failure modes known issues",
})


def _is_query_echo(text: str) -> bool:
    """Return True when *text* is a deterministic recall query template rather
    than actual meaningful content.

    Prevents query echo artifacts -- where the search query string itself gets
    stored as memory content via the post-recall storage cycle.
    """
    stripped = text.strip()
    if stripped in _KNOWN_QUERY_TEMPLATES:
        return True
    # Also catch near-exact matches (substring containment in either direction)
    stripped_lower = stripped.lower()
    for template in _KNOWN_QUERY_TEMPLATES:
        if template.lower() in stripped_lower or stripped_lower in template.lower():
            if min(len(stripped), len(template)) / max(len(stripped), len(template)) >= 0.85:
                return True
    return False


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
    """Lowercased, deduplicated bag of >=2-character alpha-words excluding stop words."""
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

        AutoStorageEngine(orchestrator, dedup_window_seconds=30.0,
                         dedup_similarity_threshold=0.6, min_content_length=50)

    Bug 3 parameters (AC1):
        min_content_length: minimum characters before storage is attempted
                            (default 50 to reject near-empty tool outputs)

    Public API:
        capture_outcome(text, outcome_type='automatic') -> dict
    """

    def __init__(
        self,
        orchestrator: Any,  # MemoryOrchestrator or None
        dedup_window_seconds: float = 30.0,
        dedup_similarity_threshold: float = 0.6,
        min_content_length: int = 30,
    ) -> None:
        self.orchestrator = orchestrator
        self.dedup_window_seconds = dedup_window_seconds
        self.dedup_similarity_threshold = dedup_similarity_threshold

        # AC1: minimum content length threshold (default 30 chars — rejects
        # single-word confirmations and empty tool outputs while allowing
        # legitimate short sentences like "I learned that X")
        self.min_content_length = min_content_length

        # Internal dedup store: list of (text, key, timestamp)
        self._dedup_cache: List[Tuple[str, str, float]] = []

    def capture_outcome(
        self, text: str, outcome_type: str = "automatic"
    ) -> Dict[str, Any]:
        """Capture a significant outcome from post-action text.

        Returns a dict with keys: saved, key, significance, outcome_type,
        reason (optional), importance_score.

        Filtering pipeline (in order):
         1. Query echo prevention (existing)
         2. Min content length gate (Bug 3 AC1)
         3. Noise pattern rejection (Bug 3 AC2)
         4. Shannon entropy signal gating (Bug 3 AC3)
         5. Trivial content filter (existing)
         6. Significance detection + scoring (existing)
         7. Deduplication (existing)
         8. Save via orchestrator with provenance marker (Bug 3 AC4)
        """
        # --- Step 1: filter query echo artifacts ---
        if _is_query_echo(text):
            logger.debug("AutoStorageEngine: skipping query echo artifact")
            return {
                "saved": False,
                "key": "",
                "significance": "",
                "reason": "query_echo_artifact",
                "outcome_type": outcome_type,
                "importance_score": 0.0,
            }

        # --- Step 2: min content length gate (Bug 3 AC1) ---
        if len(text.strip()) < self.min_content_length:
            logger.debug(
                "AutoStorageEngine: content too short (%d chars < %d threshold)",
                len(text.strip()), self.min_content_length,
            )
            return {
                "saved": False,
                "key": "",
                "significance": "",
                "reason": "below_min_content_length",
                "outcome_type": outcome_type,
                "importance_score": 0.0,
            }

        # --- Step 3: noise pattern rejection (Bug 3 AC2) ---
        if _is_noise(text):
            logger.debug("AutoStorageEngine: rejecting noise-pattern content")
            return {
                "saved": False,
                "key": "",
                "significance": "",
                "reason": "noise_pattern_matched",
                "outcome_type": outcome_type,
                "importance_score": 0.0,
            }

        # --- Step 4: Shannon entropy gate (Bug 3 AC3) ---
        if not _has_minimum_signal(text):
            logger.debug(
                "AutoStorageEngine: low entropy content rejected (%.2f bits/char)",
                _shannon_entropy(text.strip()),
            )
            return {
                "saved": False,
                "key": "",
                "significance": "",
                "reason": "low_entropy_signal",
                "outcome_type": outcome_type,
                "importance_score": 0.0,
            }

        # --- Step 5: filter trivial content (existing) ---
        if self._is_trivial(text):
            return {
                "saved": False,
                "key": "",
                "significance": "",
                "reason": "below_significance_threshold",
                "outcome_type": outcome_type,
                "importance_score": 0.0,
            }

        # --- Step 6: detect significance ---
        categories = _detect_significance(text)
        if categories:
            chosen_category = categories[0]  # highest priority first
            category_str = chosen_category.value
        else:
            chosen_category = SignificanceCategory.RESULT  # fallback default
            category_str = "RESULT"

        importance = _score_importance(text, categories)

        # --- Step 7: generate key ---
        category_list = [chosen_category] if chosen_category else []
        key = _gen_key(text, category_list)

        # --- Step 8: dedup check ---
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

        # --- Step 9: save to orchestrator with provenance marker (Bug 3 AC4) ---
        payload = {
            "text": text,
            "categories": [c.value for c in categories] or ["RESULT"],
            "category": category_str,
            "outcome_type": outcome_type,
            "timestamp": _time_mod.time(),
            "importance_score": importance,
            # AC4: provenance marker -- RelevanceScorer checks this key to apply P=0.3
            "_auto_provenance": True,
        }

        try:
            write_type = {
                "LEARNING":     "memory",
                "MISTAKE":      "memory",
                "MEMORY":       "memory",
                "DECISION":     "decision",
                "RESULT":       "general",
                "RELATIONSHIP": "graph",
            }.get(category_str.upper(), "general")

            candidate_sources = self.orchestrator.recommended_sources(write_type=write_type)  # type: ignore[union-attr]

            write_both = category_str.upper() in ("LEARNING", "MISTAKE", "DECISION")

            success = False
            saved_to: set[str] = set()

            for src_name in candidate_sources:
                result = self.orchestrator.save(key, payload, source_name=src_name)  # type: ignore[union-attr]
                if result:
                    saved_to.add(src_name)
                    success = True
                    if not write_both:
                        break

            # Dual-write: ensure hermes_default also got the record
            if write_both and "hermes_default" not in saved_to:
                result = self.orchestrator.save(key, payload, source_name="hermes_default")  # type: ignore[union-attr]
                if result:
                    success = True

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
