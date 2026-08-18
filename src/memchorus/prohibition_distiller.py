"""Prohibition Distillation: Convert detected mistakes into hard behavioral guards.

When a stored mistake involves self-breaking behavior (environment corruption, data loss,
toolchain breakage), this module automatically creates a new prohibition record from the
pattern instead of just storing raw error text that gets overlooked later.

This is entry point #2 in the prohibitions architecture - after a critical failure, MemChorus
distills it into an enforceable rule rather than another soft-recall fragment.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity Classification
# ---------------------------------------------------------------------------


class MistakeSeverity(Enum):
    """How bad the mistake was - determines prohibition urgency."""
    CRITICAL  = 3   # self-destructs environment / loses data / breaks core toolchain
    MEDIUM    = 2   # causes wasted cycles but not destructive
    LOW       = 1   # quality-of-life degradation (e.g. poor recall, duplicate storage)


# ---------------------------------------------------------------------------
# Distillation Config
# ---------------------------------------------------------------------------


@dataclass
class DistillationConfig:
    """Controls what mistakes get upgraded to prohibitions."""

    minimum_severity: int = 3          # only CRITICAL mistakes become rules by default
    max_rules_per_session: int = 2     # prevent guard explosion; cap fresh guards per cycle
    cooldown_hours: float = 24.0       # don't create a guard for the same pattern twice in X hours


# ---------------------------------------------------------------------------
# The Distiller itself
# ---------------------------------------------------------------------------


class ProhibitionDistiller:
    """Takes mistake text + metadata and emits new prohibition rules when appropriate."""

    # Patterns that indicate self-breaking behavior worth preventing next time
    SELFBREAK_KEYWORDS: List[str] = [
        "modulenotfounderror",
        "broken editable install",
        ".pth shim file",
        "site-packages corrupted",
        "venv damaged",
        "hermes broken",
        "cli crashed",
        "environment pollution",
        "pip install -e",
        "editable install broke",
        "source path deleted",
    ]

    # Patterns that mean the mistake was non-destructive (do not promote these)
    NONDESTRUCTIVE_KEYWORDS: List[str] = [
        "recall returned nothing useful",
        "no matching memories",
        "search returned empty",
        "failed to find relevant context",
        "orientation returned no results",
        "cache miss",
        "empty response",
    ]

    def __init__(self, config: Optional[DistillationConfig] = None):
        self.config = config or DistillationConfig()
        self._rules_created_this_session: int = 0
        self._recent_pattern_hashes: Dict[str, float] = {}  # hash -> timestamp

    # ------------------------------------------------------------------
    # Classification gate
    # ------------------------------------------------------------------

    @staticmethod
    def _text_lower(text: str) -> str:
        """Lowercase helper for keyword matching."""
        return text.lower() if isinstance(text, str) else ""

    @classmethod
    def is_worthy_of_guard(cls, error_text: str) -> MistakeSeverity:
        """Classify whether the given error/mistake text deserves a hard guard.

        Returns:
            MistakeSeverity.CRITICAL (3)   if self-breaking behavior detected
            MistakeSeverity.NON_WORTHY equivalent to LOW    if clearly non-destructive
            MistakeSeverity.LOW (1)         for everything else

        The acceptance criteria require:
         - 'ModuleNotFoundError hermes_cli' -> CRITICAL
         - 'recall returned nothing useful' -> NON_WORTHY (mapped to LOW)
        """
        lower = cls._text_lower(error_text)
        if not lower.strip():
            return MistakeSeverity.LOW

        # Check for non-destructive patterns first — these get excluded early
        for kw in cls.NONDESTRUCTIVE_KEYWORDS:
            if kw.lower() in lower:
                logger.debug(
                    "distiller: '%s' matched NONDESTRUCTIVE keyword %r — not worthy",
                    error_text[:80], kw,
                )
                return MistakeSeverity.LOW

        # Check for critical self-breaking patterns
        for kw in cls.SELFBREAK_KEYWORDS:
            if kw.lower() in lower:
                logger.info(
                    "distiller: '%s' matched SELFBREAK keyword %r — CRITICAL, worthy of guard",
                    error_text[:80], kw,
                )
                return MistakeSeverity.CRITICAL

        # Unknown content — default to not worthy (conservative)
        return MistakeSeverity.LOW

    # ------------------------------------------------------------------
    # Distillation engine
    # ------------------------------------------------------------------

    def distill(self, error_text: str, context: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Attempt to distill a prohibition rule from the given error text.

        Returns:
            A dict matching Prohibition.from_dict() schema if the mistake is worthy
            and passes all gates (severity threshold, session limit, cooldown).
            None if the mistake should not become a guard.

        The returned dict contains all required fields:
            id, condition, trigger_keywords, severity, rationale,
            source, created, type, tool_call_check, block_action
        """
        # Gate 1: severity classification
        severity = self.is_worthy_of_guard(error_text)
        if severity.value < self.config.minimum_severity:
            logger.debug(
                "distiller: severity %d below minimum %d — skipping distillation",
                severity.value, self.config.minimum_severity,
            )
            return None

        # Gate 2: session-level rule cap (prevent guard explosion)
        if self._rules_created_this_session >= self.config.max_rules_per_session:
            logger.warning(
                "distiller: already created %d rules this session (cap=%d) — skipping",
                self._rules_created_this_session,
                self.config.max_rules_per_session,
            )
            return None

        # Gate 3: cooldown check (do not re-create a guard for the same pattern within N hours)
        if self._is_on_cooldown(error_text):
            logger.debug("distiller: similar guard created recently — cooldown active, skipping")
            return None

        # Extract trigger keywords from the error text itself
        trigger_keywords = self._extract_keywords(error_text)
        if not trigger_keywords:
            logger.debug("distiller: no extractable keywords from '%s' — cannot distill", error_text[:80])
            return None

        # Build the prohibition dict
        rule_id = f"distilled-{uuid.uuid4().hex[:12]}"
        condition = self._build_condition(error_text, trigger_keywords)
        rationale = self._build_rationale(error_text, context)
        block_action = self._build_block_action(condition)

        now_iso = datetime.now(timezone.utc).isoformat()

        result: Dict[str, Any] = {
            "id": rule_id,
            "condition": condition,
            "trigger_keywords": trigger_keywords[:5],  # cap at 5 keywords max
            "tool_call_check": self._build_tool_call_regex(trigger_keywords),
            "severity": severity.value,
            "block_action": block_action,
            "rationale": rationale,
            "source": "distilled-from-mistake",
            "created": now_iso,
            "type": "infrastructure",
        }

        # Record this creation for session tracking + cooldown
        self._rules_created_this_session += 1
        self._record_cooldown(error_text)

        logger.info(
            "distiller: created new prohibition rule %s from error (severity=%d, keywords=%d)",
            rule_id, severity.value, len(trigger_keywords),
        )

        return result

    # ------------------------------------------------------------------
    # Keyword extraction helpers
    # ------------------------------------------------------------------

    @classmethod
    def _extract_keywords(cls, error_text: str) -> List[str]:
        """Extract meaningful trigger keywords from error text.

        Pulls out the self-break keywords that matched and any error type markers.
        """
        lower = cls._text_lower(error_text)
        keywords: List[str] = []

        # First, grab any matching self-break keywords (high-signal)
        for kw in cls.SELFBREAK_KEYWORDS:
            if kw.lower() in lower and kw not in keywords:
                keywords.append(kw)

        # If no direct keyword match, try to extract error-class tokens
        if not keywords:
            # Look for common error identifiers like "ModuleNotFoundError", "ImportError" etc.
            error_patterns = [
                r'\b(ModuleNotFoundError|ImportError|FileNotFoundError|PermissionError)\b',
                r'\b(CommandNotFound|NoCommandError)\b',
            ]
            for pat in error_patterns:
                matches = re.findall(pat, error_text, re.IGNORECASE)
                for m in matches:
                    if m not in keywords:
                        keywords.append(m)

        # Fallback: extract any multi-word phrases that look like actionable patterns
        if not keywords and len(error_text.strip().split()) <= 12:
            # If the error text itself is short enough, use trimmed snippets as keywords
            words = error_text.strip().split()[:4]
            keyword_phrase = " ".join(words)
            if len(keyword_phrase) >= 8:
                keywords.append(keyword_phrase.lower())

        return keywords

    @classmethod
    def _build_condition(cls, error_text: str, trigger_keywords: List[str]) -> str:
        """Build the human-readable condition statement for the prohibition rule.

        Turns the error text into a 'Never do X because Y happened' format.
        """
        sanitized = re.sub(r'\s+', ' ', error_text.strip())[:200]
        keyword_summary = " / ".join(trigger_keywords[:3])

        # Build condition from the most significant keywords if available
        for kw in trigger_keywords:
            lower_kw = kw.lower()
            if "pip install" in lower_kw or "editable" in lower_kw:
                return (
                    f"Never run editable installs or pip install -e into self-critical paths. "
                    f"Past error ({keyword_summary}): {sanitized[:100]}"
                )
            if "modulenotfounderror" in lower_kw or "importerror" in lower_kw:
                return (
                    f"Do not delete or corrupt source paths that cause ModuleNotFoundError. "
                    f"Past error ({keyword_summary}): {sanitized[:100]}"
                )
            if "site-packages" in lower_kw or "venv" in lower_kw:
                return (
                    f"Protect venv site-packages from accidental deletion or corruption. "
                    f"Past error ({keyword_summary}): {sanitized[:100]}"
                )

        # Generic condition builder when no specific pattern matched
        return f"Refrain from actions that trigger '{keyword_summary}'. Past error: {sanitized[:100]}"

    @classmethod
    def _build_rationale(cls, error_text: str, context: Optional[str]) -> str:
        """Build the rationale explaining WHY this rule exists.

        Agents ignore rules without reasoning — the rationale field is critical.
        """
        sanitized = re.sub(r'\s+', ' ', error_text.strip())[:150]
        rationale_parts = [
            f"Auto-generated from observed failure: {sanitized}"
        ]
        if context:
            ctx_clean = re.sub(r'\s+', ' ', str(context).strip())[:100]
            rationale_parts.append(f"Context: {ctx_clean}")
        rationale_parts.append(
            "This pattern caused self-breaking behavior that was difficult to diagnose. "
            "Prevent recurrence by blocking the triggering action before execution."
        )
        return ". ".join(rationale_parts)

    @classmethod
    def _build_block_action(cls, condition: str) -> str:
        """Build the block_action summary from the condition text."""
        # Extract the core action being blocked for the prompt injection display
        if "Never run" in condition or "Do not" in condition or "Never delete" in condition:
            return f"Blocking the action described in this guard"
        return "Recurring self-breaking behavior that caused past failures"

    @classmethod
    def _build_tool_call_regex(cls, trigger_keywords: List[str]) -> Optional[str]:
        """Try to produce a tool_call_check regex from extracted keywords.

        Returns None if we cannot derive a safe pattern.
        """
        if not trigger_keywords:
            return None

        # Escape for safe regex usage, join with OR
        escaped = [re.escape(kw) for kw in trigger_keywords]
        try:
            combined = r'(?:' + '|'.join(escaped) + r')'
            # Validate it compiles
            re.compile(combined, re.IGNORECASE)
            return combined
        except re.error:
            logger.warning("distiller: combined keyword regex failed to compile")
            return None

    # ------------------------------------------------------------------
    # Cooldown tracking
    # ------------------------------------------------------------------

    def _is_on_cooldown(self, error_text: str) -> bool:
        """Check if a similar error was recently distilled (within cooldown window)."""
        content_hash = _hash_error_fingerprint(error_text)
        now = time_now_float()

        for hash_key, ts in self._recent_pattern_hashes.items():
            if hash_key == content_hash:
                elapsed_hours = (now - ts) / 3600.0
                if elapsed_hours < self.config.cooldown_hours:
                    return True
            # Clean up old entries during the check
        _cleanup_old_hashes(self._recent_pattern_hashes, self.config.cooldown_hours * 2)
        return False

    def _record_cooldown(self, error_text: str) -> None:
        """Record a new pattern hash for cooldown tracking."""
        content_hash = _hash_error_fingerprint(error_text)
        self._recent_pattern_hashes[content_hash] = time_now_float()


# ---------------------------------------------------------------------------
# Utility helpers (module level)
# ---------------------------------------------------------------------------

import hashlib
import time as _time

def time_now_float() -> float:
    """Current UTC timestamp as float for cooldown tracking."""
    return _time.time()


def _hash_error_fingerprint(error_text: str) -> str:
    """Generate a stable fingerprint hash for an error message.

    Uses normalized lowercased text so minor wording differences still group together.
    """
    normalized = re.sub(r'\s+', ' ', error_text.strip().lower())[:200]
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


def _cleanup_old_hashes(hashes: Dict[str, float], max_age_hours: float) -> None:
    """Remove expired cooldown entries to prevent unbounded growth."""
    cutoff = time_now_float() - (max_age_hours * 3600.0)
    stale_keys = [k for k, ts in hashes.items() if ts < cutoff]
    for k in stale_keys:
        del hashes[k]
