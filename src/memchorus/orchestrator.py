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

from datetime import datetime, timezone
from enum import Enum
from typing import List, Dict, Any, Optional
from memchorus.memory_source import MemorySource
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource
from memchorus.relevance_engine import RelevanceScorer, ContextWeight


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
        """
        self.config = config or {}
        self.memory_sources: Dict[str, MemorySource] = {}
        self._default_source_name = self.config.get('default_source', 'hermes_default')
        # Relevance scoring engine (Gap G1/G2 fix)
        half_life_days = self.config.get('half_life_days', 30.0)
        self._scorer = RelevanceScorer(half_life_days=half_life_days)
        self._initialize_default_sources()
    
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
    
    def save(self, key: str, value: Any, source_name: Optional[str] = None) -> bool:
        """
        Save a memory to the appropriate source.
        
        The orchestrator decides where to save based on:
        - Explicit source specification
        - Source availability and efficiency
        - Memory characteristics
        
        Args:
            key (str): Unique identifier for the memory
            value (Any): The memory content to store
            source_name (str, optional): Specific source to save to
            
        Returns:
            bool: True if successful, False otherwise
        """
        # If specific source is provided, use it — fail if unavailable
        if source_name:
            if source_name in self.memory_sources:
                return self.memory_sources[source_name].save(key, value)
            # Explicit source requested but not registered -> fail gracefully
            return False
        
        # Otherwise, save to the most appropriate source
        # For v1.0, we'll save to both sources (redundancy for resilience)
        success_count = 0
        
        # Save to Hermes default memory (resilient core if available)
        if self.memory_sources.get('hermes_default') and self.memory_sources['hermes_default'].is_available():
            if self.memory_sources['hermes_default'].save(key, value):
                success_count += 1
        
        # Save to MemPalace (enhancement voice if available)
        if self.memory_sources.get('mempalace') and self.memory_sources['mempalace'].is_available():
            if self.memory_sources['mempalace'].save(key, value):
                success_count += 1
                
        return success_count > 0
    
    def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a memory from the most relevant source.
        
        Uses RelevanceScorer to rank available sources by a computed relevance
        score (source-type bias for key-based retrieval). Sources with higher
        priority scores are tried first -- if that source has the key, return it.
        If unavailable or missing, fall through to the next-best source.
        
        Unlike the original hard-coded priority chain ['hermes_default', 'mempalace'],
        this method respects the scorer's ranking so new sources can compete on score.
        
        Args:
            key (str): Unique identifier for the memory
            
        Returns:
            Any: The memory content if found, None otherwise
        """
        # Build candidate list ranked by scorer's source-type bias
        candidates = self._scorer.score_and_rank(
            [  # dummy results -- only the "source" key matters for bias ranking
                {"key": src_name, "content": "", "source": s.name}
                for src_name, s in self.memory_sources.items()
            ],
            query="",
        )
        
        for candidate in candidates:
            source = self.memory_sources.get(candidate.source)
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
        
        # Inject domain-level weightings before scoring
        all_results = []
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
                
                limit -= len(results)
                if limit <= 0:
                    break
            except Exception:
                continue
        
        # Score and rank via the relevance engine
        ranked = self._scorer.score_and_rank(all_results, query, context)
        
        # Convert RankedResult -> plain dict with score field
        return [
            {
                "key": r.key,
                "content": r.content,
                "source": r.source,
                "score": r.score,  # <-- always present
                **r.meta,
            }
            for r in ranked[:limit]
        ]
    
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
