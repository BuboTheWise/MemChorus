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

    # Patterns that mean the mistake was non-destructive (don't promote these)
    NONDESTRUCTIVE_KEYWORDS: List[str] = [