"""Lifecycle Merge Engine — merge existing content before overwriting it."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- 
# Token helpers for Jaccard similarity
# --------------------------------------------------------------------------- 

def _tokenize(text: str) -> Set[str]:
    """Tokenise text into a lowercase set of words."""
    return set(re.findall(r'\w+', str(text).lower()))


def _jaccard_similarity(a: Any, b: Any) -> float:
    """Jaccard similarity between two values (token-based for strings; dict-keys for dicts)."""
    if isinstance(a, dict) and isinstance(b, dict):
        set_a = set(a.keys())
        set_b = set(b.keys())
        union = set_a | set_b
        return len(set_a & set_b) / len(union) if union else 1.0

    tokens_a = _tokenize(str(a))
    tokens_b = _tokenize(str(b))
    union = tokens_a | tokens_b
    return len(tokens_a & tokens_b) / len(union) if union else 1.0


# --------------------------------------------------------------------------- 
# Audit actions for merge engine logging
# ---------------------------------------------------------------------------

class AuditAction(str, Enum):
    PRE_SAVE_CHECK = "pre_save_check"
    MERGE_OVERWRITE = "merge_overwrite"
    MERGE_APPEND = "merge_append"
    MERGE_UNION = "merge_union"
    PASSTHROUGH_NEW_KEY = "pass_through_new_key"
    DEGRADE_SAFETY_NET = "degrade_safety_net"


# --------------------------------------------------------------------------- 
# Strategy implementations
# ---------------------------------------------------------------------------

def _strategy_overwrite(existing: Any, new_value: Any) -> Tuple[Any, AuditAction]:
    """Overwrite: replace existing content entirely."""
    return new_value, AuditAction.MERGE_OVERWRITE


def _strategy_append(existing: Any, new_value: Any) -> Tuple[Any, AuditAction]:
    """Append: combine values into a list, preserving both old and new."""
    if isinstance(existing, list):
        result = list(existing)
    else:
        result = [existing]
    result.append(new_value)
    return result, AuditAction.MERGE_APPEND


def _strategy_union(existing: Any, new_value: Any) -> Tuple[Any, AuditAction]:
    """Union: merge two dicts together (new overrides common keys)."""
    if isinstance(existing, dict) and isinstance(new_value, dict):
        merged = dict(existing)
        merged.update(new_value)
        return merged, AuditAction.MERGE_UNION
    # Fall back to overwrite when values are not dicts
    return new_value, AuditAction.MERGE_UNION


# --------------------------------------------------------------------------- 
# Merge result dataclass  
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    """Result of a pre_save_check decision."""
    should_proceed: bool = True
    final_value: Any = None
    merge_action: Optional[AuditAction] = None
    similar_found: int = 0
    _audit_data: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- 
# Merge engine class
# ---------------------------------------------------------------------------

class MergeEngine:
    """Lifecycle merge engine that intercepts saves and merges with existing content.
    
    Config schema (from orchestrator config.merge_at_write):
        enabled: bool  (default True)
        strategy: "overwrite" | "append" | "union"  (default "overwrite")
        eviction.similarity_min: float  (default 0.3)
        eviction.duplicate_cluster_max: int  (default 5) -- min hits to trigger merge
    """

    STRATEGIES = {
        "overwrite": _strategy_overwrite,
        "append": _strategy_append,
        "union": _strategy_union,
    }

    def __init__(self, orchestrator: Any, config: Dict[str, Any]):
        self._orchestrator = orchestrator
        raw_eviction = config.get("eviction", {}) or {}
        self._similarity_min = float(raw_eviction.get("similarity_min", 0.3))
        self._cluster_max = int(raw_eviction.get("duplicate_cluster_max", 5) or 5)

        raw_merge = config.get("merge_at_write", {}) or {}
        self._enabled = bool(raw_merge.get("enabled", True))
        self._strategy: str = raw_merge.get("strategy", "overwrite")

        self._audit_log: List[Dict[str, Any]] = []

        logger.info(
            "MergeEngine initialised — enabled=%s, strategy=%r, cluster_max=%d, sim_min=%.2f",
            self._enabled, self._strategy, self._cluster_max, self._similarity_min,
        )

    @staticmethod
    def _strategy_overwrite(existing, new_value):
        return _strategy_overwrite(existing, new_value)

    @staticmethod
    def _strategy_append(existing, new_value):
        return _strategy_append(existing, new_value)

    @staticmethod
    def _strategy_union(existing, new_value):
        return _strategy_union(existing, new_value)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def pre_save_check(
        self, key: str, value: Any, source_name: Optional[str] = None
    ) -> MergeResult:
        """Intercept a save call and merge if similar content exists."""
        result = MergeResult(final_value=value)

        # Fast-pass when disabled
        if not self._enabled or self._orchestrator is None:
            return result

        similar = self._find_similar(key, value, source_name)
        high_similar = [e for e in similar if e["similarity"] >= self._similarity_min]
        result.similar_found = len(high_similar)

        # Below cluster threshold → pass through immediately
        if len(high_similar) < self._cluster_max:
            logger.debug("pre_save_check '%s': %d high-sim hits below max=%d — pass through",
                         key, len(high_similar), self._cluster_max)
            result.merge_action = AuditAction.PASSTHROUGH_NEW_KEY
            self._audit(key, value, "check", None, result=similar, similar=len(similar))
            return result

        # Merge triggered — find the best matching existing entry 
        best_match = max(high_similar, key=lambda e: e["similarity"])
        existing_key = best_match.get("key", "")

        try:
            existing_value = self._retrieve_existing(existing_key, source_name)
            merged_value, action = self._apply_strategy(existing_value, value)
            result.should_proceed = False
            result.final_value = merged_value
            result.merge_action = action
            logger.info("pre_save_check '%s': merge triggered (action=%s, sim=%.2f)",
                        key, action, best_match["similarity"])
        except Exception as exc:
            # De-escalate to safe pass-through on any merge failure
            logger.warning("pre_save_check '%s': merge degraded — %s", key, exc)
            result.merge_action = AuditAction.DEGRADE_SAFETY_NET
            merged_value = value

        self._audit(key, value, "merge", best_match, similar=len(similar), action=result.merge_action)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_similar(
        self, key: str, value: Any, target_source_name: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Search across all sources for content similar to the incoming save."""
        if not getattr(self._orchestrator, 'memory_sources', {}):
            return []

        results: List[Dict[str, Any]] = []
        raw_value_str = str(value) if value is not None else ""
        new_tokens = _tokenize(raw_value_str)

        for src_name, src_obj in self._orchestrator.memory_sources.items():
            try:
                raw_hits = src_obj.search(key, limit=500)
                if not raw_hits:
                    continue
                for hit in raw_hits:
                    existing_val = hit.get("value") or hit.get("content")
                    if existing_val is None:
                        continue
                    sim = _jaccard_similarity(new_tokens, _tokenize(str(existing_val)))
                    entry = {
                        "key": hit.get("key", key),
                        "source": src_name,
                        "similarity": round(sim, 4),
                        "existing_value": existing_val,
                    }
                    results.append(entry)
            except Exception as exc:
                logger.debug("MergeEngine search skipped %s — %s", src_name, exc)

        return sorted(results, key=lambda e: e["similarity"], reverse=True)

    # ------------------------------------------------------------------

    def _retrieve_existing(
        self, existing_key: str, target_source_name: Optional[str]
    ) -> Optional[Any]:
        """Pull existing value from the source so merge strategies can see it."""
        if not existing_key or self._orchestrator is None:
            return None

        # Try orchestrator.retrieve() first for well-known keys
        try:
            hits = self._orchestrator.retrieve(existing_key)
            if hits is not None:
                return hits
        except Exception:
            pass

        # Direct source search fallback when retrieve misses or returns None
        for name, src in getattr(self._orchestrator, 'memory_sources', {}).items():
            try:
                raw = src.search(existing_key, limit=50)
                if isinstance(raw, list):
                    for r in raw:
                        rk = r.get("key", "")
                        if rk == existing_key or (rk and hasattr(rk, 'lower') and rk.lower() == existing_key.lower()):
                            return r.get("value") or r.get("content")
            except Exception:
                pass

        return None

    # ------------------------------------------------------------------

    def _apply_strategy(
        self, existing_value: Any, new_value: Any
    ) -> Tuple[Any, AuditAction]:
        """Dispatch to the configured merge strategy."""
        fn = self.STRATEGIES.get(self._strategy) or self.STRATEGIES["overwrite"]
        return fn(existing_value, new_value)

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def _audit(
        self, key: str, value: Any, phase: str, best_match: Optional[Dict], **extra: Any
    ) -> None:
        """Append a JSONL audit entry for traceability."""
        entry = {
            "ts": time.time(),
            "phase": phase,
            "key": key,
            "best_match_key": best_match.get("key") if best_match else None,
            **extra,
        }
        self._audit_log.append(entry)

    def get_audit_trail(self) -> List[Dict[str, Any]]:
        """Return accumulated audit entries."""
        return list(self._audit_log)


# --------------------------------------------------------------------------- 
# Factory function for orchestrator use
# ---------------------------------------------------------------------------

def create_merge_engine(
    orchestrator: Any, config: Dict[str, Any]
) -> Optional[MergeEngine]:
    """Build a MergeEngine or return None when config is missing/disabled."""
    merge_cfg = config.get("merge_at_write", {}) or {}
    if not merge_cfg.get("enabled", False):
        logger.info("MergeEngine disabled in config — skipping")
        return None

    try:
        engine = MergeEngine(orchestrator, config)
        logger.info("MergeEngine ready (strategy=%s)", engine._strategy)
        return engine
    except Exception as exc:
        logger.warning("MergeEngine init failed — degraded to passthrough: %s", exc)
        return None
