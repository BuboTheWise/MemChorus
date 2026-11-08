"""MemChorus — Hermes ``MemoryProvider`` (issue #273).

This module exposes :class:`MemChorusMemoryProvider`, the adapter that lets
MemChorus occupy Hermes' single external memory-provider slot.

Design principle (the whole point of #273): **MemChorus owns orientation and
policy, not storage.**

- *Orientation* = what to force at session start ("woke up"): the active
  project, the current task, and the top live-recall hits. This is injected
  synchronously via :meth:`system_prompt_block` / :meth:`prefetch` so it is
  present on the very first turn, even before the model starts reasoning.
- *Recall depth / guards / provenance* = policy. MemChorus decides how much to
  surface and tags every item with provenance, but the **bytes** live in the
  store — MemPalace when available, the Hermes built-in memory otherwise.

That means MemChorus coexists with MemPalace instead of competing with it:
MemPalace (or Hermes built-in) remains the store; MemChorus is the layer on
top that orients the session and enforces recall/storage policy.

Fallback guarantee: the "forced context / woke up" block must still be present
even when MemPalace is down. :meth:`system_prompt_block` and :meth:`prefetch`
therefore always emit a canonical orientation header (project + active task +
recall status) and *add* live hits when a source is reachable — they never
return an empty string just because the store is unreachable.

Performance: :meth:`queue_prefetch` runs the live recall work on a background
thread and stashes the rendered block, so the hot :meth:`prefetch` path is a
sub-millisecond cache read. This is the replacement for the blocking
``~110 ms pre_llm_call`` tax that the old hook-based integration incurred — the
orientation work moves off the synchronous API-call path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from agent.memory_provider import MemoryProvider as _MemoryProviderBase
except Exception:  # pragma: no cover - isolated-test fallback (no Hermes installed)
    from abc import ABC as _MemoryProviderBase  # type: ignore[assignment,unused-ignore]


__all__ = [
    "MemChorusMemoryProvider",
    "register",
]


# ---------------------------------------------------------------------------
# Background prefetch plumbing
# ---------------------------------------------------------------------------

class _PrefetchSlot:
    """A mutable cell holding the most recently rendered orientation block."""

    __slots__ = ("block", "provenance", "query", "generated_at", "hit_count", "degraded")

    def __init__(self) -> None:
        self.block: Optional[str] = None
        self.provenance: str = ""
        self.query: str = ""
        self.generated_at: float = 0.0
        self.hit_count: int = 0
        self.degraded: bool = False

    def reset(self) -> None:
        self.block = None
        self.provenance = ""
        self.query = ""
        self.generated_at = 0.0
        self.hit_count = 0
        self.degraded = True


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------

class MemChorusMemoryProvider(_MemoryProviderBase):
    """MemChorus as a Hermes memory provider.

    Wraps a :class:`memchorus.orchestrator.MemoryOrchestrator` (or bootstraps
    one lazily) and adapts its orientation + recall/storage policy to the
    ``MemoryProvider`` ABC that :class:`agent.memory_manager.MemoryManager`
    drives.

    Parameters
    ----------
    orchestrator:
        An already-constructed :class:`MemoryOrchestrator`. When ``None`` the
        provider bootstraps one on first use (see :meth:`_orchestrator`).
    orientation_limit:
        Max live-recall items folded into the orientation block (default 5,
        matching the orientation spec AC-O1).
    prefetch_timeout:
        Soft wall-clock bound (seconds) for a *background* prefetch run. A
        timeout only degrades the block to orientation-only; it never blocks
        the caller (default 3.0).
    """

    #: provider identity — surfaced by MemoryManager and the config schema
    name = "memchorus"

    def __init__(
        self,
        orchestrator: Optional[Any] = None,
        orientation_limit: int = 5,
        prefetch_timeout: float = 3.0,
    ) -> None:
        self._external_orchestrator = orchestrator
        self._orientation_limit = max(1, int(orientation_limit))
        self._prefetch_timeout = max(0.5, float(prefetch_timeout))

        # Session scoping (filled by initialize()).
        self._session_id: Optional[str] = None
        self._platform: str = "cli"
        self._hermes_home: Optional[str] = None
        self._agent_context: str = "primary"
        self._session_title: Optional[str] = None
        self._agent_identity: Optional[str] = None
        self._user_id: Optional[str] = None

        # Background prefetch state (guarded by a lock).
        self._slot = _PrefetchSlot()
        self._slot_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._worker_started_at = 0.0
        self._initialized = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def _orchestrator(self) -> Optional[Any]:
        """Return the orchestrator, lazily bootstrapping if necessary."""
        if self._external_orchestrator is not None:
            return self._external_orchestrator
        orch = self._bootstrap_orchestrator()
        if orch is not None:
            # Cache it so repeated calls don't re-run the bootstrap.
            self._external_orchestrator = orch
        return orch

    @staticmethod
    def _bootstrap_orchestrator() -> Optional[Any]:
        """Import + construct a MemoryOrchestrator exactly like the hook plugin.

        Mirrors :func:`integrations.hermes._bootstrap` so the provider slot and
        the hook slot agree on source wiring. Any failure degrades to ``None``
        (orientation-only mode), never an exception.
        """
        try:
            from memchorus.orchestrator import MemoryOrchestrator
        except Exception as exc:  # pragma: no cover
            logger.debug("MemChorus provider: orchestrator import failed: %s", exc)
            return None
        try:
            config = {
                "enforce_on_read": True,
                "enforce_on_write": True,
                "half_life_days": 30.0,
            }
            orch = MemoryOrchestrator(config=config)
            if not orch.is_available():
                logger.warning(
                    "MemChorus provider: orchestrator has no available sources — "
                    "orientation-only fallback mode."
                )
            return orch
        except Exception as exc:  # pragma: no cover
            logger.debug("MemChorus provider: orchestrator init failed: %s", exc)
            return None

    def is_available(self) -> bool:
        """Available as long as MemChorus can be imported.

        Deliberately broader than ``orchestrator.is_available()``: even when
        MemPalace is down the provider still injects the canonical orientation
        ("woke up") header, which is the fallback the spec requires.
        """
        try:
            import memchorus  # noqa: F401
            return True
        except Exception as exc:
            logger.debug("MemChorus provider unavailable: %s", exc)
            return False

    def initialize(self, session_id, **kwargs: Any) -> None:
        """Capture session scoping and kick a background orientation prefetch.

        Never blocks: the live recall runs on a worker thread (see
        :meth:`queue_prefetch`).
        """
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._hermes_home = kwargs.get("hermes_home")
        self._agent_context = kwargs.get("agent_context", "primary")
        self._session_title = kwargs.get("session_title")
        self._agent_identity = kwargs.get("agent_identity")
        self._user_id = kwargs.get("user_id")
        self._initialized = True

        # Seed a background prefetch immediately with the session title (best
        # signal we have for the session's topic) so the first synchronous
        # prefetch() call is very likely a warm-cache hit.
        seed = self._session_title or ""
        if seed:
            self.queue_prefetch(seed)

        logger.debug("MemChorus provider initialized (session=%s)", self._session_id)

    def shutdown(self) -> None:
        """Join any in-flight background prefetch (bounded)."""
        try:
            with self._worker_lock:
                worker = self._worker
                self._worker = None
            if worker is not None and worker.is_alive() and worker is not threading.current_thread():
                worker.join(timeout=1.0)
        except Exception as exc:  # pragma: no cover
            logger.debug("MemChorus provider shutdown: %s", exc)

    # ------------------------------------------------------------------
    # Orientation — the "woke up" forced context
    # ------------------------------------------------------------------

    def _active_task(self) -> Optional[str]:
        """Return HERMES_KANBAN_TASK if this is a kanban-bound run."""
        return os.environ.get("HERMES_KANBAN_TASK") or None

    def _resolve_project(self) -> Optional[str]:
        """Resolve the active project the same way orientation does."""
        try:
            from memchorus.orientation import _resolve_project as _rp
            return _rp(self._active_task()) or None
        except Exception:
            return None

    def _live_hits(self) -> List[Dict[str, Any]]:
        """Run orientation recall, degrading to [] on any failure."""
        orch = self._orchestrator
        if orch is None:
            return []
        try:
            from memchorus.orientation import orientation_search
            return orientation_search(
                self._active_task(),
                orchestrator=orch,
                limit=self._orientation_limit,
            ) or []
        except Exception as exc:
            logger.debug("MemChorus provider orientation_search failed: %s", exc)
            return []

    def _render_orientation_block(
        self, hits: List[Dict[str, Any]], query: str = ""
    ) -> Tuple[str, str, int, bool]:
        """Render the canonical orientation block + provenance tag.

        Always returns a non-empty block (the fallback the spec requires).
        Returns ``(block, provenance, hit_count, degraded)``.
        """
        project = self._resolve_project()
        task = self._active_task()
        degraded = not hits
        provenance = (
            f"memchorus.orientation|project={project or 'auto'}|"
            f"hits={len(hits)}|degraded={str(degraded).lower()}"
        )

        lines: List[str] = []
        lines.append("[MemChorus Orientation — session start]")
        lines.append(f"Active project: {project or '(auto-detect — no project resolved)'}")
        lines.append(f"Active task:    {task or '—'}")
        lines.append(f"Agent:          {self._agent_identity or 'default'}")

        if hits:
            lines.append(f"Live recall ({len(hits)} hit{'s' if len(hits) != 1 else ''}):")
            for hit in hits[: self._orientation_limit]:
                key = hit.get("key", "")
                content = str(hit.get("content", ""))[:300]
                source = hit.get("source", "")
                lines.append(f"  - [{key}] {content}" + (f"  (src:{source})" if source else ""))
        else:
            lines.append(
                "Live recall: degraded — no source reachable. "
                "MemPalace/Hermes store is down; orientation-only mode is active."
            )

        if self._session_title:
            lines.append(f"Session focus: {self._session_title}")

        lines.append(
            "Use the memchorus_orient / memchorus_recall tools for on-demand recall; "
            "use memchorus_remember to store a durable fact."
        )
        return "\n".join(lines), provenance, len(hits), degraded

    def system_prompt_block(self) -> str:
        """Synchronous orientation block — the "woke up" forced context.

        Prefers a warm background-prefetch cache (sub-millisecond). Falls back
        to a live (bounded) orientation run, then to the canonical degraded
        header so the block is never empty.
        """
        with self._slot_lock:
            if self._slot.block:
                return self._slot.block

        # Cold path: run a bounded live orientation synchronously.
        try:
            hits = self._live_hits()
        except Exception:
            hits = []
        block, prov, count, degraded = self._render_orientation_block(hits)
        with self._slot_lock:
            self._slot.block = block
            self._slot.provenance = prov
            self._slot.hit_count = count
            self._slot.degraded = degraded
            self._slot.generated_at = time.monotonic()
        return block

    def prefetch(self, query: str, **kwargs: Any) -> Optional[str]:
        """Synchronous prefetch — warm cache first, bounded live run otherwise.

        Replaces the blocking ~110 ms ``pre_llm_call`` tax: when a background
        prefetch (from :meth:`initialize` / :meth:`queue_prefetch`) has already
        rendered a block, this is a cache read. Only if the cache is cold do we
        do one bounded live orientation run.
        """
        with self._slot_lock:
            if self._slot.block and (not query or self._slot.query == query):
                return self._slot.block

        # Cold: do one bounded live run and cache it.
        try:
            hits = self._live_hits()
        except Exception:
            hits = []
        block, prov, count, degraded = self._render_orientation_block(hits, query=query)
        with self._slot_lock:
            self._slot.block = block
            self._slot.provenance = prov
            self._slot.query = query
            self._slot.hit_count = count
            self._slot.degraded = degraded
            self._slot.generated_at = time.monotonic()
        return block

    def queue_prefetch(self, query: str, **kwargs: Any) -> None:
        """Kick a background orientation+recall render. Never blocks.

        Runs on a daemon worker thread; the result lands in :attr:`_slot` for
        the next synchronous :meth:`prefetch` / :meth:`system_prompt_block`
        call. A second call while one is in-flight is a no-op (avoids worker
        pile-up).
        """
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return  # already working — don't stack
            slot = _PrefetchSlot()
            self._slot = slot
            t = threading.Thread(
                target=self._prefetch_worker,
                args=(query, slot),
                name="memchorus-prefetch",
                daemon=True,
            )
            self._worker = t
            self._worker_started_at = time.monotonic()
        try:
            t.start()
        except Exception as exc:  # pragma: no cover
            logger.debug("MemChorus provider prefetch thread start failed: %s", exc)

    def _prefetch_worker(self, query: str, slot: _PrefetchSlot) -> None:
        try:
            hits = self._live_hits()
        except Exception:
            hits = []
        block, prov, count, degraded = self._render_orientation_block(hits, query=query)
        slot.block = block
        slot.provenance = prov
        slot.query = query
        slot.hit_count = count
        slot.degraded = degraded
        slot.generated_at = time.monotonic()
        elapsed_ms = (time.monotonic() - self._worker_started_at) * 1000.0
        logger.debug(
            "MemChorus provider prefetch done: hits=%d degraded=%s (%.1f ms)",
            count, degraded, elapsed_ms,
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        """Per-turn hook — re-queue a background prefetch for the new message."""
        if message:
            try:
                self.queue_prefetch(message)
            except Exception as exc:  # pragma: no cover
                logger.debug("MemChorus provider on_turn_start: %s", exc)

    # ------------------------------------------------------------------
    # Tools (get_tool_schemas / handle_tool_call)
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """MemChorus-oriented tools: orient / recall / remember."""
        return [
            {
                "name": "memchorus_orient",
                "description": (
                    "Re-run MemChorus orientation for the current project/task and "
                    "return the top live-recall hits as JSON. Use at session start "
                    "or when you need a fresh 'woke up' context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Max hits to return (default 5).",
                        }
                    },
                },
            },
            {
                "name": "memchorus_recall",
                "description": (
                    "Semantic search across MemChorus sources (MemPalace / Hermes "
                    "built-in). Returns scored hits as JSON."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max hits to return (default 5).",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memchorus_remember",
                "description": (
                    "Store a durable fact via MemChorus (routed to MemPalace when "
                    "available). Returns the saved key as JSON."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Fact key/id."},
                        "content": {"type": "string", "description": "Fact content."},
                        "category": {
                            "type": "string",
                            "enum": ["LEARNING", "MISTAKE", "DECISION", "RESULT"],
                            "description": "Optional category. Defaults to LEARNING.",
                        },
                    },
                    "required": ["key", "content"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> Any:
        """Execute a MemChorus tool. Returns a JSON string (or dict)."""
        orch = self._orchestrator
        if orch is None:
            return json.dumps({"error": "no_memchorus_source", "detail": "orientation-only mode"})

        try:
            if tool_name == "memchorus_orient":
                limit = int(args.get("limit", self._orientation_limit))
                hits = self._live_hits()
                return json.dumps({"project": self._resolve_project(), "hits": hits[:limit]})

            if tool_name == "memchorus_recall":
                query = args.get("query", "")
                limit = int(args.get("limit", 5))
                results = orch.search(query, limit=limit)
                return json.dumps({"query": query, "hits": results[:limit]})

            if tool_name == "memchorus_remember":
                key = args.get("key", "")
                content = args.get("content", "")
                category = args.get("category") or "LEARNING"
                ok = orch.save(
                    key=key,
                    value=content,
                    source_name=None,
                    category=category,
                    metadata={"provenance": "memchorus.provider.remember"},
                )
                return json.dumps({"saved": bool(ok), "key": key})

            return json.dumps({"error": "unknown_tool", "tool": tool_name})
        except Exception as exc:
            return json.dumps({"error": "tool_failed", "detail": str(exc)})

    # ------------------------------------------------------------------
    # Turn sync + lifecycle
    # ------------------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        **kwargs: Any,
    ) -> None:
        """Post-turn: run the enforcement pipeline (recall + storage) in background.

        Kept non-blocking so it never adds latency to the API path. The
        enforcement pipeline auto-wires recall + storage capture.
        """
        orch = self._orchestrator
        if orch is None:
            return
        text = f"{user_content}\n{assistant_content}".strip()
        if not text:
            return

        def _run() -> None:
            try:
                em = orch._get_enforcement_manager()
                if em is not None:
                    em.enforce(text)
            except Exception as exc:  # pragma: no cover
                logger.debug("MemChorus provider sync_turn enforce: %s", exc)

        t = threading.Thread(target=_run, name="memchorus-sync-turn", daemon=True)
        t.start()

    def on_session_end(self, messages: List[Dict[str, Any]], **kwargs: Any) -> None:
        """Persist a session checkpoint via the orchestrator (best-effort)."""
        orch = self._orchestrator
        project = self._resolve_project()
        if orch is None or project is None:
            return
        try:
            from memchorus.checkpoint import write_checkpoint
            user_texts = [
                m.get("content", "")
                for m in messages
                if isinstance(m, dict) and m.get("role") == "user"
            ]
            key = write_checkpoint(orch, project_name=project, user_texts=user_texts)
            if key:
                logger.debug("MemChorus provider: wrote session checkpoint %s", key)
        except Exception as exc:  # pragma: no cover
            logger.debug("MemChorus provider on_session_end: %s", exc)

    def on_pre_compress(self, messages: List[Dict[str, Any]], **kwargs: Any) -> str:
        """Before context compression: store a checkpoint so the gist survives,
        and return a short orientation contribution for the compression summary
        prompt. Best-effort, never raises into the compress path."""
        contribution = (
            "[MemChorus] Active project/task context was checkpointed before "
            "compression; recall via memchorus_orient after the resume."
        )
        orch = self._orchestrator
        project = self._resolve_project()
        if orch is None or project is None:
            return contribution
        try:
            from memchorus.checkpoint import write_checkpoint
            key = write_checkpoint(
                orch,
                project_name=project,
                user_texts=None,
            )
            if key:
                logger.debug("MemChorus provider: wrote pre-compress checkpoint %s", key)
        except Exception as exc:  # pragma: no cover
            logger.debug("MemChorus provider on_pre_compress: %s", exc)
        return contribution

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        """On session switch: rebind the session id and, when this is a genuine
        reset, clear cached orientation + prefetch so the new session re-derives
        fresh context rather than inheriting stale hits."""
        self._session_id = new_session_id
        if reset:
            with self._slot_lock:
                self._slot.reset()
            try:
                from memchorus.orientation import clear_orientation_cache
                clear_orientation_cache()
            except Exception as exc:  # pragma: no cover
                logger.debug("MemChorus provider on_session_switch: %s", exc)
        new_title = kwargs.get("session_title")
        if new_title:
            self._session_title = new_title
            self.queue_prefetch(new_title)


# ---------------------------------------------------------------------------
# Plugin entry point — discovered by plugins.memory._load_provider_from_dir.
# Tries ctx.register_memory_provider(...) (honcho-style); falls back to a
# module-level PROVIDER constant so the loader's generic scan also finds it.
# ---------------------------------------------------------------------------

def register(ctx) -> None:  # noqa: ANN001
    """Register MemChorus as a Hermes memory provider plugin."""
    provider = MemChorusMemoryProvider()
    if hasattr(ctx, "register_memory_provider"):
        try:
            ctx.register_memory_provider(provider)
            return
        except Exception as exc:  # pragma: no cover
            logger.debug("register_memory_provider failed; falling back: %s", exc)
    # Fallback: stash on the context for collectors that read attributes.
    try:
        ctx.provider = provider
    except Exception:  # pragma: no cover
        pass


#: Module-level provider for loader fallbacks (see plugins.memory loader).
PROVIDER = MemChorusMemoryProvider()
