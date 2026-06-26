"""MemPalace Memory Source Adapter - v2.1

Provides real integration with the MemPalace knowledge graph and diary system
via MCP stdio transport using the ``mcp`` Python SDK (v1.x).

Fallback behaviour: when MCP is unreachable the source degrades to a local
file cache so the orchestrator never loses its enhancement voice.
"""
import json
import logging
import os
import re
import shutil
import sys
import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from memchorus.memory_source import MemorySource

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Execute an async coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _McpClient:
    """Minimal stdio client for MemPalace.

    Strategy (v2.1): open a **fresh subprocess + session per tool call**.
    This avoids the impossible lifecycle problem of keeping an async ClientSession
    alive inside a synchronous class while its internal reader/writer tasks
    would be dead once they leave their context manager.

    The cost is starting a Python subprocess for each operation, but MemChorus
    save/retrieve/search are not firehose operations -- the overhead is acceptable.
    """

    def __init__(self, timeout: float = 30.0, config: Optional[Dict[str, Any]] = None):
        self.timeout = float(timeout)
        self._connected = False
        self._config = config or {}
        # Discover a suitable Python interpreter instead of assuming pipx
        self._python_bin = self._discover_python()

    def _discover_python(self) -> str:
        """Discover a Python interpreter for the MCP subprocess.

        Discovery chain (highest → lowest priority):
        1. ``config.get("python_bin")`` -- explicit user override
        2. ``shutil.which("mempalace-python")`` -- dedicated shim on PATH
        3. pipx venv locations:
           - ``~/.local/share/pipx/venvs/mempalace/bin/python``
           - ``~/.local/pipx/venvs/mempalace/bin/python``
        4. ``sys.executable`` -- shares env with this process
        5. ``python3`` on PATH -- system-wide / conda environments
        6. ``/usr/bin/python3`` as absolute fallback

        Returns the first candidate confirmed to exist and be executable, plus logs
        a diagnostic explaining how the path was resolved.
        """
        # Step 1: explicit config override
        user_path = self._config.get("python_bin", None)
        if user_path:
            expanded = os.path.expanduser(user_path)
            real = os.path.realpath(expanded)
            if Path(real).exists():
                logger.info(
                    "python_bin resolved via explicit config override: %s", real
                )
                return real
            else:
                logger.warning(
                    "config.python_bin points to non-existent file: %s (expanded: %s) -- skipping",
                    user_path, expanded,
                )

        # Step 2: dedicated PATH shim
        mp_python = shutil.which("mempalace-python")
        if mp_python:
            logger.info(
                "python_bin resolved via PATH shim (mempalace-python): %s", mp_python
            )
            return mp_python

        # Step 3: pipx venv locations
        for pipx_candidate in [
            os.path.expanduser("~/.local/share/pipx/venvs/mempalace/bin/python"),
            os.path.expanduser("~/.local/pipx/venvs/mempalace/bin/python"),
        ]:
            if Path(pipx_candidate).is_file():
                logger.info(
                    "python_bin resolved via pipx venv: %s", pipx_candidate
                )
                return pipx_candidate

        # Step 4: sys.executable (shares env with this process)
        py = sys.executable
        if Path(py).exists():
            logger.info(
                "python_bin resolved via sys.executable (same env): %s", py
            )
            return py

        # Step 5: python3 on PATH
        py3 = shutil.which("python3")
        if py3:
            logger.info(
                "python_bin resolved via python3 on PATH: %s", py3
            )
            return py3

        # Step 6: absolute fallback
        abs_fallback = "/usr/bin/python3"
        if Path(abs_fallback).exists():
            logger.warning(
                "python_bin fell back to absolute path (no other candidates): %s", abs_fallback
            )
            return abs_fallback

        # All paths exhausted -- sys.executable is our best guess
        logger.warning(
            "python_bin could not verify any candidate via Path.exists or shutil.which; "
            "falling back to sys.executable: %s", py
        )
        return py

    def connect(self) -> bool:
        """Start server subprocess, run initialize handshake, return True/False."""
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client.session import ClientSession

        python_bin = self._python_bin
        if not Path(python_bin).exists():
            return False

        server_params = StdioServerParameters(
            command=python_bin,
            args=["-m", "mempalace.mcp_server"],
        )

        async def _do_init():
            async with stdio_client(server_params) as (r_stream, w_stream):
                async with ClientSession(
                    read_stream=r_stream,
                    write_stream=w_stream,
                    read_timeout_seconds=timedelta(seconds=self.timeout),
                ) as session:
                    await session.initialize()

        try:
            _run_async(asyncio.wait_for(_do_init(), timeout=self.timeout))
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False

    # -- tool calling ---------------------------------------------------------------

    def _call(self, name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Open subprocess + session, run one tool call, clean up."""
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client.session import ClientSession

        python_bin = self._python_bin
        if not Path(python_bin).exists():
            return None

        server_params = StdioServerParameters(
            command=python_bin,
            args=["-m", "mempalace.mcp_server"],
        )

        async def _do_call():
            async with stdio_client(server_params) as (r_stream, w_stream):
                async with ClientSession(
                    read_stream=r_stream,
                    write_stream=w_stream,
                    read_timeout_seconds=timedelta(seconds=self.timeout),
                ) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments=arguments)
            return result

        try:
            result = _run_async(asyncio.wait_for(_do_call(), timeout=self.timeout * 2))
            texts = []
            if hasattr(result, "content") and result.content:
                for block in result.content:
                    if hasattr(block, "text"):
                        texts.append(str(block.text))
            raw = "\n".join(texts) if texts else ""
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {"raw": raw} if raw else {}
        except Exception:
            return None

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call a MemPalace MCP tool.  Returns parsed dict or None."""
        if not self._connected:
            return None
        return self._call(name, arguments)

    # -- convenience wrappers -------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> Optional[List[Dict[str, Any]]]:
        """mempalace_search -> list of dicts."""
        result = self.call_tool("mempalace_search", {"query": query, "limit": limit})
        if result is None:
            return None

        data = result.get("result", result) if isinstance(result, dict) else result
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return [{"raw_text": data}]

        return data if isinstance(data, list) else (
            [data] if isinstance(data, dict) else []
        )

    def add_drawer(self, wing: str, room: str, content: str) -> bool:
        """mempalace_add_drawer -> True on success."""
        result = self.call_tool(
            "mempalace_add_drawer",
            {"wing": wing, "room": room, "content": content},
        )
        if result is None:
            return False

        text_result = (
            str(result.get("result", ""))
            if isinstance(result, dict)
            else str(result)
        ).lower()
        for err_word in ("error", "failed", "not found"):
            if err_word in text_result:
                return False
        return True

    def kg_query(self, entity: str) -> Optional[List[Dict[str, Any]]]:
        """mempalace_kg_query -> list of fact dicts."""
        result = self.call_tool("mempalace_kg_query", {"entity": entity})
        if result is None:
            return None

        data = result.get("result", result) if isinstance(result, dict) else result
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, list) else [parsed]
            except (json.JSONDecodeError, TypeError):
                return [{"entity": entity, "raw_text": data}]

        return data if isinstance(data, list) else (
            [data] if isinstance(data, dict) else []
        )

    @property
    def is_alive(self) -> bool:
        # With per-call subprocess model, `is_alive` means the connection was
        # probed successfully at init time.  Individual calls may still fail,
        # which is handled by returning None / triggering fallback.
        return self._connected


# --- Memory source implementation --------------------------------------------------------

class MemPalaceMemorySource(MemorySource):
    """Memory source backed by the live MemPalace MCP server with local fallback.

    When the MCP connection succeeds, save/retrieve/search route through real
    MemPalace tools (wing = ``memchorus``).  If the MCP server is unreachable at
    init time or crashes mid-flight, operations silently fall back to a local
    JSON cache directory so the orchestrator continues functioning.

    Configuration keys (passed via *config*):

    ``cache_dir``   Local fallback path (default ``~/.hermes/mempalace_cache``).
    ``mcp_timeout`` Seconds before an MCP call is considered failed (default 10).
    ``skip_mcp``    If true, skip live MCP connection entirely (local fallback only).
                   Useful for testing (default false).
    ``python_bin``  Override the auto-detected Python interpreter for the MCP subprocess.
                    Accepts an absolute path or a path relative to ``~``. When set the
                    discovery chain skips directly to verifying the candidate.
    """

    def __init__(
        self,
        name: str = "mempalace",
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(name, config)
        self._name = name
        self.config = config or {}

        # Fallback local cache.
        self._cache_dir = Path(
            self.config.get("cache_dir", os.path.expanduser("~/.hermes/mempalace_cache"))
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # MCP client -- attempts a real connection eagerly unless disabled.
        mcp_timeout = float(self.config.get("mcp_timeout", 10))
        self._client = _McpClient(timeout=mcp_timeout, config=self.config)
        self._connected = False

        if not self.config.get("skip_mcp", False):
            try:
                self._connected = self._client.connect()
            except Exception:
                # Connection failed -- continue in fallback mode.
                self._connected = False

    # --- MemorySource abstract methods ------------------------------------------

    def save(self, key: str, value: Any) -> bool:
        """Persist the memory.  Tries MCP first; falls back to local cache."""
        content = self._to_str(value)

        if self._connected and self._client.is_alive:
            room = self._key_to_room(key)
            ok = self._client.add_drawer(
                wing="memchorus", room=room, content=content
            )
            if ok:
                # Mirror locally for resilience.
                self._cache_locally(key, value)
                return True

        # MCP unavailable or call failed -> local cache only.
        return bool(self._cache_locally(key, value))

    def retrieve(self, key: str) -> Optional[Any]:
        """Look up the memory.  Tries MCP search first; falls back to local."""
        if self._connected and self._client.is_alive:
            results = self._client.search(query=key, limit=3)
            if results:
                for r in results:
                    r_content = (
                        r.get("content", "") if isinstance(r, dict) else str(r)
                    )
                    if key.lower() in str(r_content).lower():
                        return self._from_str(str(r_content))

        # Local cache fallback.
        filepath = self._cache_dir / f"{key}.json"
        if filepath.exists():
            try:
                with open(filepath) as f:
                    return json.load(f)
            except Exception:
                pass

        return None

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search across MCP + local cache, deduplicating by key."""
        results: List[Dict[str, Any]] = []
        seen_keys: set = set()

        if self._connected and self._client.is_alive:
            mp_results = self._client.search(query=query, limit=limit)
            if mp_results:
                for r in mp_results:
                    wing = r.get("wing", "unknown") if isinstance(r, dict) else None
                    room = r.get("room", "unknown") if isinstance(r, dict) else None
                    comp_key = f"{wing}/{room}" if wing and room else query

                    content_val = (
                        r.get("content", str(r)) if isinstance(r, dict) else str(r)
                    )
                    entry: Dict[str, Any] = {
                        "key": comp_key,
                        "content": self._from_str(str(content_val)),
                        "source": self._name,
                    }
                    if "similarity" in r:
                        entry["score"] = r["similarity"]
                    results.append(entry)
                    seen_keys.add(comp_key)

        # Also search local cache.
        try:
            for filename in os.listdir(self._cache_dir):
                if len(results) >= limit:
                    break
                if not filename.endswith(".json"):
                    continue
                lo_key = filename[:-5]
                if query.lower() not in lo_key.lower():
                    continue
                content = self._retrieve_local(lo_key)
                if content is None or lo_key in seen_keys:
                    continue
                results.append({
                    "key": lo_key,
                    "content": content,
                    "source": self._name,
                })
                seen_keys.add(lo_key)
        except Exception:
            pass

        return results[:limit]

    def is_available(self) -> bool:
        """True if MCP is alive *or* the local cache dir is writable.

        Worst case the source stays available in local-fallback mode so the
        orchestrator keeps working.
        """
        if self._connected and self._client.is_alive:
            return True
        try:
            return (
                self._cache_dir.exists()
                and os.access(str(self._cache_dir), os.R_OK | os.W_OK)
            )
        except Exception:
            return False

    def get_source_info(self) -> Dict[str, Any]:
        mcp_up = self._connected and self._client.is_alive
        return {
            "name": self._name,
            "type": "mempalace",
            "available": self.is_available(),
            "mcp_connected": mcp_up,
            "fallback_dir": str(self._cache_dir),
            "python_bin": getattr(self._client, "_python_bin", "unknown"),
            "description": (
                "MemPalace MCP (live)" if mcp_up else "MemPalace (local fallback)"
            ),
            "version": "2.1",
        }

    @property
    def name(self) -> str:
        return self._name

    # --- internal helpers -----------------------------------------------------------

    @staticmethod
    def _to_str(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _from_str(text: str) -> Any:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return text

    @staticmethod
    def _key_to_room(key: str) -> str:
        """Convert a memory key into a MemPalace room slug."""
        sanitized = key.lower().strip()
        sanitized = re.sub(r'[^a-z0-9\-]', '-', sanitized)
        parts = [p for p in sanitized.split("-") if p]
        return "-".join(parts)[:128]

    def _cache_locally(self, key: str, value: Any) -> bool:
        """Write to the local JSON cache (fallback / resilience)."""
        try:
            filepath = self._cache_dir / f"{key}.json"
            with open(filepath, "w") as f:
                json.dump(value, f)
            return True
        except Exception:
            return False

    def _retrieve_local(self, key: str) -> Optional[Any]:
        """Read from the local JSON cache."""
        filepath = self._cache_dir / f"{key}.json"
        if filepath.exists():
            try:
                with open(filepath) as f:
                    return json.load(f)
            except Exception:
                pass
        return None


    # ------------------------------------------------------------------
    # Proactive methods (spec §Triggered behaviour – chorus-wide invocation)
    # ------------------------------------------------------------------

    def proactive_check(
        self, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Search MemPalace for memories relevant to the pending action.

        Uses a simple keyword query built from whatever values appear in *context*.
        Falls back to local cache when MCP is unavailable.
        """
        if not context:
            return {
                "status": "ready",
                "found_memories": 0,
                "source": self._name,
                "mcp_connected": self._connected and self._client.is_alive,
            }

        query = " ".join(str(v) for v in context.values() if v)
        findings: List[Dict[str, Any]] = []

        # Try MCP first.
        if self._connected and self._client.is_alive:
            mp_hits = self._client.search(query=query, limit=5)
            if mp_hits:
                for r in (isinstance(mp_hits, list) and mp_hits or []):
                    content_val = (
                        r.get("content", str(r)) if isinstance(r, dict) else str(r)
                    )
                    findings.append({"key": "mempalace_hit", "content": self._from_str(str(content_val))})

        # Also try local cache.
        cache_hits = []
        for f in self._cache_dir.glob("*.json"):
            if any(word in f.stem.lower() for word in query.lower().split()):
                val = self._retrieve_local(f.stem)
                if val is not None:
                    cache_hits.append({"key": f.stem, "content": val})

        return {
            "status": "ready",
            "found_memories": len(findings) + len(cache_hits),
            "source": self._name,
            "mcp_connected": self._connected and self._client.is_alive,
            "recommendations": findings + cache_hits,
        }

    def proactive_save(
        self, key: str, value: Any, context: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Save a memory after an action completes.

        Always writes to local cache for reliability; also attempts MCP push
        when the live server is available. Returns True as soon as *any*
        persistence path succeeds (graceful degradation per spec).
        """
        ok = self.save(key, value)

        if ok and context:
            action_key = f"proactive_{key}"
            self._cache_locally(action_key, {
                "action": "proactive_save",
                "memory_key": key,
                "context": context,
                "source": self._name,
            })

        return ok
