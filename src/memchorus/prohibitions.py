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

from memchorus.hermes_home import hermes_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result Types
# ---------------------------------------------------------------------------


class GuardVerdict(Enum):
    """Whether a prohibition matched and what response it warrants."""
    OK            = "ok"            # green light - proceed normally
    WARNING       = "warning"       # severity 1-2 match; note in prompt but don't block
    BLOCK         = "block"         # severity 3 match; inject hard guard that stops the action


@dataclass
class GuardResult:
    """Outcome of running prohibition checks at a decision point."""

    verdict: GuardVerdict        = dc_field(default_factory=lambda: GuardVerdict.OK)
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
            "trigger_keywords": self.trigger_keywords,
            "tool_call_check": self.tool_call_check,
            "severity": self.severity,
            "block_action": self.block_action,
            "rationale": self.rationale,
            "source": self.source,
            "created": self.created,
            "type": self.type_,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Prohibition":
        """Deserialize from prohibitions.jsonl line."""
        return cls(
            id=data.get("id", ""),
            condition=data.get("condition", ""),
            trigger_keywords=data.get("trigger_keywords", []),
            tool_call_check=data.get("tool_call_check"),
            severity=data.get("severity", 3),
            block_action=data.get("block_action", ""),
            rationale=data.get("rationale", ""),
            source=data.get("source", "system"),
            created=data.get("created", ""),
            type_=data.get("type", "infrastructure"),
        )

    def _compile_patterns(self) -> None:
        """Pre-compile regex patterns from trigger_keywords and tool_call_check."""
        self._compiled_patterns = []
        if self.trigger_keywords:
            # Join keywords with OR, case-insensitive word-boundary matching
            escaped = [re.escape(kw) for kw in self.trigger_keywords]
            combined_pattern = r'\b(?:' + '|'.join(escaped) + r')\b'
            try:
                self._compiled_patterns.append(
                    re.compile(combined_pattern, re.IGNORECASE)
                )
            except re.error:
                logger.warning("Invalid regex from keywords in rule %s", self.id)

        if self.tool_call_check:
            try:
                self._compiled_patterns.append(
                    re.compile(self.tool_call_check, re.IGNORECASE)
                )
            except re.error:
                logger.warning(
                    "Invalid tool_call_check regex '%s' in rule %s",
                    self.tool_call_check, self.id,
                )


# ---------------------------------------------------------------------------
# Default Seed Rules
# ---------------------------------------------------------------------------


_DEFAULT_SEED_RULES: List[Dict[str, Any]] = [
    {
        "id": "guard-001-no-editable-install",
        "condition": "Never run pip install -e (editable install) inside ~/.hermes/ or any self-critical venv path. Always install from pushed GitHub commits only.",
        "trigger_keywords": ["pip install -e", "editable install", "pip install .", "-e ~/.hermes"],
        "tool_call_check": r"pip\s+install\s+(?:-e|-\\s*e)\s+.*hermes",
        "severity": 3,
        "block_action": "Editable installs into self-critical venvs that break the CLI on path shifts",
        "rationale": "Aug 17 2026 incident: editable .pth shim in self-hosted venv caused ModuleNotFoundError for hermes_cli after path shift. Broken CLI = total agent outage until manual fix.",
        "source": "system",
        "created": datetime.now(timezone.utc).isoformat(),
        "type": "infrastructure",
    },
    {
        "id": "guard-002-no-scratch-delete",
        "condition": "Never delete ~/.hermes or any parent directory of the active Hermes installation. This includes site-packages within self-hosted venvs.",
        "trigger_keywords": ["rm -rf ~/.hermes", "remove hermes dir", "delete site-packages", "unlink hermes"],
        "tool_call_check": r"(?:rm|rmdir|shutil\.rmtree|os\.remove)\s+.*(?:hermes|site.packages)",
        "severity": 3,
        "block_action": "Removing the agent's own installation directory or venv site-packages",
        "rationale": "Deleting ~/.hermes breaks the agent itself. There have been incidents where cleanup scripts removed the wrong path during debugging sessions.",
        "source": "system",
        "created": datetime.now(timezone.utc).isoformat(),
        "type": "infrastructure",
    },
    {
        "id": "guard-003-no-public-opsec-leak",
        "condition": "Never commit agent names, local filesystem paths, or personal identifiers to public-facing repos. Run full scrub before pushing.",
        "trigger_keywords": ["commit message with name", "local path in code", "personal identifier"],
        "tool_call_check": None,
        "severity": 2,
        "block_action": "Including agent names or local paths in public repository content",
        "rationale": "OPSEC is mission-critical. Public repos leaking internal identifiers or machine topology is permanently visible.",
        "source": "system",
        "created": datetime.now(timezone.utc).isoformat(),
        "type": "security",
    },
]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ProhibitionsManager:
    """Load, scan, persist behavioral prohibition rules.

    Responsible for:
    - File I/O against prohibitions.jsonl in the data directory
    - Seeding with default guards so a fresh install still has protections
    - Compiling regex patterns at load time (zero runtime cost)
    - Scanning arbitrary text (prompt input, tool call strings) and returning GuardResult
    """

    def __init__(self, data_dir: Optional[Path] = None):
        self._rules: List[Prohibition] = []
        self._data_dir = data_dir
        self._file_path: Optional[Path] = None

    @property
    def file_path(self) -> Path:
        """Resolve prohibitions.jsonl path. Returns data/memchorus/ subpath."""
        if self._file_path is not None:
            return self._file_path
        if self._data_dir:
            self._file_path = self._data_dir / "prohibitions.jsonl"
        else:
            # Default next to the package data directory if available, else use ~/.memchorus-data/
            base = hermes_home()
            candidate = base / "workspace" / "Code" / "MemChorus" / "data" / "prohibitions.jsonl"
            if not candidate.exists():
                fallback_dir = Path.home() / ".memchorus-data"
                fallback_dir.mkdir(exist_ok=True, parents=True)
                self._file_path = fallback_dir / "prohibitions.jsonl"
            else:
                self._file_path = candidate
        return self._file_path

    def load(self) -> int:
        """Load rules from disk. If file does not exist yet, seed with defaults.

        Returns the count of loaded/seeded rules so the caller knows we have something.
        """
        fpath = self.file_path
        fpath.parent.mkdir(parents=True, exist_ok=True)

        if fpath.exists():
            try:
                text = fpath.read_text(encoding="utf-8")
                for line in text.strip().splitlines():
                    if not line or line.startswith("#"):
                        continue
                    data = json.loads(line)
                    rule = Prohibition.from_dict(data)
                    rule._compile_patterns()
                    self._rules.append(rule)
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load prohibitions from %s: %s — seeding defaults", fpath, exc)
                self._seed_defaults()
        else:
            self._seed_defaults()

        return len(self._rules)

    def save(self) -> None:
        """Persist all rules back to disk as JSONL."""
        fpath = self.file_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r.to_dict()) for r in self._rules]
        fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- Scanning ---------------------------------------------------------

    def scan_text(self, text: str) -> GuardResult:
        """Scan arbitrary text against all loaded rules.

        Returns a GuardResult indicating whether any rule matched and at what severity.
        The highest severity across matches wins the verdict.
        """
        start = time.monotonic()
        result = GuardResult()
        for rule in self._rules:
            if rule.matches_text(text):
                result.matched_rules.append(rule)
        if result.matched_rules:
            max_sev = max(r.severity for r in result.matched_rules)
            if max_sev >= 3:
                result.verdict = GuardVerdict.BLOCK
            elif max_sev >= 2:
                result.verdict = GuardVerdict.WARNING

        elapsed_ms = (time.monotonic() - start) * 1000.0
        result.timing_ms = round(elapsed_ms, 3)
        return result

    def scan_tool_call(self, command: str, args: Optional[str] = None) -> GuardResult:
        """Specialised scan for shell commands / tool-call strings."""
        text = command
        if args:
            text = f"{command} {args}"
        return self.scan_text(text)

    # -- Rule Management --------------------------------------------------

    def add_rule(self, rule: Prohibition) -> None:
        """Add a new rule (after dedup check)."""
        for existing in self._rules:
            if existing.id == rule.id:
                logger.warning("Rule %s already exists — not adding duplicate", rule.id)
                return
        rule._compile_patterns()
        self._rules.append(rule)

    def remove_rule(self, rule_id: str) -> bool:
        """Remove a rule by ID. Returns True if found and removed."""
        for i, r in enumerate(self._rules):
            if r.id == rule_id:
                self._rules.pop(i)
                return True
        return False

    @property
    def rules(self) -> List[Prohibition]:
        """Read-only access to the rule list."""
        return list(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    # -- Private helpers --------------------------------------------------

    def _seed_defaults(self) -> None:
        """Populate with built-in seed rules and persist to disk."""
        for data in _DEFAULT_SEED_RULES:
            rule = Prohibition.from_dict(data)
            rule._compile_patterns()
            self._rules.append(rule)
        self.save()
        logger.info("Seeded %d default prohibition rules", len(self._rules))
