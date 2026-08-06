"""orientation.py -- Session-oriented recall subsystem for MemChorus v1.2.

Provides automatic project context injection at session start (on_session_start
hook) by combining KG triples and semantic search results, limited to 5 items.

Key properties:
* Respects the existing LRU cache (cache_ttl_seconds: 60) so repeated calls
  within the TTL return instantly without an MCP round-trip.
* Silent empty result handling -- if project is detected but has no associated
  memories, returns ``[]`` silently (no warning log).
* Only logs when something genuinely goes wrong (MCP unreachable).

Query construction priority chain:
    HERMES_KANBAN_TASK -> workspace dir name -> silent skip.
"""

import functools
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tunables — set at import time or overridden during bootstrap (by auto_bootstrap).
# Reduced from 60s to 15s so that project context changes propagate quickly.
# Empty results are never cached (see _CacheRegistry.put) regardless of TTL.
DEFAULT_CACHE_TTL_SECONDS: float = 15.0


# --------------------------------------------------------------------------- #
# Cache helpers                                                               #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _CacheKey:
    """Immutable cache key built from the detected project context."""
    project: str
    query_types: Tuple[str, ...]  # e.g. ("kg", "semantic")


@dataclass
class _CacheEntry:
    results: List[Dict[str, Any]]
    timestamp: float  # time.monotonic() when populated
    ttl: int


# --------------------------------------------------------------------------- #
# LRU cache for orientation queries                                           #
# --------------------------------------------------------------------------- #

class _CacheRegistry:
    """Simple LRU-like cache keyed by ``_CacheKey`` with TTL eviction.

    Key behaviours:
    - Empty result lists are NEVER cached (prevents poison entries after
      project switches or when orchestrator is unavailable).
    - ``clear_project(project)`` invalidates all entries for a specific
      project context without touching unrelated keys.
    """

    def __init__(self, *, maxsize: int = 256) -> None:
        self._cache: Dict[_CacheKey, _CacheEntry] = {}
        self._maxsize = maxsize

    def get(self, key: _CacheKey, ttl_override: Optional[float] = None) -> Optional[List[Dict[str, Any]]]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        effective_ttl = float(ttl_override) if ttl_override else float(entry.ttl)
        if time.monotonic() - entry.timestamp > effective_ttl:
            # Expired – remove it
            del self._cache[key]
            return None
        return entry.results

    def put(self, key: _CacheKey, results: List[Dict[str, Any]], ttl_seconds: float) -> None:
        # Do NOT cache empty lists — they indicate orchestrator unavailable or
        # genuinely no data and would poison subsequent calls for the same
        # project context until TTL expiry.
        if not results:
            return

        if len(self._cache) >= self._maxsize:
            oldest = min(self._cache, key=lambda k: self._cache[k].timestamp)
            del self._cache[oldest]
        self._cache[key] = _CacheEntry(results=results, timestamp=time.monotonic(), ttl=int(ttl_seconds))

    def clear_project(self, project: str) -> None:
        """Invalidate all entries whose cache key matches *project*.

        Call this when the project context changes so that stale data
        doesn't persist after a project switch.
        """
        stale_keys = [k for k in self._cache if k.project == project]
        for k in stale_keys:
            del self._cache[k]

    def clear(self) -> None:
        self._cache.clear()
_cache = _CacheRegistry()


# --------------------------------------------------------------------------- #
# Query construction                                                          #
# --------------------------------------------------------------------------- #

def _build_orientation_query(
    env_task: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build the list of orientation queries for the current project context.

    Priority chain determines *project*:
        1. HERMES_KANBAN_TASK (Kanban task ID / slug)
        2. HERMES_WORKSPACE env var (workspace path -- basename used as fallback)
        3. Current working directory (basename)
        4. ``None`` -- silent skip

    Returns:
        List of query dicts with keys "type" (kg/semantic) and "query" (string).
        Empty list when no project detected -- caller should treat as silent skip.
    """
    project = _resolve_project(env_task)
    if project is None:
        return []  # silent skip

    return [
        {"type": "kg", "query": f"{project} relationship entity"},
        {"type": "semantic", "query": f"session context {project} current task"},
    ]


_KANBAN_HEX_RE = re.compile(r"^t_[0-9a-f]{8}$", flags=re.IGNORECASE)


def _is_hermez_project_name(name: str) -> bool:
    """Return True if *name* looks like a real project name rather than a skip value.

    Hex Kanban task IDs (e.g. ``t_a1b2c3d4``) are NOT useful as query terms, so they
    return False and cause the caller to fall through the priority chain.
    """
    if not name:
        return False
    if _KANBAN_HEX_RE.match(name):
        return False
    return True


def _resolve_project(env_task: Optional[str]) -> Optional[str]:
    """Return the project identifier or None (silent skip).

    Priority chain:
        1. HERMES_KANBAN_TASK (skipped when it matches a hex Kanban ID pattern)
        2. HERMES_WORKSPACE env var (workspace directory basename)
        3. Current working directory (basename)
        4. ``None`` -- silent skip
    """
    # 1. Kanban task ID (highest priority — but skip hex IDs)
    if env_task and env_task.strip() and _is_hermez_project_name(env_task.strip()):
        return env_task.strip()

    # 2. HERMES_WORKSPACE env var (workspace directory basename)
    workspace = os.environ.get("HERMES_WORKSPACE")
    if workspace and workspace.strip():
        return os.path.basename(os.path.normpath(workspace))  # type: ignore[return-value]

    # 3. Current working directory fallback
    cwd = os.getcwd()
    if cwd:
        return os.path.basename(cwd)  # type: ignore[return-value]

    return None


# --------------------------------------------------------------------------- #
# Orientation search (executes queries through orchestrator, respects cache)  #
# --------------------------------------------------------------------------- #

def orientation_search(
    env_task: Optional[str],
    orchestrator: Any = None,  # MemoryOrchestrator -- injected at runtime
    limit: int = 5,
    cache_ttl_seconds: float = 60.0,
) -> List[Dict[str, Any]]:
    """Run orientation queries and return combined results (kg + semantic).

    This function is the bridge between ``on_session_start`` and ``orchestrator.search``.
    It:
        1. Calls ``_build_orientation_query()`` to get query specs.
        2. Checks local cache first -- hits served without MCP round-trip.
        3. Executes queries against *orchestrator* (or the global memory sources).
        4. Merges up to *limit* results into a single list, de-duplicated by key.

    Args:
        env_task: Value of HERMES_KANBAN_TASK (may be ``None``).
        orchestrator: MemoryOrchestrator instance for executing queries.
        limit: Maximum number of results to return (default 5 per spec AC-O1).
        cache_ttl_seconds: LRU cache TTL in seconds (default 60).

    Returns:
        List of memory items (dicts with "key", "content", "source", "score"),
        or ``[]`` silently when project undetected.
    """
    queries = _build_orientation_query(env_task)
    if not queries:
        return []  # silent skip -- no project detected

    cache_key = _CacheKey(
        project=_resolve_project(env_task) if env_task else os.environ.get("HERMES_TASK", ""),
        query_types=tuple(q["type"] for q in queries),
    )

    # Check cache first (AC-O2: repeated calls return instantly)
    cached = _cache.get(cache_key, cache_ttl_seconds)
    if cached is not None:
        return cached[:limit]  # enforce limit even on hit

    all_results: List[Dict[str, Any]] = []
    seen_keys: set = set()

    for qdef in queries:
        results = _execute_query(qdef, orchestrator)
        # De-duplicate by "key" field -- first occurrence wins.
        for r in results:
            k = r.get("key", str(r))
            if k not in seen_keys:
                seen_keys.add(k)  # type: ignore[arg-type]  -- key is always a string after set add
                all_results.append(r)

    # Cap to limit (AC-O1: up to 5 items) — score-based so higher-relevance results
    # survive truncation rather than being position-dependent on query execution order.
    all_results = sorted(all_results, key=lambda r: r.get("score", 0), reverse=True)[:limit]

    # Write to LRU cache (AC-O2)
    _cache.put(cache_key, all_results, cache_ttl_seconds)

    return all_results


def _execute_query(
    qdef: Dict[str, str],
    orchestrator: Any = None,
) -> List[Dict[str, Any]]:
    """Execute a single orientation query against the orchestrator.

    If orchestrator is None or unavailable, returns [].
    Always degrades silently -- no warnings for empty results (AC-O3).
    Only logs errors when MCP or orchestrator raises.
    """
    qtype = qdef["type"]
    query_str = qdef["query"]

    # --- Branch based on type to use the right executor strategy -----------
    if qtype == "kg" and orchestrator is not None:
        try:
            return orchestrator.search(query_str, limit=5)
        except Exception as exc:
            logger.warning("Orientation KG query failed -- skipping. %s", exc)
            return []

    if qtype == "semantic" and orchestrator is not None:
        try:
            return orchestrator.search(query_str, limit=5)
        except Exception as exc:
            logger.warning("Orientation semantic query failed -- skipping. %s", exc)
            return []

    # If orchestrator is missing we fall through silently (AC-O3).
    logger.debug(
        "No orchestrator available for %s query '%s' -- returning empty.", qtype, query_str,
    )
    return []


# --------------------------------------------------------------------------- #
# Cache purge (for testing / manual management)                               #
# --------------------------------------------------------------------------- #

def clear_orientation_cache() -> None:
    """Clear all cached orientation results.  Useful for testing."""
    # Import ourselves so we always hit the actual module-level _cache, even
    # when a previous test has monkeypatched/removed our local reference.
    import memchorus.orientation as _mod
    _mod._cache.clear()
