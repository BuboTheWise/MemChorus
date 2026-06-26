"""
BehavioralEnforcementManager: Wiring layer that chains trigger → recall + storage.

Plug the BehavioralTrigger, AutoRecallEngine and AutoStorageEngine into the
MemoryOrchestrator so memory ops fire automatically at decision points instead
of requiring manual agent calls.

This implements the "Mandatory pre-action recall with enforcement" and
"Mandatory post-action storage" requirements from MemChorus-Spec.md v1.0:

  BE-1: Constructor accepts MemoryOrchestrator (or None for degradation).
  BE-2: enforce(text) auto-fires through trigger -> recall -> storage pipeline.
  BE-3: Individual engines can be toggled on/off independently.
  BE-4: Graceful degradation when orchestrator is unavailable.
  BE-5: Returns structured EnforcementResult with per-step outcomes.

Dependencies: behavioral_trigger, auto_recall_engine, auto_storage_engine,
              orchestrator (MemoryOrchestrator).
"""

from __future__ import annotations

import logging
import time as _time_mod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from memchorus.behavioral_trigger import BehavioralTrigger, DetectedPoint  # type: ignore[import-not-found]
from memchorus.auto_recall_engine import AutoRecallEngine           # type: ignore[import-not-found]
from memchorus.auto_storage_engine import AutoStorageEngine         # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EnforcementResult — per-call summary
# ---------------------------------------------------------------------------

@dataclass
class EnforcementResult:
    """Return value of ``BehavioralEnforcementManager.enforce()``."""

    triggered_points: int                          # how many DPs were detected
    recall_context: List[Dict[str, Any]] = field(default_factory=list)  # union of recall hits
    storage_outcome: Optional[Dict[str, Any]] = None                # last capture result
    timing_ms: float = 0.0                           # total wall-clock time in ms
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# BehavioralEnforcementManager
# ---------------------------------------------------------------------------

class BehavioralEnforcementManager:
    """Wires trigger, recall and storage into a unified enforcement pipeline.

    Constructor::

        BehavioralEnforcementManager(
            orchestrator,          # MemoryOrchestrator instance (or None)
            trigger=None,         # BehavioralTrigger (auto-created if omitted)
            recall_engine=None,   # AutoRecallEngine   (auto-wired if omitted)
            storage_engine=None,  # AutoStorageEngine  (auto-wired if omitted)
        )

    Public API:
        enforce(text) -> EnforcementResult
        enable_recall(bool), enable_storage(bool)
    """

    def __init__(
        self,
        orchestrator: Any,
        trigger: Optional[BehavioralTrigger] = None,
        recall_engine: Optional[AutoRecallEngine] = None,
        storage_engine: Optional[AutoStorageEngine] = None,
    ) -> None:
        self._orchestrator = orchestrator

        # --- Auto-wire sub-components when not provided ---
        self._trigger = trigger or BehavioralTrigger()

        if recall_engine is None:
            recall_engine = AutoRecallEngine(
                orchestrator=orchestrator,
                trigger=self._trigger,
            )
        self._recall_engine = recall_engine

        if storage_engine is None:
            storage_engine = AutoStorageEngine(orchestrator=orchestrator)
        self._storage_engine = storage_engine

        # --- Feature toggles (all ON by default) ---
        self._recall_enabled = True
        self._storage_enabled = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enforce(self, text: str) -> EnforcementResult:
        """Run the full enforcement pipeline on *text*.

        1. Detect decision points via BehavioralTrigger.
        2. For each DP detected → query recall engine (if enabled).
        3. After recall cycle → attempt automatic storage capture (if enabled).

        Returns an ``EnforcementResult`` summarising what happened.
        """
        start = _time_mod.time()
        result = EnforcementResult(triggered_points=0)

        # --- Step 1: trigger detection ---
        try:
            detected = self._trigger.fire(text)
        except Exception as exc:
            logger.warning("Enforce: trigger fire failed: %s", exc)
            result.errors.append(f"trigger_failed: {exc}")
            detected = []

        result.triggered_points = len(detected)

        # --- Step 2: recall injection (per DP type) ---
        if self._recall_enabled and detected:
            seen_types: set = set()
            for point in detected:
                dp_type = point.type
                if dp_type.value not in seen_types:
                    try:
                        hits = self._recall_engine.on_decision_point(point)
                        result.recall_context.extend(hits)
                    except Exception as exc:
                        logger.warning("Enforce: recall failed for %s: %s", point.type, exc)
                        result.errors.append(f"recall_failed:{point.type}")
                    seen_types.add(dp_type.value)

        # --- Step 3: automatic storage capture ---
        if self._storage_enabled:
            try:
                storage_outcome = self._storage_engine.capture_outcome(text)
                result.storage_outcome = storage_outcome
            except Exception as exc:
                logger.warning("Enforce: storage capture failed: %s", exc)
                result.errors.append(f"storage_failed: {exc}")

        # --- Timing ---
        result.timing_ms = (_time_mod.time() - start) * 1000.0
        return result

    def enable_recall(self, enabled: bool = True) -> None:
        """Toggle recall injection on/off."""
        self._recall_enabled = bool(enabled)

    def enable_storage(self, enabled: bool = True) -> None:
        """Toggle automatic storage capture on/off."""
        self._storage_enabled = bool(enabled)

    @property
    def is_available(self) -> bool:
        return self._orchestrator is not None
