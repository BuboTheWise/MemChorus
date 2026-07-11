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
import shlex
import shutil
import sys
import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from memchorus.memory_source import MemorySource

logger = logging.getLogger(__name__)

# --- Wing / room routing defaults (§1 + §3 of spec) ----------------------------
_DEFAULT_WING_MAP: Dict[str, str] = {
    "DECISION": "memchorus_decisions",
    "LEARNING": "memchorus_learning",
    "MISTAKE":  "memchorus_learning",      # mistakes group with lessons
    "RESULT":   "memchorus_general",
    "DEFAULT":  "memchorus_general",        # catch-all fallback inside the map
}

_DEFAULT_ROOM_MAP: Dict[str, str] = {
    "DECISION": "decisions",
    "LEARNING": "lessons-learned",
    "MISTAKE":  "corrections",
    "RESULT":   "outcomes",
    "DEFAULT":  "general",
}


def _run_async(coro):
    """Execute an async coroutine in a fresh event loop.

    Uses ``asyncio.run()`` which is the safe, modern way to execute a coroutine
    from synchronous code.  On rare occasions (e.g. inside a test harness that
    already drives its own event loop) calling ``asyncio.run()`` raises
    ``RuntimeError('set_event_loop_policy')``.  We catch that fall‑back to the old
    manual loop pattern while logging a warning so operators know an unusual
    environment is in play.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "set_event_loop" in str(exc):
            # Already inside running event-loop — fall back to manual loop.
            # The coroutine *will* run, but callers should prefer passing back an
            # awaitable instead of mixing sync/async boundaries here.
            logger.warning(
                "_run_async: already-in-loop; falling back to new_event_loop: %s",
                exc,
            )
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        raise  # re-raise unexpected RuntimeErrors (e.g. "no running event loop")


class _McpTransportDetector:
    """Detect MCP transport configuration from Hermes config.yaml.

    Reads ``$HERMES_HOME/config.yaml`` (or ``~/.hermes/config.yaml``) and looks
    for the key path ``mcp_servers.mempalace.command``.  When present the value
    is split with ``shlex.split()`` into a command + args list suitable for
    subprocess launch.

    Returns a dict like::

        {"command": "/path/to/python",
         "args": ["-m", "mempalace.mcp_server"],
         "resolved_from": "config.yaml mcp_servers.mempalace.command"}

    or ``None`` when no override is configured, allowing the caller to fall
    through to the existing discovery chain.
    """

    @staticmethod
    def _find_config() -> Optional[Path]:
        """Locate the Hermes config.yaml file."""
        hermes_home = os.environ.get("HERMES_HOME", None)
        candidates: List[Path] = []

        if hermes_home and hermes_home != "~/.hermes":
            candidates.append(Path(hermes_home) / "config.yaml")

        home_config = Path.home() / ".hermes" / "config.yaml"
        if home_config not in candidates:
            candidates.append(home_config)

        for c in candidates:
            if c.is_file():
                return c
        return None

    @staticmethod
    def detect(config_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
        """Parse config.yaml and return transport override, or None.

        Parameters
        ----------
        config_path :
            Explicit path to the Hermes config file.  When omitted, auto-locates
            via ``_find_config()``.
        """
        target = config_path if config_path is not None else _McpTransportDetector._find_config()

        if target is None:
            logger.debug("_McpTransportDetector: no config.yaml found")
            return None

        try:
            with open(target) as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            logger.warning(
                "_McpTransportDetector: failed to parse %s: %s", target, exc
            )
            return None
        if not isinstance(data, dict):
            logger.warning("_McpTransportDetector: config.yaml is not a mapping")
            return None

        # Navigate mcp_servers -> mempalace -> command
        mcp_servers = data.get("mcp_servers", {})
        if not isinstance(mcp_servers, dict):
            return None

        mempalace_cfg = mcp_servers.get("mempalace", {})
        if not isinstance(mempalace_cfg, dict):
            return None

        command_raw = mempalace_cfg.get("command", None)
        if not command_raw or not isinstance(command_raw, str):
            return None

        try:
            parts = shlex.split(command_raw)
        except ValueError as exc:
            logger.warning(
                "_McpTransportDetector: invalid command string in config.yaml: %s", exc
            )
            return None

        if not parts:
            return None

        # Split into [command, *args]
        resolved = {
            "command": parts[0],
            "args": parts[1:],
            "resolved_from": f"config.yaml mcp_servers.mempalace.command ({target})",
        }

        logger.info(
            "_McpTransportDetector: config override detected -> command=%r, args=%r, source=%s",
            resolved["command"],
            resolved["args"],
            resolved["resolved_from"],
        )

        return resolved


async def _call_tool_async(
    command: str,
    args: list,
    timeout: float,
    name: str,
    arguments: dict,
) -> Any:
    """Async core of _McpClient._call wrapped in proper error handling."""
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.session import ClientSession

    server_params = StdioServerParameters(
        command=command,
        args=args,
    )

    async with stdio_client(server_params) as (r_stream, w_stream):
        async with ClientSession(
            read_stream=r_stream,
            write_stream=w_stream,
            read_timeout_seconds=timedelta(seconds=timeout),
        ) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments)

            # Parse the MCP tool result into JSON dict (or raw text if unparsable).
            texts = []
            try:
                if hasattr(result, "content") and result.content:
                    for block in result.content:
                        if hasattr(block, "type"):
                            # MCP SDK blocks have a 'type' discriminator.
                            if getattr(block, "type", "") == "text":
                                texts.append(str(getattr(block, "text", "")))
            except Exception:
                # Result object is malformed — move on to raw.
                pass
        raw = "\n".join(texts) if texts else ""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {"raw": raw} if raw else {}


class _McpClient:
    """Minimal stdio client for MemPalace.

    Strategy (v2.1): open a **fresh subprocess + session per tool call**.
    This avoids the impossible lifecycle problem of keeping an async ClientSession
    alive inside a synchronous class while its internal reader/writer tasks
    would be dead once they leave their context manager.

    The cost is starting a Python subprocess for each operation, but MemChorus
    save/retrieve/search are not firehose operations -- the overhead is acceptable.

    **Transport resolution chain (v2.2):**
    0. ``config.yaml mcp_servers.mempalace.command`` — user override from Hermes config
       (highest priority, checked by ``_McpTransportDetector``)
    1. ``config.get("python_bin")`` — explicit user override passed to the constructor
    2. ``shutil.which("mempalace-python")`` — dedicated shim on PATH
    3. pipx venv locations (existing)
    4. ``sys.executable`` (existing)
    5. ``python3`` on PATH (existing)
    6. ``/usr/bin/python3`` fallback, lowest priority (existing)
    """

    def __init__(self, timeout: float = 30.0, config: Optional[Dict[str, Any]] = None):
        self.timeout = float(timeout)
        self._connected = False
        self._config = config or {}

        # Step 0: Check Hermes config.yaml for mcp_servers.mempalace.command override
        self._transport_override: Optional[Dict[str, Any]] = _McpTransportDetector.detect()

        if self._transport_override:
            logger.info(
                "MCP transport: using config.yaml override -> %s",
                self._transport_override.get("resolved_from"),
            )

        # Always discover python_bin as a fallback so _python_bin attribute exists.
        self._python_bin = self._discover_python()

    def _get_transport(self) -> tuple[str, list]:
        """Return (command, args) for launching the MCP subprocess.

        Priority 0: config.yaml override from ``_McpTransportDetector``.
        Fallback: self._python_bin + standard module path.
        """
        if self._transport_override:
            return (self._transport_override["command"], list(self._transport_override["args"]))
        return (self._python_bin, ["-m", "mempalace.mcp_server"])

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

        cmd, args = self._get_transport()
        if not Path(cmd).exists():
            logger.warning("MCP transport command does not exist: %s", cmd)
            return False

        is_override = bool(self._transport_override)
        server_params = StdioServerParameters(
            command=cmd,
            args=args,
        )

        if is_override:
            logger.info(
                "connect: using config.yaml transport override (command=%r, args=%r)",
                cmd, args,
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
        """Open subprocess + session, run one tool call, clean up.

        Handles broken-pipe / OSError via graceful degradation: if a previously‑alive
        connection dies mid‑flight we reset ``_connected`` so the next call triggers a
        fresh connect attempt and avoids repeatedly hammering a dead child process.
        """
        cmd, args = self._get_transport()

        if not Path(cmd).exists():
            return None

        try:
            result = _run_async(
                asyncio.wait_for(
                    _call_tool_async(
                        cmd, args, self.timeout * 2, name, arguments
                    ),
                    timeout=self.timeout * 2,
                )
            )
            # Success path — clear dead-connection flag if previously set.
            if not self._connected:
                logger.info("MemPalace MCP connection re-established after failure.")
                try:
                    self._connected = self.connect()
                except Exception:
                    self._connected = False
            return result
        except BrokenPipeError as exc:
            logger.error("_call: broken pipe to MCP server: %s", exc)
            self._connected = False  # force re-connect on next call
            return None
        except asyncio.TimeoutError as exc:
            logger.error("_call: MCP call timed out: %s", exc)
            self._connected = False  # reset; may recover on next attempt
            return None
        except OSError as exc:
            logger.error(
                "_call: OS error communicating with MCP server (%s): %s",
                type(exc).__name__,
                exc,
            )
            self._connected = False
            return None
        except Exception as exc:
            # Catch-all: log anything else and degrade gracefully.
            logger.error(
                "_call: unexpected error during MCP call: %s (%s)",
                type(exc).__name__,
                exc,
            )
            self._connected = False
            return None

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call a MemPalace MCP tool.  Returns parsed dict or None."""
        if not self._connected:
            return None
        return self._call(name, arguments)

    # -- convenience wrappers -------------------------------------------------------

    @staticmethod
    def _unwrap_responses(data: Any) -> Any:
        """Unwrap MCP search envelope nesting.

        MemPalace server returns results wrapped in envelope dicts with a
        ``"results"`` key:  ``{"query": ..., "filters": ..., "results": [...]}``

        Also handles a list of such envelopes by extracting inner hit dicts.
        Passes through data that is already flat (no envelope detected).
        """
        if isinstance(data, dict) and "results" in data:
            inner = data["results"]
            return inner if isinstance(inner, list) else [inner]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "results" in item:
                    inner = item["results"]
                    if isinstance(inner, list):
                        return inner
        return data

    def search(self, query: str, limit: int = 5, *, wing: Optional[str] = None, room: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """mempalace_search -\u003e list of dicts."""
        args: Dict[str, Any] = {"query": query, "limit": limit}
        if wing is not None:
            args["wing"] = wing
        if room is not None:
            args["room"] = room
        result = self.call_tool("mempalace_search", args)
        if result is None:
            return None

        # Unwrap "result" / "results" envelope layers before anything else.
        if isinstance(result, dict):
            for candidate_key in ("result", "results"):
                if candidate_key in result:
                    result = result[candidate_key]
                    break

        data = self._unwrap_responses(result)

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
    ``mempalace_routing``  A dict with ``wing_map`` and/or ``room_map` sub-dicts
                          (§1 + §3 of spec).  Omitted → built-in defaults.
                           Empty dict → also built-in defaults (AC-R3.1).
    """

    # Built-in routing tables — used when config provides no override or is empty.
    _WING_MAP_DEFAULT = _DEFAULT_WING_MAP
    _ROOM_MAP_DEFAULT = _DEFAULT_ROOM_MAP

    def __init__(
        self,
        name: str = "mempalace",
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(name, config)
        self._name = name
        self.config = config or {}

        # --- Routing configuration (§1 + §3) ---------------------------------
        routing_cfg = self.config.get("mempalace_routing", None)
        if isinstance(routing_cfg, dict) and routing_cfg:
            self._routing_config = routing_cfg
        else:
            self._routing_config = {}

        wing_map_raw = self._routing_config.get("wing_map", None)
        if isinstance(wing_map_raw, dict) and wing_map_raw:
            # Build a case-insensitive lookup table: uppercase key → value.
            self._wing_map: Dict[str, str] = {
                k.upper(): v for k, v in wing_map_raw.items()
            }
        else:
            # AC-R1.2 / AC-R3.1: use built-in defaults when missing or empty.
            self._wing_map = dict(self._WING_MAP_DEFAULT)

        room_map_raw = self._routing_config.get("room_map", None)
        if isinstance(room_map_raw, dict) and room_map_raw:
            self._room_map: Dict[str, str] = {
                k.upper(): v for k, v in room_map_raw.items()
            }
        else:
            self._room_map = dict(self._ROOM_MAP_DEFAULT)

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

        # Extract category from payload for wing routing (§1).
        # AutoStorageEngine attaches ``category`` / ``significance`` keys in the
        # value dict (lines 256-260 of auto_storage_engine.py).
        category = None
        if isinstance(value, dict):
            category = value.get("category", None) or value.get("significance", None)

        wing = self._resolve_wing(category)

        if self._connected and self._client.is_alive:
            # §2 Room selection by significance category (AC-R2.1-2.4)
            cat_room = self._categorize_room(value, room_map=self._room_map)
            # AC-R2.3: raw string keys without category metadata fall back to legacy hashing
            if cat_room == 'general':
                room = self._key_to_room(key)
            else:
                room = cat_room
            ok = self._client.add_drawer(
                wing=wing, room=room, content=content
            )
            if ok:
                # Mirror locally for resilience.
                self._cache_locally(key, value)
                return True

        # MCP unavailable or call failed -> local cache only.
        return bool(self._cache_locally(key, value))

    def retrieve(self, key: str) -> Optional[Any]:
        """Look up the memory.  Tries MCP first; falls back to local cache.

        §6 AC-R6.1: Uses resolved wing/room from category info when available.
        §6 AC-R6.2: Broadens to wing-level search when category unavailable.
        """
        if self._connected and self._client.is_alive:
            # First try: check local cache for category metadata (§6)
            filepath = self._cache_dir / f"{key}.json"
            cached_value = None
            if filepath.exists():
                try:
                    with open(filepath) as f:
                        cached_value = json.load(f)
                except Exception:
                    pass

            # Derive wing and room from cached category info
            wing = self._resolve_wing_from_payload(cached_value)
            cat_room = self._categorize_room(
                cached_value, room_map=self._room_map
            ) if cached_value else None

            # Primary search: targeted wing + room when we have category
            found_results = None
            if cat_room:
                found_results = self._client.search(
                    query="", wing=wing, room=cat_room, limit=1
                )
            # AC-R6.2: Broaden to wing-level when room search fails or no category
            if not found_results:
                found_results = self._client.search(
                    query=key[:32], wing=wing, limit=5
                )

            results = found_results
            if results:
                for r in results:
                    # MCP search returns hit dicts with a 'text' field.
                    r_content = (
                        r.get("text", "") or r.get("content", "")
                        if isinstance(r, dict)
                        else str(r)
                    )
                    if r_content:
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

    def search(self, query: str, limit: int = 10, *, wing: Optional[str] = None, room: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search across MCP + local cache, deduplicating by key.

        §6 AC-R6.3: Optional wing/room filters for targeted recall.
        When neither is specified, searches across all wings (full recall).
        """
        results: List[Dict[str, Any]] = []
        seen_keys: set = set()

        if self._connected and self._client.is_alive:
            mp_results = self._client.search(
                query=query, limit=limit, wing=wing, room=room
            )
            if mp_results:
                for r in mp_results:
                    wing = r.get("wing", "unknown") if isinstance(r, dict) else None
                    room = r.get("room", "unknown") if isinstance(r, dict) else None
                    comp_key = f"{wing}/{room}" if wing and room else query

                    content_val = (
                        r.get("text", "") or r.get("content", str(r))
                        if isinstance(r, dict)
                        else str(r)
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
        """Convert a memory key into a MemPalace room slug (legacy path).

        Used for backward compat when category metadata is unavailable.
        """
        sanitized = key.lower().strip()
        sanitized = re.sub(r'[^a-z0-9]', '-', sanitized)
        parts = [p for p in sanitized.split("-") if p]
        return "-".join(parts)[:128]

    @staticmethod
    def _categorize_room(memory: Any, *, room_map: Optional[Dict[str, str]] = None) -> str:
        """Derive a semantic room slug from the memory payload category (§2).

        Inspects ``category`` / ``significance`` metadata on the value.
        Falls back to ``general`` when neither field is present or unknown.

        AC-R2.1: Payloads with a category/significance dict key use that.
        AC-R2.2: Room slugs are deterministic per category.
        AC-R2.3: No metadata → fallback to 'general'.
        AC-R2.4: Lowercase hyphen-separated slugs.

        Returns the room slug string.
        """
        lookup = room_map or dict(_DEFAULT_ROOM_MAP)

        if not isinstance(memory, dict):
            return lookup.get('DEFAULT', 'general')

        # Try multiple metadata paths where the category might live
        raw_cat = memory.get("category") or memory.get("significance")

        # AutoStorageEngine wraps significance in a nested dict
        if not raw_cat:
            meta = memory.get("metadata", {}) or {}
            if isinstance(meta, dict):
                sig = meta.get("significance", {})
                if isinstance(sig, dict):
                    raw_cat = sig.get("category")

        # If significance is a string, use it directly
        if not raw_cat and isinstance(memory.get("significance"), str):
            raw_cat = memory["significance"]

        if raw_cat:
            upper = str(raw_cat).upper()
            slug = lookup.get(upper)
            if slug:
                return slug

        # Unknown category or no category → DEFAULT / general
        return lookup.get('DEFAULT', 'general')

    @staticmethod
    def _resolve_wing_from_payload(payload: Any) -> str:
        """Extract wing from cached payload metadata (section 6 AC-R6.1).

        Used by retrieve() to determine which wing a memory was saved to
        when category info exists in the local cache copy.
        Falls back to default wing if no category metadata found.
        """
        if not isinstance(payload, dict):
            return _DEFAULT_WING_MAP.get('DEFAULT', 'memchorus')

        # Same extraction paths as save() for consistency
        cat = payload.get("category") or payload.get("significance")
        if not cat:
            meta = payload.get("metadata", {}) or {}
            if isinstance(meta, dict):
                sig = meta.get("significance", {})
                if isinstance(sig, dict):
                    cat = sig.get("category")
        if not cat and isinstance(payload.get("significance"), str):
            cat = payload["significance"]

        if cat:
            upper = str(cat).upper()
            wing = _DEFAULT_WING_MAP.get(upper)
            if wing:
                return wing

        return _DEFAULT_WING_MAP.get('DEFAULT', 'memchorus')

    def _resolve_wing(self, category: Optional[str] = None) -> str:
        """Resolve the target MemPalace wing for a given significance category.

        Look up *category* (case-insensitive) in ``self._wing_map``.  When the
        category is not found — or it was ``None``/empty — fall through to the
        map's ``DEFAULT`` entry, and if that doesn't exist either, return the
        original hard-coded fallback ``\"memchorus"`` for backward compat (AC-R1.2).

        Parameters
        ----------
        category :
            Significance category string (e.g. ``"DECISION"``, ``"LEARNING"``)
            or anything falsy to trigger the default path.

        Returns
        -------
        str  — a wing name such as ``"memchorus_decisions"`` or
               ``"memchorus_general"``.
        """
        if not category:
            return self._wing_map.get(
                "DEFAULT", "memchorus"  # AC-R1.2 final safety fallback
            )

        hit = self._wing_map.get(category.upper(), None)
        if not hit:
            # AC-R3.3: unknown key → default mapping, not crash.
            return self._wing_map.get("DEFAULT", "memchorus")
        return hit

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

    def delete(self, key: str) -> bool:
        """Remove a memory identified by *key*.

        Tries MCP ``mempalace_delete_drawer`` first (requires knowing the drawer_id).
        As a fallback strategy we remove the local cache copy.  Returns ``True`` when
        at least one persistence path reported success, ``False`` otherwise.
        """
        deleted = False

        # Attempt MCP deletion — search for the drawer that matches this key
        if self._connected and self._client.is_alive:
            hits = self._client.search(query=key, limit=5)
            if isinstance(hits, list):
                for hit in hits:
                    if not isinstance(hit, dict):
                        continue
                    # Check if this hit matches our key
                    hit_key = (hit.get("key", "") or "").lower()
                    if key.lower() == hit_key:
                        drawer_id = hit.get("drawer_id") or hit.get("id")
                        if drawer_id:
                            result = self._client.call_tool(
                                "mempalace_delete_drawer",
                                {"drawer_id": str(drawer_id)},
                            )
                            if result is not None:
                                deleted = True
                                break

        # Always remove the local cache copy regardless of MCP outcome
        local_ok = False
        try:
            filepath = self._cache_dir / f"{key}.json"
            if filepath.exists():
                filepath.unlink()
                local_ok = True
        except Exception:
            pass

        return deleted or local_ok
