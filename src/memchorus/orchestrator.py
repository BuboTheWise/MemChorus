"""
Memory Orchestrator

The MemoryOrchestrator is the core component that manages multiple memory sources
and provides intelligent context management for agents.

Relevance scoring: the orchestrator now uses a RelevanceScorer to rank multi-source
search results by computed relevance (G1 + G2 gap fixes) rather than a hard-coded
priority chain that defeats the "chorus" principle.

Smart placement (t_d0150e05 / G4+G5): memory_profile enum, inference from content,
cross-source deduplication to prevent redundant storage.

Lifecycle management (§8 Phase 1): config schema, LifecycleManager skeleton,
SweepScheduler, AuditLogger — all opt-in, disabled by default for backward compat (§9).
"""

import time
import threading
import hashlib
import json
import logging
import dataclasses
from datetime import datetime, timezone
from enum import Enum
from typing import List, Dict, Any, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from memchorus.lifecycle_manager import LifecycleManager

from memchorus.memory_source import MemorySource
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource
from memchorus.relevance_engine import RelevanceScorer, RankedResult, ContextWeight
from memchorus.enforcement_manager import BehavioralEnforcementManager
from memchorus.lifecycle_merge import create_merge_engine, MergeEngine

logger = logging.getLogger(__name__)


def _check_source_available(source) -> bool:
    """Check source availability safely, handling both method and property forms.

    The ABC contract in memory_source.py defines is_available as an abstract *method*,
    but enforcement_manager.py implements it as a @property, and custom subclasses
    may use either form. This helper resolves the mismatch transparently:
      - If callable (method), call it to get the boolean result.
      - If not callable (property/data), read the value directly.
      - Return False if source is None or is_available attribute is missing.
      - Return True on exception so a broken availability check doesn't kill operations.
    """
    if source is None:
        return False
    attr = getattr(source, "is_available", None)
    if attr is None:
        return False
    try:
        if callable(attr):
            return bool(attr())
        return bool(attr)
    except Exception:
        return True


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
                lifecycle_config (Dict[str, Any]): Optional lifecycle management config (§6.2).

                    Nested sub-keys:
                        enabled (bool): Master toggle — disabled by default (§9 backward compat)
                        sweep_interval_hours (int): Sweep frequency in hours (default 8, §6.1)
                        retention_days (Dict[str, int|None]): Per-profile TTL mapping (§3.1)
                        eviction (Dict[str, Any]): Eviction policy thresholds (§4.1)
                        archive (Dict[str, Any]): Archival config — grace period + penalty (§4.2)
                        merge_at_write (Dict[str, bool]): Pre-save dedup toggle (§5.1)
                        audit (Dict[str, Any]): Audit logger path, max entries, enabled flag (§6.4)
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
        # Read-side recursion guard (MC-001 / t_7d26af26): prevents orchestrator.search()
        # and orchestrator.retrieve() from recursively triggering enforcement when the recall
        # engine's nested search/recall fires an inner enforce().  Without this flag, pre-decision
        # recall -> AutoRecallEngine._do_search() -> orchestrator.search() -> enforce() recurses
        # indefinitely until RecursionError.
        self._in_enforcement_recall = False
        # Thread-safe access to the recursion guards (MC-003)
        self._enforcement_lock = threading.RLock()

        # GAP010: source enable/disable state (default-enabled on registration)
        self._source_enabled: Dict[str, bool] = {}

        # GAP008: retrieval cache (LRU with TTL in seconds)
        self._retrieve_cache: Dict[str, Tuple[Any, float]] = {}
        self._cache_ttl = float(self.config.get('cache_ttl_seconds', 60.0))
        self._cache_max_size = int(self.config.get('cache_max_size', 256))

        # GAP008: configurable source priority for retrieval
        self._priority_order: List[str] = list(self.config.get('priority_order', []))

        # Lifecycle management (§6.2 / Phase 1 — opt-in)
        self._lifecycle_manager: Optional['LifecycleManager'] = None
        self._initialize_lifecycle()

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

    def _initialize_lifecycle(self) -> None:
        """Initialize lifecycle management (opt-in, disabled by default — §9).

        Reads ``lifecycle_config`` from the orchestrator config, merges it with
        sensible defaults from `DEFAULT_LIFECYCLE_CONFIG`, and only instantiates
        a LifecycleManager when the master ``enabled`` toggle is True.

        If the user explicitly requested lifecycle tracking but left enabled=False
        by accident, we still create the manager so that ``self.lifecycle_manager``
        exists for inspection — just with its scheduler stopped.
        """
        from memchorus.lifecycle_manager import LifecycleManager, _resolve_lifecycle_config

        raw_lifecycle = self.config.get("lifecycle_config", None)
        resolved = _resolve_lifecycle_config(raw_lifecycle)

        # Even if disabled, instantiate the manager so that callers can inspect config/audit
        self._lifecycle_manager = LifecycleManager(
            config=resolved,
            orchestrator=self,
        )

        if self._lifecycle_manager.is_enabled:
            logger.info("MemoryOrchestrator: lifecycle management enabled")
        else:
            logger.debug(
                "MemoryOrchestrator: lifecycle disabled (lifecycle_config.enabled=False or not set)"
            )

        # --- Merge engine (pre-save interception) -----------------------
        self._merge_engine: Optional[MergeEngine] = create_merge_engine(self, resolved)

    def _initialize_default_sources(self):
        """Initialize the default memory sources.

        HermesDefault is always registered — it only needs a local file directory
        and never blocks orchestrator creation. MemPalace is added when available;
        if MCP or its import fails, we log a warning and continue with degraded
        but operational state so that _instance remains non-None (AC-1).
        """
        # Add Hermes default as the resilient core — this cannot fail because
        # it only depends on local filesystem access which is guaranteed.
        hermes_source = HermesDefaultMemorySource(
            name='hermes_default',
            config=self.config.get('hermes_default_config', {})
        )
        self.memory_sources['hermes_default'] = hermes_source
        self._source_enabled['hermes_default'] = True

        # Add MemPalace as the primary voice — tolerate failure so that
        # MCP unavailability does not destroy the orchestrator.
        try:
            mempalace_source = MemPalaceMemorySource(
                name='mempalace',
                config=self.config.get('mempalace_config', {})
            )
            self.memory_sources['mempalace'] = mempalace_source
            self._source_enabled['mempalace'] = True
        except Exception as exc:
            logger.warning(
                "MemPalace source unavailable during orchestrator init — "
                "continuing with hermes_default only. Error: %s", exc,
            )
    
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
            # GAP010: newly registered sources are enabled by default — without this
            # they crash on every save/retrieve/search with KeyError in _source_enabled.
            self._source_enabled.setdefault(source.name, True)
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
                self._source_enabled.pop(source_name, None)
                return True
            return False
        except Exception:
            return False
    
    # ---------------------------------------------------------------------------
    # GAP010: Source enable/disable without unregistering
    # ---------------------------------------------------------------------------
    
    def disable_source(self, source_name: str) -> bool:
        """Disable a memory source so it is skipped during save/retrieve/search.
        
        The source remains registered and can be re-enabled later.
        
        Returns True on success, False if source was not found."""
        try:
            if source_name in self._source_enabled:
                self._source_enabled[source_name] = False
                return True
            return False
        except Exception:
            return False
    
    def enable_source(self, source_name: str) -> bool:
        """Re-enable a previously disabled source.
        
        Returns True on success, False if source was not found."""
        try:
            if source_name in self._source_enabled:
                self._source_enabled[source_name] = True
                return True
            return False
        except Exception:
            return False
    
    def is_source_enabled(self, source_name: str) -> bool:
        """Check whether a source is in an enabled state without unregistering it."""
        if source_name in self._source_enabled:
            return self._source_enabled[source_name]
        # Nonexistent sources are never enabled
        if source_name not in self.memory_sources:
            return False
        # If the source exists but was never added to the toggle dict (e.g. injected
        # mocks or old code paths), treat it as implicitly enabled.
        return True
    
    def _infer_profile(self, value: Any) -> MemoryProfile:
        """
        Infer the memory profile from the content's characteristics when AUTO.

        Heuristics:
        - Large payloads (>4500 str bytes or >1000 dict/list items) → LARGE_DATA_BLOCK
        - Dict-like structures with key:value pairs → USER_PREFERENCE
        - Lists containing tuples/edges → RELATIONSHIP_GRAPH
        - Everything else defaults to EPHEMERAL as a safe fallback

        Args:
            value (Any): The memory content to analyze

        Returns:
            MemoryProfile: Classified profile for this content
        """
        # --- large-data early exit ----------------------------------
        if isinstance(value, str) and len(value.encode('utf-8', errors='replace')) > _MAX_KV_STRING_BYTES:
            return MemoryProfile.LARGE_DATA_BLOCK
        if isinstance(value, (dict, list)) and len(value) > _JSON_LARGE_LIMIT:
            return MemoryProfile.LARGE_DATA_BLOCK

        # --- structural hints ---------------------------------------
        if isinstance(value, dict):
            # Detect relationship-graph signatures in dicts (keys or values that hint at relations)
            _graph_keywords = {"relation", "relates_to", "connected", "friend", "entity",
                              "edge", "link", "associate", "network"}
            text = " ".join(str(k).lower() for k in value.keys()) + " " \
                  + " ".join(str(v).lower() for v in value.values())
            if any(kw in text for kw in _graph_keywords):
                return MemoryProfile.RELATIONSHIP_GRAPH
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

    @staticmethod
    def _try_save_to(source: MemorySource, key: str, value: Any) -> bool:
        """Attempt to save to a source, returning success status."""
        try:
            return source.save(key, value)
        except Exception:
            return False

    # ---------------------------------------------------------------------------
    # GAP009: Smart placement helpers (dedup + consolidation)
    # ---------------------------------------------------------------------------

    def find_duplicates(self, key: str) -> List[str]:
        """Return list of source names that currently store the given key.

        Useful for detecting redundant copies across sources.
        """
        duplicates = []
        for name, source in self.memory_sources.items():
            if not _check_source_available(source) or not self.is_source_enabled(name):
                continue
            try:
                result = source.retrieve(key)
                if result is not None:
                    duplicates.append(name)
            except Exception:
                pass
        return duplicates

    def consolidate_key(self, key: str) -> Dict[str, Any]:
        """Remove redundant copies of a key, keeping only the best copy.

        Strategy: prefer mempalace for graph/long-lived data; prefer hermes_default otherwise.
        Returns a summary dict with the consolidated key name and the surviving source(s).
        """
        duplicates = self.find_duplicates(key)
        if len(duplicates) <= 1:
            return {
                "key": key,
                "surviving": duplicates[:1],
                "removed_sources": [],
                "deleted_count": 0,
            }

        # Infer content shape to decide which copy to keep -------------
        _keep_value = None
        for sname in duplicates:
            try:
                val = self.memory_sources[sname].retrieve(key)
                if val is not None:
                    _keep_value = val
                    break
            except Exception:
                pass

        # Safety guard: if we couldn't retrieve any value, we can't
        # infer a profile — bail out without deleting anything.
        if _keep_value is None:
            logger.warning(
                "consolidate_key('%s'): could not retrieve value from any of "
                "%d duplicate sources; keeping all copies", key, len(duplicates),
            )
            return {
                "key": key,
                "surviving": list(duplicates),
                "removed_sources": [],
                "deleted_count": 0,
            }

        # Decide best target based on inferred profile -----------------
        inferred = self._infer_profile(_keep_value)
        preference_list = _PROFILE_SOURCE_HINT.get(inferred, duplicates)

        surviving: List[str] = []
        for pref in preference_list:
            if pref in duplicates and pref not in surviving:
                surviving.append(pref)
                break

        # Safety guard: if no preferred source survived, keep ALL copies
        # rather than deleting everything — data safety over cleanup
        if not surviving:
            logger.warning(
                "consolidate_key('%s'): no preferred target found; keeping all "
                "%d copies to prevent data loss", key, len(duplicates)
            )
            return {
                "key": key,
                "surviving": list(duplicates),
                "removed_sources": [],
                "deleted_count": 0,
            }

        # Identify which copies should be removed ---------------------
        removed_sources: List[str] = []
        for sname in duplicates:
            if sname not in surviving:
                removed_sources.append(sname)

        # Actually remove the key from each redundant source ----------
        deleted_count = 0
        for sname in removed_sources:
            src = self.memory_sources.get(sname)
            if src is not None:
                try:
                    ok = src.delete(key)
                    if ok:
                        deleted_count += 1
                        logger.info(
                            "consolidate_key('%s'): deleted from '%s'", key, sname
                        )
                    else:
                        logger.warning(
                            "consolidate_key('%s'): delete returned False for "
                            "source '%s' (may already be gone)",
                            key, sname,
                        )
                except Exception as exc:
                    logger.error(
                        "consolidate_key('%s'): exception deleting from '%s': %s",
                        key, sname, exc,
                    )

        return {
            "key": key,
            "surviving": surviving,
            "removed_sources": removed_sources,
            "deleted_count": deleted_count,
        }

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
                # Merge engine pre-save check
                if self._merge_engine is not None:
                    merge_result = self._merge_engine.pre_save_check(key, value, source_name)
                    if not merge_result.should_proceed:
                        value = merge_result.final_value
                saved = self.memory_sources[source_name].save(key, value)
            # GAP008: invalidate cache on explicit-source write too -----
            if saved and key in self._retrieve_cache:
                del self._retrieve_cache[key]
            return saved

        # --- resolve profile (auto-infer from content when omitted) -----\
        if profile is not None:
            effective_profile = profile
        else:
            effective_profile = self._infer_profile(value)
        
        # --- get ranked target sources for this profile -----------------\
        preferred_targets = _PROFILE_SOURCE_HINT.get(effective_profile, [])

        # ---- merge engine pre-save check (before any source write) -----
        if self._merge_engine is not None:
            merge_result = self._merge_engine.pre_save_check(
                key, value, profile=effective_profile
            )
            if not merge_result.should_proceed:
                value = merge_result.final_value

        # ---- preferred targets first (skip disabled) -----------
        for t in preferred_targets:
            src = self.memory_sources.get(t)
            if _check_source_available(src) and self.is_source_enabled(t):
                saved = self._try_save_to(src, key, value)
                break

        # ---- safety net: try ANY available non-disabled source --------
        if not saved:
            for n, src in self.memory_sources.items():
                if src and getattr(src, 'is_available', True) and self.is_source_enabled(n):
                    saved = self._try_save_to(src, key, value)
                    break

        # GAP008: invalidate cache entry on successful write --------------
        if saved and key in self._retrieve_cache:
            del self._retrieve_cache[key]

        # --- Post-action storage capture (behavioral enforcement hook) ---
        # Guard against recursive enforcement when capture_outcome calls back into save()
        if saved and self._enforce_on_write:
            with self._enforcement_lock:
                if not self._in_enforcement_save:
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

        GAP008: configurable priority_order overrides default scorer ranking. LRU
        cache serves cached values before hitting source storage (with TTL).

        If enforcement-on-read is enabled, pre-decision recall runs before queries and
        any recalled context is injected into returned results so the caller also
        receives relevant memory that surfaced at a detected decision point.

        Args:
            key (str): Unique identifier for the memory

        Returns:
            Any: The memory content if found, None otherwise
        """
        # --- GAP008: check LRU cache first ---------------------------
        if key in self._retrieve_cache:
            cached_value, cached_ts = self._retrieve_cache[key]
            if time.monotonic() - cached_ts < self._cache_ttl:
                return cached_value  # cache hit (not expired)
            else:
                del self._retrieve_cache[key]  # expired

        # --- Pre-decision recall (behavioral enforcement hook) ---
        _recall_context: List[Dict[str, Any]] = []
        if self._enforce_on_read:
            with self._enforcement_lock:
                if not self._in_enforcement_recall:
                    em = self._get_enforcement_manager()
                    if em is not None:
                        try:
                            self._in_enforcement_recall = True
                            _recall_result = em.enforce(key)
                            _recall_context = getattr(_recall_result, 'recall_context', [])
                        except Exception:
                            pass  # degrade gracefully
                        finally:
                            self._in_enforcement_recall = False

        # GAP008: use priority_order if configured, else default scorer ranking
        if self._priority_order:
            candidate_sources = list(self._priority_order)
        else:
            candidate_sources = self._scorer.rank_sources(
                list(self.memory_sources.keys()),
            )

        # If recall fired and found exact-key hit, cache + return it early  ----------
        if _recall_context:
            for rec in _recall_context:
                if rec.get("key") == key:
                    self._retrieve_cache[key] = (rec.get("content", rec), time.monotonic())
                    self._evict_oldest_if_needed()
                    return rec.get("content", rec)

        for src_name in candidate_sources:
            source = self.memory_sources.get(src_name)
            if source and getattr(source, 'is_available', True) and self.is_source_enabled(src_name):
                result = source.retrieve(key)
                if result is not None:
                    self._retrieve_cache[key] = (result, time.monotonic())
                    self._evict_oldest_if_needed()
                    return result

        return None
    
    def clear_cache(self) -> None:
        """Clear the retrieval LRU cache (GAP008)."""
        self._retrieve_cache.clear()

    def _evict_oldest_if_needed(self) -> None:
        """Evict the oldest cached entry when the cache exceeds its size limit.

        Uses the monotonic timestamp stored as the second tuple element, so we
        actually evict the oldest entry instead of comparing full tuples
        element-by-element (which would break on mixed value types).
        """
        if len(self._retrieve_cache) > self._cache_max_size:
            oldest_key = min(
                self._retrieve_cache,
                key=lambda k: self._retrieve_cache[k][1],
            )
            del self._retrieve_cache[oldest_key]

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
            with self._enforcement_lock:
                if not self._in_enforcement_recall:
                    em = self._get_enforcement_manager()
                    if em is not None:
                        try:
                            self._in_enforcement_recall = True
                            _recall_result = em.enforce(query)
                            _recall_context = getattr(_recall_result, 'recall_context', [])
                        except Exception:
                            pass  # degrade gracefully — base search continues
                        finally:
                            self._in_enforcement_recall = False

        # Inject domain-level weightings before scoring
        all_results = []
        remaining_fetch_budget = limit  # cap on raw results collected from sources
        for source_name, source in self.memory_sources.items():
            if not source or not getattr(source, 'is_available', True) or not self.is_source_enabled(source_name):
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

        # Provenance filter: demote auto-generated session metadata results so real
        # user-authored content dominates search rankings. Auto-storage marks entries
        # with categories: ["RESULT"] — these echo query text and otherwise outrank
        # genuine documents through coincidental similarity.
        # GAP P0-1 FIX (2026-07-19): Also catch auto-tool-* string-typed artifacts that
        # lack the dict structure entirely, PLUS result-delivery markers for completeness.
        _AUTO_KEY_PREFIXES = ("auto-tool-", "result-", "auto-result-")
        def _is_auto_metadata(rr_obj):
            """Check if a RankedResult is auto-generated session metadata."""
            content = getattr(rr_obj, "content", None)

            # PATH 1: dict-typed content with RESULT/AUTO categories (existing path)
            if isinstance(content, dict):
                cat = content.get("categories", [])
                for tag in cat:
                    upper_tag = str(tag).upper()
                    if "RESULT" == upper_tag or "AUTO" == upper_tag:
                        return True

            # PATH 2: key-name pattern match (catches ALL auto-tool- and result- artifacts)
            key = getattr(rr_obj, "key", "")
            for prefix in _AUTO_KEY_PREFIXES:
                if key.startswith(prefix):
                    return True

            # PATH 3: raw string content without categories is almost certainly auto-generated
            # user-authored memories save as dicts with structured fields; tool dumps save as strings
            if isinstance(content, str) and len(content) > 500:
                try:
                    json.loads(content)
                    return True  # JSON-parsed string = machine-generated artifact
                except (json.JSONDecodeError, TypeError):
                    pass

            return False  # Default: NOT auto metadata

        PENALTY_FACTOR = 0.3  # Scale auto-metadata scores to 30% so real docs win
        for _rr in ranked:
            if _is_auto_metadata(_rr):
                _rr.score *= PENALTY_FACTOR

        # Re-sort descending (highest first) to preserve the contract that ranked[0]
        # holds the best result after penalty adjustment.
        ranked = sorted(ranked, key=lambda r: -r.score)

        # --- G3 fix: content-level dedup AFTER scoring but BEFORE truncation ---
        # Multiple keys can carry identical content (query-echo artifacts). Keep only the
        # highest-scored instance per unique content text, preserving ranking order.
        def _dedup_key(content_obj):
            """Extract just the readable content text for duplicate detection.

            The previous implementation used RelevanceScorer._extract_content_text which
            serialises every string value in a dict — timestamp, extra metadata fields,
            importance_score, etc.  That means two results with identical 'text' but
            different metadata produce different hashes and never collapse (t_cc003615).

            Strategy: extract only the user-visible content text from well-known keys,
            falling back to full extraction only when no structured content field exists.
            """
            if not content_obj:
                return ""
            if isinstance(content_obj, str):
                return " ".join(content_obj.lower().split())

            if isinstance(content_obj, dict):
                # Primary path: extract the 'text' field which holds the actual readable
                # content — this is used by hermes_default structured results
                raw_text = content_obj.get("text", "")
                if isinstance(raw_text, str) and raw_text.strip():
                    return " ".join(raw_text.lower().split())

                # Secondary path: dict without 'text' key — extract string values while
                # skipping per-key metadata that makes identical results look different.
                skip_keys = {"timestamp", "_timestamp", "importance_score",
                             "categories", "category", "outcome_type",
                             "score", "_domain"}
                parts = []
                for dk, val in content_obj.items():
                    if dk in skip_keys:
                        continue
                    if isinstance(val, str):
                        parts.append(val)
                    else:
                        try:
                            parts.append(str(val))
                        except Exception:
                            pass
                return " ".join(parts).lower()

            # Fallback — everything else treated as a flat string
            try:
                return " ".join(str(content_obj).lower().split())
            except Exception:
                return ""

        seen_content: Dict[str, int] = {}          # dedup_key -> ranked index
        pruned_ranked: List[RankedResult] = []
        dupes_removed = 0
        for r in ranked:
            ck = _dedup_key(getattr(r, 'content', ''))
            if ck not in seen_content:
                seen_content[ck] = len(seen_content)
                pruned_ranked.append(r)
            else:
                dupes_removed += 1
        ranked = pruned_ranked

        # Convert RankedResult -> plain dict with score field — use original limit
        results = [
            {
                "key": r.key,
                "content": r.content,
                "source": r.source,
                "score": r.score,  # <-- always present
                "preview": MemoryOrchestrator._synthesize_preview(r.content),
                **r.meta,
            }
            for r in ranked[:limit]
        ]

        if dupes_removed:
            logger.debug(
                "Content dedup removed %d duplicate items from %d total (before hash collapse)",
                dupes_removed, dupes_removed + len(ranked),
            )

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
                "preview": MemoryOrchestrator._synthesize_preview(r.content),
                **r.meta,
            }
            for r in scored
        ]
    
    @staticmethod
    def _synthesize_preview(content: Any) -> str:
        """Extract a readable preview string from whatever content structure a
        memory source returns. Different sources use different keys ('text',
        'content', 'summary', etc.) and some return plain strings or lists.

        Returns at most 200 characters so it fits in chat context without
        blowing token budgets.
        """
        if isinstance(content, str):
            truncated = content[:200] + "..." if len(content) > 200 else content
            return truncated
        elif isinstance(content, dict):
            # Try well-known readable keys in preference order
            for key in ("text", "content", "summary", "description", "note"):
                val = content.get(key)
                if isinstance(val, str) and val.strip():
                    t = val[:200] + "..." if len(val) > 200 else val
                    return t
            # Fallback: join the first few string values
            parts = [str(v) for v in content.values() if isinstance(v, str)]
            combined = " ".join(parts[:3])
            truncated = combined[:200] + "..." if len(combined) > 200 else combined
            return truncated
        elif isinstance(content, list):
            t = str(content)[:200] + "..." if len(str(content)) > 200 else str(content)
            return t if t else ""
        else:
            t = str(content)[:200] + "..." if content and len(str(content)) > 200 else (str(content) if content else "")
            return t

    def is_available(self) -> bool:
        """
        Check if the memory orchestrator and any sources are available.
        
        Returns:
            bool: True if at least one source is available, False otherwise
        """
        for source in self.memory_sources.values():
            if getattr(source, 'is_available', True):
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
                'version': __import__('memchorus').__version__,
                'default_source': self._default_source_name,
                'available_sources': len([s for s in self.memory_sources.values() if getattr(s, 'is_available', True)]),
                'total_sources': len(self.memory_sources)
            },
            'sources': {}
        }
        
        for name, source in self.memory_sources.items():
            source_info = source.get_source_info()
            source_info['enabled'] = self.is_source_enabled(name)
            info['sources'][name] = source_info

        return info

    def recommended_sources(
        self, write_type: str = "general", max_results: int = 3
    ) -> List[str]:
        """Return a ranked list of source names suitable for saving *write_type*.

        Honours three acceptance criteria (B-2 bug fix t_b9205369):
          AC1 - enabled gating: disabled sources never appear in results
          AC2 - priority tiering: higher priority_tier sources come first
          AC3 - write_restrictions: sources that refuse the write_type are excluded

        Args:
            write_type: Logical category of the data being written (default 'general')
            max_results: Maximum number of source names to return (default 3)

        Returns:
            List[str]: Ordered source names best suited for this write operation
        """
        ranked: List[tuple] = []

        for name, src in self.memory_sources.items():
            # AC1 — enabled gating
            if not self.is_source_enabled(name):
                continue

            # availability guard
            try:
                available = getattr(src, 'is_available', True)
            except (TypeError, AttributeError):
                # is_available may be unbound if it's a dataclass method stub or missing entirely
                available = True
            if not available:
                continue

            # AC3 — write restrictions (empty/missing list means "accepts everything")
            restriction_list = []
            try:
                cfg = getattr(src, 'config', {})
                raw = cfg.get("write_restrictions", [])
                if isinstance(raw, (list, tuple)):
                    restriction_list = [str(r).lower() for r in raw]
            except Exception:
                pass
            if restriction_list and write_type.lower() in restriction_list:
                continue

            # AC2 — priority tiering (default 0 when absent)
            try:
                cfg = getattr(src, 'config', {})
                tier = int(cfg.get("priority_tier", 0))
            except (ValueError, TypeError):
                tier = 0

            ranked.append((-tier, name))  # negate for descending sort

        ranked.sort()
        return [name for _, name in ranked[:max_results]]
