"""Feedback loop module for GH#101.

Replaces the stub _try_feedback_loop() that previously returned an empty list.
Corrections are stored per unique error fingerprint, matched against decision-point
trigger categories at recall time, and auto-expire after their exhaust TTL counter
reaches zero — archived to permanent memory and removed from the active feedback queue.

Each correction is a [[FEEDBACK CORRECTION]] block distinct from [MemChorus Memory Recall]
so agents can distinguish between generic context and targeted course-corrections.
"""

import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FeedbackCorrection:
    """Single correction entry with exhaustion tracking."""

    fingerprint: str
    context: str
    correction_text: str
    category: str
    exhaust_ttl: int
    current_ttl: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FeedbackCorrection":
        return cls(
            fingerprint=d["fingerprint"],
            context=d.get("context", ""),
            correction_text=d["correction_text"],
            category=d.get("category", ""),
            exhaust_ttl=d.get("exhaust_ttl", 3),
            current_ttl=d.get("current_ttl", d.get("exhaust_ttl", 3)),
        )

# ---------------------------------------------------------------------------
# Persistence store
# ---------------------------------------------------------------------------

class FeedbackPersistenceStore:
    """JSON-backed persistence for feedback corrections.

    One file, one list. Saves are additive (update existing fingerprint or append new).
    Archives remove the entry from the active queue so it won't fire again until
    re-discovered — the archived content lives permanently via whatever downstream
    recall path exists.

    Max ~20 entries to avoid noise in recall blocks (see AC: "one per unique fingerprint").
    """

    MAX_ENTRIES = 20

    def __init__(self, store_path: str):
        self.store_path = store_path

    def load(self) -> List[FeedbackCorrection]:
        path = Path(self.store_path)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [FeedbackCorrection.from_dict(item) for item in data]
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("feedback persistence: bad JSON at %s — resetting (%s)", self.store_path, e)
            return []

    def save(self, correction: FeedbackCorrection) -> bool:
        """Persist a correction. Returns True on success, False if content is rejected."""
        # Query echo guard: reject internal artifacts that look like tool output / state dumps
        from memchorus.auto_storage_engine import (
            _is_internal_artifact,
            _is_placeholder_artifact,
            _is_query_echo,
        )

        text = f"{correction.context} {correction.correction_text}"
        if _is_query_echo(text) or _is_internal_artifact(text) or _is_placeholder_artifact(text):
            logger.debug("feedback: rejected artifact fingerprint=%s (query echo / internal)", correction.fingerprint)
            return False

        corrections = self.load()
        existing = [c for c in corrections if c.fingerprint != correction.fingerprint]
        existing.append(correction)

        # Respect entry cap
        if len(existing) > self.MAX_ENTRIES:
            # Sort by current_ttl descending so most-expired entries drop first
            existing.sort(key=lambda c: c.current_ttl, reverse=True)
            existing = existing[: self.MAX_ENTRIES]
            if any(c.fingerprint == correction.fingerprint for c in existing):
                self._write(existing)
                return True
            logger.debug("feedback: fingerprint %s dropped (exceeded max entries)", correction.fingerprint)
            return False

        self._write(existing)
        return True

    def archive(self, fingerprint: str) -> bool:
        """Remove a correction from the active feedback queue."""
        corrections = self.load()
        remaining = [c for c in corrections if c.fingerprint != fingerprint]
        if len(remaining) < len(corrections):
            self._write(remaining)
            return True
        return False

    def decrement_ttl(self, fingerprint: str) -> int:
        """Decrement TTL counter. Returns new remaining count (0 means exhausted)."""
        corrections = self.load()
        for c in corrections:
            if c.fingerprint == fingerprint:
                c.current_ttl -= 1
                if c.current_ttl <= 0:
                    self.archive(fingerprint)
                    return 0
                self._write(corrections)
                return c.current_ttl
        return -1

    def _write(self, corrections: List[FeedbackCorrection]):
        data = [c.to_dict() for c in corrections]
        path = Path(self.store_path)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Feedback loop manager
# ---------------------------------------------------------------------------

_DECISION_KEYWORDS = {
    "dependency": ["pip", "install", "package", "importerror", "modulenotfounderror", "venv", "virtualenv", "site-packages"],
    "vcs": ["git", "branch", "merge", "conflict", "push", "commit", "force-push"],
    "build": ["build", "compile", "make", "cmake", "cargo", "gradle", "maven"],
    "config": ["config", "yaml", "toml", "json", "setting"],
}

_KEYWORD_MIN_MATCH = 2


class FeedbackLoopManager:
    """Decision-point feedback manager.

    Matches input text against stored corrections whose category matches the current
    trigger context. When a match fires, the TTL decrements and the correction is returned
    as a [[FEEDBACK CORRECTION]] block string.

    Exhaust entries (TTL=0) are archived so they stop firing.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        fb_config = (config or {}).get("feedback_loop", {})
        self.enabled = bool(fb_config.get("enabled", False))
        default_path = str(Path.home() / ".cache" / "memchorus_feedback_corrections.json")
        self._store = FeedbackPersistenceStore(fb_config.get("store_path", default_path))

    def process_feedback(self, input_text: str, kwargs: Dict[str, Any]) -> List[str]:
        """Check for relevant corrections and return feedback block strings."""
        if not self.enabled:
            return []

        trigger_category = kwargs.get("trigger_category", "")
        if not trigger_category:
            # Fallback: detect category from input text keywords
            trigger_category = self._infer_category(input_text)

        if not trigger_category:
            logger.debug("feedback: no trigger category — skipping")
            return []

        corrections = self._store.load()
        matches = self._match_corrections(corrections, input_text, trigger_category)

        blocks: List[str] = []
        for correction in matches:
            block = self._format_block(correction)
            blocks.append(block)
            remaining = self._store.decrement_ttl(correction.fingerprint)
            logger.info(
                "feedback: injected correction %s (remaining TTL=%d)",
                correction.fingerprint,
                remaining,
            )
            if remaining <= 0:
                logger.info("feedback: archived exhausted entry %s", correction.fingerprint)

        return blocks

    def _match_corrections(
        self, corrections: List[FeedbackCorrection], input_text: str, category: str
    ) -> List[FeedbackCorrection]:
        """Return stored corrections matching both category and semantic overlap with input."""
        matches = []
        input_lower = input_text.lower()
        keywords = _DECISION_KEYWORDS.get(category, [])

        # Require at least KEYWORD_MIN_MATCH overlapping terms between input and category keywords
        keyword_overlap = sum(1 for kw in keywords if kw.lower() in input_lower)
        if keyword_overlap < _KEYWORD_MIN_MATCH:
            return matches

        for correction in corrections:
            if not (correction.current_ttl > 0):
                continue
            # Category must overlap — exact or substring match
            if category.lower() not in correction.category.lower():
                continue
            # Correction context should share semantic signal with input
            if self._semantic_overlap(input_text, f"{correction.context} {correction.correction_text}") > 0.15:
                matches.append(correction)

        # Prefer freshest TTL first
        matches.sort(key=lambda c: c.current_ttl, reverse=True)
        return matches

    def _semantic_overlap(self, a: str, b: str) -> float:
        """Simple word-set Jaccard similarity for matching."""
        words_a = set(re.findall(r"\b\w+\b", a.lower()))
        words_b = set(re.findall(r"\b\w+\b", b.lower()))
        if not (words_a and words_b):
            return 0.0
        intersection = len(words_a & words_b)
        union = len(words_a | words_b)
        return intersection / union

    def _infer_category(self, input_text: str) -> Optional[str]:
        """Best-guess category based on keyword density in input text."""
        lower = input_text.lower()
        best_cat = None
        best_count = 0
        for cat, kws in _DECISION_KEYWORDS.items():
            count = sum(1 for kw in kws if kw.lower() in lower)
            if count > best_count:
                best_count = count
                best_cat = cat
        if best_count >= _KEYWORD_MIN_MATCH and best_cat:
            return best_cat
        return None

    def _format_block(self, correction: FeedbackCorrection) -> str:
        """Render a [[FEEDBACK CORRECTION]] markdown block."""
        block_lines = [
            "[[FEEDBACK CORRECTION]]",
            f"Category: {correction.category}",
            f"Fingerprint: {correction.fingerprint}",
            f"Original context: {correction.context}",
            f"Correction: {correction.correction_text}",
        ]
        return "\n".join(block_lines)
