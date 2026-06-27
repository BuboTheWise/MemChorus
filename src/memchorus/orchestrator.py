"""
Memory Orchestrator

The MemoryOrchestrator is the core component that manages multiple memory sources
and provides intelligent context management for agents.

Relevance scoring: the orchestrator now uses a RelevanceScorer to rank multi-source
search results by computed relevance (G1 + G2 gap fixes) rather than a hard-coded
priority chain that defeats the "chorus" principle.

Smart placement (t_d0150e05 / G4+G5): memory_profile enum, inference from content,
cross-source deduplication to prevent redundant storage.
"""

import hashlib
import json
import logging
import dataclasses
from datetime import datetime, timezone
from enum import Enum
from typing import List, Dict, Any, Optional, Tuple
from memchorus.memory_source import MemorySource
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource
from memchorus.relevance_engine import RelevanceScorer, ContextWeight
from memchorus.enforcement_manager import BehavioralEnforcementManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Enum: memory_profile (explicit or inferred)
# ---------------------------------------------------------------------------

class MemoryProfile(Enum):
    """Classification that guides smart storage placement decisions."""

    # --- user-facing profile names ---------------------------------------
    USER_PREFERENCE         = "user_preference"        # hermes_default
    LONG_LIVED_KNOWLEDGE    = "long_lived_knowledge"   # mempalace
    EPHEMERAL               = "ephemeral"              # hermes_default
    LARGE_DATA_BLOCK        = "large_data_block"       # hermes_default
    RELATIONSHIP_GRAPH      = "relationship_graph"     # mempalace
    CONTEXT_SENSITIVE_PREF  = "context_sensitive_pref" # hermes_default

    # --- auto-inferred sentinel -----------------------------------------
    AUTO                    = "auto"                   # infer from content / type


# Helper map: which source each profile *prefers* (not an exclusive list)
_PROFILE_SOURCE_HINT: Dict[MemoryProfile, List[str]] = {
    MemoryProfile.USER_PREFERENCE:         ["hermes_default"],
    MemoryProfile.LONG_LIVED_KNOWLEDGE:   ["mempalace"],
    MemoryProfile.EPHEMERAL:               ["hermes_default", "mempalace"],          # both cheaply
    MemoryProfile.LARGE_DATA_BLOCK:        ["hermes_default", "mempalace"],           # fallback ok
    MemoryProfile.RELATIONSHIP_GRAPH:      ["mempalace"],
    MemoryProfile.CONTEXT_SENSITIVE_PREF:  ["hermes_default"],
    MemoryProfile.AUTO:                    ["mempalace", "hermes_default"],           # try mempalace first
}

# Heuristic thresholds for AUTO inference
_MAX_KV_STRING_BYTES = 4_500        # key-value payloads above this are "large"
_JSON_LARGE_LIMIT  = 1_000          # dict/list size (items/keys) > this → large


class MemoryOrchestrator:
    """
    Core orchestrator for managing multiple memory sources in MemChorus.
    
    This orchestrator handles:
    - Registration and management of memory sources
    - Intelligent retrieval decisions based on relevance and efficiency  
    - Optimized storage placement decisions
    - Proactive memory checking before actions
    - Post-action memory saving behavior
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the MemoryOrchestrator.

        Args:
            config (Dict[str, Any], optional): Configuration parameters for the orchestrator

            Supported config keys:
                enforce_on_read (bool): Enable pre-decision recall during search/retrieve (default True)
                enforce_on_write (bool): Enable post-action storage during save (default True)
                half_life_days (float): Relevance scoring decay (default 30.0)
                default_source (str): Default source name (default 'hermes_default')
        """
        self.config = config or {}
        self.memory_sources: Dict[str, MemorySource] = {}
        self._default_source_name = self.config.get('default_source', 'hermes_default')

        # Relevance scoring engine (Gap G1/G2 fix)
        half_life_days = self.config.get('half_life_days', 30.0)
        self._scorer = RelevanceScorer(half_life_days=half_life_days)

        # Behavioral enforcement pipeline
        self._enforce_on_read = bool(self.config.get('enforce_on_read', True))
        self._enforce_on_write = bool(self.config.get('enforce_on_write', True))
        self._enforcement_manager: Optional[BehavioralEnforcementManager] = None
        # Guard against recursive enforcement when capture_outcome calls back into save()
        self._in_enforcement_save = False
        self._initialize_default_sources()

    def _get_enforcement_manager(self) -> Optional[BehavioralEnforcementManager]:
        """Lazily instantiate BehavioralEnforcementManager once enforcement is needed.

        Returns None if enforcement is disabled in config or sources are unavailable.
        """
        if self._enforcement_manager is not None:
            return self._enforcement_manager

        # Only bootstrap enforcement when both read and write are disabled is False
        if not self._enforce_on_read and not self._enforce_on_write:
            return None

        try:
            self._enforcement_manager = BehavioralEnforcementManager(orchestrator=self)
            # Respect individual knobs on the manager too (recall for reads, storage for writes)
            self._enforcement_manager.enable_recall(self._enforce_on_read)
            self._enforcement_manager.enable_storage(self._enforce_on_write)
        except Exception as exc:
            logger.warning("MemoryOrchestrator: failed to create BehavioralEnforcementManager: %s", exc)
            return None

        return self._enforcement_manager
    
    def _initialize_default_sources(self):
        """Initialize the default memory sources."""
        # Add Hermes default as the resilient core
        hermes_source = HermesDefaultMemorySource(
            name='hermes_default',
            config=self.config.get('hermes_default_config', {})
        )
        self.memory_sources['hermes_default'] = hermes_source
        
        # Add MemPalace as the primary voice  
        mempalace_source = MemPalaceMemorySource(
            name='mempalace',
            config=self.config.get('mempalace_config', {})
        )
        self.memory_sources['mempalace'] = mempalace_source
    
    def register_source(self, source: MemorySource) -> bool:
        """
        Register a new memory source with the orchestrator.
        
        Args:
            source (MemorySource): The memory source to register
            
        Returns:
            bool: True if successfully registered, False otherwise
        """
        try:
            self.memory_sources[source.name] = source
            return True
        except Exception:
            return False
    
    def unregister_source(self, source_name: str) -> bool:
        """
        Unregister a memory source from the orchestrator.
        
        Args:
            source_name (str): The name of the memory source to unregister
            
        Returns:
            bool: True if successfully unregistered, False otherwise
        """
        try:
            if source_name in self.memory_sources:
                del self.memory_sources[source_name]
                return True
            return False
        except Exception:
            return False
    
    def _infer_profile(self, value: Any) -> MemoryProfile:
        """
        Infer the memory profile from the content's characteristics when AUTO.

        Heuristics:
        - Large payloads (>4500 str bytes or >1000 dict/list items) → LARGE_DATA_BLOCK
        - Dict-like structures with key:value pairs → USER_PREFERENCE
        - Lists containing tuples/edges → RELATIONSHIP_GRAPH
        - Everything else defaults to EPHEMERAL as a safe fallback

        Args:
            value (Any): The memory content being persisted

        Returns:
            MemoryProfile: Classified profile based on value shape and size
        """
        # --- large-data early exit ----------------------------------
        if isinstance(value, str) and len(value.encode('utf-8', errors='replace')) > _MAX_KV_STRING_BYTES:
            return MemoryProfile.LARGE_DATA_BLOCK
        if isinstance(value, (dict, list)) and len(value) > _JSON_LARGE_LIMIT:
            return MemoryProfile.LARGE_DATA_BLOCK

        # --- structural hints ---------------------------------------
        if isinstance(value, dict):
            return MemoryProfile.USER_PREFERENCE
        if isinstance(value, list):
            # Detect relationship-graph signatures (tuples/2-element lists representing edges)
            has_edges = False
            for item in value:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    has_edges = True
                    break
            if has_edges:
                return MemoryProfile.RELATIONSHIP_GRAPH

        return MemoryProfile.EPHEMERAL

    def save(self,
             key: str,
             value: Any,
             source_name: Optional[str] = None,
             profile: Optional[MemoryProfile] = None) -> bool:
        """
        Save a memory to the appropriate source using intelligent placement.

        Placement strategy (order of precedence):
        1. Explicit ``source_name`` → write there directly and return.
        2. Profile hint (explicit or AUTO-inferred) → consult ``_PROFILE_SOURCE_HINT``
           for preferred targets; try each in order, falling back to available ones.
        3. As a final safety net, save to any source that is currently available

        This avoids duplication: once the memory is successfully stored at the
        first acceptable target we stop iterating even if additional sources
        match the profile preference list.

        If enforcement-on-write is enabled, post-action storage capture runs
        after a successful save via BehavioralEnforcementManager so significant
        outcomes are automatically captured for future recall.

        Args:
            key (str): Unique identifier for the memory
            value (Any): The memory content to store
            source_name (str, optional): Specific source to save to when caller overrides smart placement
            profile (MemoryProfile, optional): Classification hint; defaults to AUTO inference

        Returns:
            bool: True if successful storage occurred at any registered target, False otherwise
        """
        # --- explicit source override takes precedence ------------------
        saved = False
        if source_name:
            if source_name in self.memory_sources:
                saved = self.memory_sources[source_name].save(key, value)
            return saved

        # --- resolve profile (auto-infer from content when omitted) -----\
        if profile is not None:
            effective_profile = profile
        else:
            effective_profile = self._infer_profile(value)
        
        # --- get ranked target sources for this profile -----------------\
        preferred_targets = _PROFILE_SOURCE_HINT.get(effective_profile, [])

        # ---- preferred targets first ---------------------------------\
        for t in preferred_targets:
            src = self.memory_sources.get(t)
            if src and src.is_available() and src.save(key, value):
                saved = True
                break

        # ---- safety net: try ANY available source -----------------------
        if not saved:
            for src in self.memory_sources.values():
                if src.is_available() and src.save(key, value):
                    saved = True
                    break
        
        # --- Post-action storage capture (behavioral enforcement hook) ---
        # Guard against recursive enforcement when capture_outcome calls back into save()
        if saved and self._enforce_on_write and not self._in_enforcement_save:
            em = self._get_enforcement_manager()
            if em is not None:
                self._in_enforcement_save = True
                try:
                    outcome_text = f"Saved memory '{key}' to orchestrator pipeline. Content type: {type(value).__name__}."
                    _storage_result = em.enforce(outcome_text)
                    logger.debug("Post-action storage capture after save('%s'): %d points, errors=%d",
                                key, _storage_result.triggered_points, len(_storage_result.errors))
                except Exception:
                    pass  # degrade gracefully — the save itself already succeeded
                finally:
                    self._in_enforcement_save = False
        
        return saved
    
    def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a memory from the most relevant source.

        Uses RelevanceScorer.rank_sources() to rank available sources by source-type
        bias (ignoring quality/recency since there is no content yet). Sources ranked
        higher are tried first; if one has the key, return it. This avoids creating
        dummy result dicts that run useless recency/quality math on empty data.

        If enforcement-on-read is enabled, pre-decision recall runs before queries and
        any recalled context is injected into returned results so the caller also
        receives relevant memory that surfaced at a detected decision point.

        Args:
            key (str): Unique identifier for the memory

        Returns:
            Any: The memory content if found, None otherwise
        """
        # --- Pre-decision recall (behavioral enforcement hook) ---
        _recall_context: List[Dict[str, Any]] = []
        if self._enforce_on_read:
            em = self._get_enforcement_manager()
            if em is not None:
                try:
                    _recall_result = em.enforce(key)
                    _recall_context = getattr(_recall_result, 'recall_context', [])
                except Exception:
                    pass  # degrade gracefully

        candidate_sources = self._scorer.rank_sources(
            list(self.memory_sources.keys()),
        )

        # If recall fired and found context for this key, return it inline
        if _recall_context:
            for rec in _recall_context:
                if rec.get("key") == key:
                    return rec.get("content", rec)

        for src_name in candidate_sources:
            source = self.memory_sources.get(src_name)
            if source and source.is_available():
                result = source.retrieve(key)
                if result is not None:
                    return result

        return None
    
    def search(self, query: str, limit: int = 10, context: Optional[ContextWeight] = None, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Search for memories matching a query across all sources.

        Every source is queried; results are combined, scored with the RelevanceScorer,
        deduplicated (highest score per key wins), and sorted descending.

        If enforcement-on-read is enabled, pre-decision recall runs before queries and any
        recalled context is injected into the result set so the caller also receives
        relevant memory that surfaced at a detected decision point.

        Args:
            query (str): Search query string
            limit (int): Maximum number of results to return
            context: Optional ContextWeight for custom scoring preferences
            domain: Optional domain hint (e.g. 'memory', 'graph') that influences
                    source-type boosting in the scorer

        Returns:
            List[Dict[str, Any]]: Sorted search results, each including a ``score`` field
        """
        if context is None:
            context = ContextWeight()

        # --- Pre-decision recall (behavioral enforcement hook) ---
        _recall_context: List[Dict[str, Any]] = []
        if self._enforce_on_read:
            em = self._get_enforcement_manager()
            if em is not None:
                try:
                    _recall_result = em.enforce(query)
                    _recall_context = getattr(_recall_result, 'recall_context', [])
                except Exception:
                    pass  # degrade gracefully — base search continues

        # Inject domain-level weightings before scoring
        all_results = []
        remaining_fetch_budget = limit  # cap on raw results collected from sources
        for source_name, source in self.memory_sources.items():
            if not source.is_available():
                continue
            try:
                results = source.search(query, limit)
                if not results:
                    continue
                
                # Attach a timestamp (current time for v1.0 stub) to every result so
                # the scorer has data to work with; also attach domain hint.
                now_iso = datetime.now(timezone.utc).isoformat()
                for r in results:
                    if "timestamp" not in r:
                        r["timestamp"] = now_iso
                    r["_domain"] = domain  # passed through into RankedResult.meta
                    
                    # If this result has an explicit score from the source honour it;
                    # otherwise let the scorer compute one (higher wins).
                    if "score" not in r:
                        r["score"] = 0.0
                all_results.extend(results)
                
                remaining_fetch_budget -= len(results)
                if remaining_fetch_budget <= 0:
                    break
            except Exception:
                continue
        
        # Score and rank via the relevance engine
        ranked = self._scorer.score_and_rank(all_results, query, context)
        
        # Convert RankedResult -> plain dict with score field — use original limit
        results = [
            {
                "key": r.key,
                "content": r.content,
                "source": r.source,
                "score": r.score,  # <-- always present
                **r.meta,
            }
            for r in ranked[:limit]
        ]

        # Inject pre-decision recalled context into the result set (deduped by key)
        if _recall_context:
            existing_keys = {r["key"] for r in results}
            for rec in _recall_context:
                rk = rec.get("key", "")
                if rk and rk not in existing_keys:
                    rec.setdefault("score", 0.5)
                    rec["key"] = rk
                    results.append(rec)
                    existing_keys.add(rk)

        return results

    def _sort_and_deduplicate_results(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Sort and remove duplicate results from search.
        
        Now delegates to the RelevanceScorer which scores every result, deduplicates
        by key (highest score wins), and returns them in descending-score order.
        
        Args:
            results (List[Dict[str, Any]]): Raw search results
            
        Returns:
            List[Dict[str, Any]]: Sorted and deduplicated results with score field
        """
        scored = self._scorer.score_and_rank(results, query="", context=ContextWeight())
        return [
            {
                "key": r.key,
                "content": r.content,
                "source": r.source,
                "score": r.score,  # --- always present per acceptance criterion
                **r.meta,
            }
            for r in scored
        ]
    
    def is_available(self) -> bool:
        """
        Check if the memory orchestrator and any sources are available.
        
        Returns:
            bool: True if at least one source is available, False otherwise
        """
        for source in self.memory_sources.values():
            if source.is_available():
                return True
        return False
    
    def get_orchestrator_info(self) -> Dict[str, Any]:
        """
        Get comprehensive information about the orchestrator and its sources.
        
        Returns:
            Dict[str, Any]: Metadata about the orchestrator and sources
        """
        info = {
            'orchestrator': {
                'name': 'memchorus_orchestrator',
                'version': '1.0.0',
                'default_source': self._default_source_name,
                'available_sources': len([s for s in self.memory_sources.values() if s.is_available()]),
                'total_sources': len(self.memory_sources)
            },
            'sources': {}
        }
        
        for name, source in self.memory_sources.items():
            info['sources'][name] = source.get_source_info()
            
        return info
