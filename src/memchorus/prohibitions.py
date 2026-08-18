"""Behavioral Prohibitions: Hard guardrails that fire before execution.

Soft recall injects optional context - this module injects BLOCKS that actually stop
self-breaking actions BEFORE they happen, not after the environment is already wrecked.

Key design principle: these guards have enforcement weight that soft recall deliberately
lacks (see hooks.py L223: "soft nudges, never hard overrides"). Prohibitions bridge that gap
by injecting into prompt space with [[double brackets]] markers that cannot be skimmed past."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result Types
# ---------------------------------------------------------------------------


@dataclass
class GuardVerdict(Enum):
    """Whether a prohibition matched and what response it warrants."""
    OK            = "ok"            # green light - proceed normally
    WARNING       = "warning"       # severity 1-2 match; note in prompt but don't block
    BLOCK         = "block"         # severity 3 match; inject hard guard that stops the action


@dataclass
class GuardResult:
    """Outcome of running prohibition checks at a decision point."""

    verdict: GuardVerdict        = GuardVerdict.OK
    matched_rules: List["Prohibition"] = dc_field(default_factory=list)
    timing_ms: float             = 0.0
    errors: List[str]            = dc_field(default_factory=list)

    @property
    def triggered(self) -> bool:
        return self.verdict != GuardVerdict.OK

    def inject_blocks(self, source_tag: str = "hermes_agent") -> List[str]:
        """Build [[BEHAVIORAL GUARD]] markdown blocks for prompt injection.

        Uses double brackets so they are visually distinct from [soft recall] blocks
        and cannot be skimmed past. Caps at 3 rules to prevent bloat (GAP024).
        """
        if not self.triggered:
            return []

        blocks: List[str] = []
        for rule in self.matched_rules[:3]:
            guard_text = f"""[[BEHAVIORAL GUARD: {rule.id}]]

**RULE:** {rule.condition}
**BLOCKS:** {rule.block_action or 'the indicated action'}
**WHY:** {rule.rationale or 'prevents repeated self-breaking behavior'}
**SEVERITY:** {'HIGH' if rule.severity == 3 else 'MEDIUM' if rule.severity == 2 else 'LOW'}

> This is a hard guardrail based on past mistakes. Do NOT override it without explicit human confirmation.
"""
            blocks.append(guard_text)

        return blocks


# ---------------------------------------------------------------------------
# Single Prohibition Rule
# ---------------------------------------------------------------------------


@dataclass
class Prohibition:
    """One behavioral guard rule, loaded from prohibitions.jsonl or distilled from a mistake."""

    id: str                          # stable UUID for deduplication / updates
    condition: str                   # the actual rule statement (injected into prompt)
    trigger_keywords: List[str] = dc_field(default_factory=list)  # fast-path match tokens
    tool_call_check: Optional[str] = None  # compiled as regex at load time
    severity: int = 3               # 1=note, 2=warning, 3=hard block
    block_action: str = ""          # what it blocks (human-readable for prompt)
    rationale: str = ""             # why this rule exists (critical - agents ignore rules without reasoning)
    source: str = "system"          # origin label: system | distilled-from-mistake | manual
    created: str = ""               # ISO-8601 timestamp
    type_: str = "infrastructure"   # category tag

    # internal caches (not persisted to disk)
    _compiled_patterns: List[re.Pattern[str]] = dc_field(default_factory=list, repr=False)

    def matches_text(self, text: str) -> bool:
        """Fast O(1) pattern match against pre-compiled regexes.

        This is the hot path - every guard check runs this. Compiled at load time so runtime
        matching has zero compilation cost (GAP107 performance budget < 1ms per check).
        """
        if not self._compiled_patterns:
            return False
        for pat in self._compiled_patterns:
            if pat.search(text):
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize back to prohibitions.jsonl format."""
        return {
            "id": self.id,
            "condition": self.condition,
            "trigger_keywords": 