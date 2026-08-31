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
import time
import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from memchorus.memory_source import MemorySource
from memchorus.hermes_home import hermes_home

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

    Also catches ``BaseExceptionGroup`` (raised by anyio TaskGroups when the MCP
    subprocess dies mid-operation) and returns ``None`` gracefully instead of
    crashing the event loop.
    """
    try:
        return asyncio.run(coro)
    except BaseExceptionGroup as exc:
        # anyio TaskGroup teardown propagates ExceptionGroup / BaseExceptionGroup
        # (which inherit from BaseException, NOT Exception).
        # Return None so callers treat this as a normal failure rather than
        # crashing the entire event loop.
        logger.warning(
            "_run_async: caught %s (%d sub-exc(s)): %s — returning None",
            type(exc).__name__, len(exc.exceptions), exc,
        )
        return None
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


def _chroma_is_empty(path: Path) -> bool:
    """Return True if the given chroma.sqlite3 has no useful data.

    "No useful data" means:
    - file does not exist, or
    - file is 0 bytes (never-opened shell), or
    - file is a valid sqlite whose ``embeddings`` table has 0 rows (the
      "empty shell" state from the Aug 20 bug), or
    - file exists but is not a readable sqlite (treat as empty to avoid
      surprising rewrites on corrupt/incomplete files).

    A valid sqlite with ≥1 embedding row is considered "has data."
    """
    if not path.exists():
        return True
    try:
        if path.stat().st_size == 0:
            return True
    except OSError:
        return True
    try:
        import sqlite3
        con = sqlite3.connect(str(path))
        try:
            try:
                n = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
                return n == 0
            except sqlite3.Error:
                # No ``embeddings`` table (or not a valid sqlite header) —
                # treat as an empty shell for this purpose.
                return True
        finally:
            con.close()
    except Exception:
        # Could not open / connect — conservative "empty" so we don't
        # rewrite based on an unreadable file.
        return True
    return False


def _normalize_palace_args(args: list[str]) -> list[str]:
    """Re-point ``--palace <path>`` at a ``palace/`` leaf dir that holds
    the real data when the configured path is one level too shallow.

    MemPalace's reader opens ``os.path.join(palace_path, "chroma.sqlite3")``
    verbatim (``mempalace/mcp_server.py``) — it never descends into a
    sub-directory.  When a profile's ``--palace`` accidentally points at
    the *parent* dir (``.mempalace``) while the real data is in
    ``.mempalace/palace/chroma.sqlite3``, the reader opens an empty shell
    at the parent level and the corpus is invisible (status/search/KG all
    report 0).

    This guard detects that split and re-points the path at the leaf.

    Rules (idempotent, single-level descent, no path invention):

    - No ``--palace`` flag → return args unchanged.
    - ``--palace <P>`` and ``<P>/palace/chroma.sqlite3`` holds data while
      ``<P>/chroma.sqlite3`` is empty/absent → rewrite to ``<P>/palace``.
    - ``<P>/chroma.sqlite3`` already holds data → no-op (correct leaf,
      e.g. mempalace's default ``~/.mempalace/palace`` convention).
    - Neither the parent nor the leaf holds a chroma file (fresh install)
      → no-op (do not invent a path the reader doesn't yet have).
    - Also handles ``--palace=<P>`` (equals form).

    Returns a new list; the input list is not mutated.
    """
    result = list(args)  # copy, never mutate caller's list

    def _rewrite(idx: int, value: str) -> None:
        """Rewrite position ``idx`` in result to ``value`` (with log)."""
        old = result[idx]
        result[idx] = value
        logger.info(
            "_normalize_palace_args: re-pointed --palace %r -> %r", old, value
        )

    for i, tok in enumerate(result):
        # Space form:  --palace <PATH>
        if tok == "--palace" and i + 1 < len(result):
            parent = Path(result[i + 1])
            leaf = parent / "palace"
            leaf_chroma = leaf / "chroma.sqlite3"
            parent_chroma = parent / "chroma.sqlite3"
            # Rewrite only when the leaf holds a real chroma but the
            # parent is an empty shell (or has none at all).
            if leaf_chroma.exists() and _chroma_is_empty(parent_chroma):
                _rewrite(i + 1, str(leaf))
            break

        # Equals form: --palace=<PATH>
        if tok.startswith("--palace="):
            parent = Path(tok[len("--palace="):])
            leaf = parent / "palace"
            leaf_chroma = leaf / "chroma.sqlite3"
            parent_chroma = parent / "chroma.sqlite3"
            if leaf_chroma.exists() and _chroma_is_empty(parent_chroma):
                _rewrite(i, f"--palace={leaf}")
            break

    return result


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

    **Caching (v2.3):** Results are cached at module level for 60 seconds to
    avoid redundant config.yaml parsing, PATH lookups, and warning spam when
    multiple ``MemoryOrchestrator`` instances are created in the same process.
    The cache can be cleared manually via ``_McpTransportDetector.clear_cache()``
    if the user changes config between runs.
    """

    # Module-level cache: (result_dict_or_None, timestamp) with 60s TTL.
    _DETECTION_CACHE: tuple[Optional[Dict[str, Any]], float] = (None, 0.0)
    _CACHE_TTL: float = 60.0
    # Tracks the resolved target path that the cached result was computed for,
    # so repeated calls with a *different* config_path still trigger a fresh scan.
    _CACHED_TARGET: Optional[str] = None
    # Track whether the "no transport found" warning has already been emitted
    # so we don't spam the user across multiple orchestrator instances.
    _WARNING_EMITTED: bool = False

    @staticmethod
    def _find_config() -> Optional[Path]:
        """Locate the Hermes config.yaml file."""
        env_home = os.environ.get("HERMES_HOME", None)
        candidates: List[Path] = []

        if env_home and env_home != "~/.hermes":
            candidates.append(Path(env_home) / "config.yaml")

        home_config = hermes_home() / "config.yaml"
        if home_config not in candidates:
            candidates.append(home_config)

        for c in candidates:
            if c.is_file():
                return c
        return None

    @staticmethod
    def _fallback_to_path() -> Optional[Dict[str, Any]]:
        """Fall back to shutil.which('mempalace-mcp') on PATH when config parsing fails."""
        binary = None
        try:
            from shutil import which
            binary = which("mempalace-mcp")
        except Exception:
            pass

        if binary:
            return {
                "command": binary,
                "args": [],
                "resolved_from": f"PATH (shutil.which) -> {binary}",
            }
        return None

    @staticmethod
    def _log_config_guidance() -> None:
        """Log actionable guidance when no MCP transport is found."""
        if _McpTransportDetector._WARNING_EMITTED:
            return  # suppress repeated warnings across orchestrator instances
        _McpTransportDetector._WARNING_EMITTED = True
        yaml_snippet = (
            "\n  mcp_servers:\n"
            "    mempalace:\n"
            "      command: /path/to/mempalace-mcp\n"
            "\nTo enable it, add this to your config.yaml.\n"
            "Alternatively, install the MCP transport via pip:\n"
            "  pip install memchorus[mcp]"
        )
        logger.warning(
            "_McpTransportDetector: no MCP transport found. Configure MemPalace in config.yaml:\n%s",
            yaml_snippet,
        )

    @staticmethod
    def clear_cache() -> None:
        """Clear the detection cache and warning flag.

        Call this if you change Hermes config.yaml between orchestrator runs
        or during tests.  Resets both the result cache and the one-shot
        warning guard so the next ``detect()`` re-runs the full scan.
        """
        _McpTransportDetector._DETECTION_CACHE = (None, 0.0)
        _McpTransportDetector._CACHED_TARGET = None
        _McpTransportDetector._WARNING_EMITTED = False

    @staticmethod
    def detect(config_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
        """Parse config.yaml and return transport override, or None.

        Falls back to ``shutil.which('mempalace-mcp')`` when config is missing,
        invalid, or lacks the expected key path. Logs actionable guidance if
        nothing can be found at all.

        **Caching:** On the first call (or after TTL expiry) this runs the full
        detection chain.  The result and a timestamp are stored in
        ``_DETECTION_CACHE`` for ``_CACHE_TTL`` seconds (default 60 s).
        Subsequent calls within the window return the cached value without
        re-parsing config.yaml or spawning subprocesses.

        Parameters
        ----------
        config_path :
            Explicit path to the Hermes config file.  When omitted, auto-locates
            via ``_find_config()``.
        """
        now = time.monotonic()

        # Resolve the target path BEFORE checking the cache so we can compare
        # it with the previously cached target.  This way repeated calls with the
        # SAME config get a fast cached return, but calls that pass a different
        # config_path always trigger a fresh scan (fixes CI ordering issues).
        target = config_path if config_path is not None else _McpTransportDetector._find_config()
        target_key = str(target) if target is not None else "<auto-detect>"

        cached_result, cached_ts = _McpTransportDetector._DETECTION_CACHE
        if (now - cached_ts) < _McpTransportDetector._CACHE_TTL:
            if _McpTransportDetector._CACHED_TARGET == target_key:
                return cached_result

        # --- cache miss or expired — run full detection -------------------

        if target is None:
            logger.debug("_McpTransportDetector: no config.yaml found")

        # Try to parse config — on any failure, fall through to PATH fallback
        goto_fallback = target is None  # type: bool
        data = None                     # typed so Pyright knows it's never unbound
        parts: list[str] = []

        if not goto_fallback:  # target is not None here
            try:
                with open(target) as f:   # ok – target proven Path above
                    data = yaml.safe_load(f)
            except Exception as exc:
                logger.warning(
                    "_McpTransportDetector: failed to parse %s: %s", target, exc
                )
                goto_fallback = True

        if not goto_fallback:
            if not isinstance(data, dict):
                logger.warning("_McpTransportDetector: config.yaml is not a mapping")
                goto_fallback = True

        # Navigate mcp_servers -> mempalace -> command (only when data is valid).
        mcp_servers: dict = {}
        mempalace_cfg: dict = {}
        command_raw: str | None = None

        if not goto_fallback:
            mcp_servers = data.get("mcp_servers", {})  # type: ignore[union-attr]
            if not isinstance(mcp_servers, dict):
                goto_fallback = True

        if not goto_fallback:
            mempalace_cfg = mcp_servers.get("mempalace", {})
            if not isinstance(mempalace_cfg, dict):
                goto_fallback = True

        # Support two config shapes:
        #  Shape A (legacy): command is a single shell string to split via shlex.
        #  Shape B (Hermes native): command is an executable path, args is a list.
        cfg_args: Optional[List[str]] = None

        if not goto_fallback:
            cfg_args = mempalace_cfg.get("args", None)
            if cfg_args is not None and not isinstance(cfg_args, (list)):
                # args key exists but isn't a list — treat as invalid shape
                logger.warning(
                    "_McpTransportDetector: config 'args' is not a list (%s) — skipping override",
                    type(cfg_args).__name__,
                )
                cfg_args = None

            command_raw = mempalace_cfg.get("command", None)
            if not command_raw or not isinstance(command_raw, str):
                goto_fallback = True

        parts: list[str] = []
        if not goto_fallback and cfg_args is not None:
            # Shape B: command is a path, args is already split.
            assert isinstance(command_raw, str)  # narrowed above
            parts.append(os.path.expanduser(command_raw))
            parts.extend(cfg_args)
        elif not goto_fallback:
            # Shape A (legacy): split the command string into argv parts.
            assert isinstance(command_raw, str)  # narrowed above
            try:
                if os.name != "nt":
                    # POSIX: shlex handles quoting and shell-style escaping.
                    parts = shlex.split(command_raw)
                else:
                    # Windows paths use backslashes, which POSIX shlex would
                    # treat as escape characters and swallow (GH-146 Windows CI).
                    # Split on unquoted whitespace and unquote each token:
                    # "C:\x\python.exe" -m foo -> ['C:\\x\\python.exe', '-m', 'foo']
                    parts = []
                    for token in command_raw.strip().split():
                        if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
                            token = token[1:-1]
                        parts.append(token)
            except ValueError as exc:
                logger.warning("_McpTransportDetector: invalid command string in config.yaml: %s", exc)
                goto_fallback = True

        if not goto_fallback and not parts:
            goto_fallback = True

        # Expand tilde paths for Shape A (already expanded for Shape B above).
        if not goto_fallback and cfg_args is None:
            parts[0] = os.path.expanduser(parts[0])

        # Validate the resolved command actually exists on disk before trusting
        # the config override. If it doesn't, fall through to PATH discovery so
        # we don't return a dead path that crashes subprocess launch.
        if not goto_fallback:
            cmd_path = Path(parts[0])
            if not (cmd_path.exists() and os.access(cmd_path, os.X_OK)):
                logger.warning(
                    "_McpTransportDetector: config override points to non-existent or "
                    "non-executable file: %s — falling back to PATH discovery",
                    parts[0],
                )
                goto_fallback = True

        # If we found a valid override, normalize the --palace path so the
        # reader sees the same layout the writer writes.  See
        # _normalize_palace_args for the rules (single-level descent to a
        # leaf dir that actually holds chroma.sqlite3; no path invention).
        if not goto_fallback and len(parts) > 1:
            parts[1:] = _normalize_palace_args(parts[1:])

        # If we found a valid override, return immediately.
        if not goto_fallback:
            resolved = {
                "command": parts[0],
                "args": parts[1:],
                "resolved_from": f"config.yaml mcp_servers.mempalace ({target})",
            }

            logger.info(
                "_McpTransportDetector: config override detected -> command=%r, args=%r, source=%s",
                resolved["command"],
                resolved["args"],
                resolved["resolved_from"],
            )

            _McpTransportDetector._DETECTION_CACHE = (resolved, now)
            _McpTransportDetector._CACHED_TARGET = target_key
            return resolved

        # Fallback: try PATH discovery
        fallback = _McpTransportDetector._fallback_to_path()
        if fallback:
            logger.info("_McpTransportDetector: using PATH fallback -> %s", fallback["command"])
            _McpTransportDetector._DETECTION_CACHE = (fallback, now)
            _McpTransportDetector._CACHED_TARGET = target_key
            return fallback

        # Nothing found — show actionable guidance before giving up
        _McpTransportDetector._log_config_guidance()
        _McpTransportDetector._DETECTION_CACHE = (None, now)
        _McpTransportDetector._CACHED_TARGET = target_key
        return None


async def _call_tool_async(
    command: str,
    args: list,
    timeout: float,
    name: str,
    arguments: dict,
) -> Any:
    """Async core of _McpClient._call wrapped in proper error handling.

    The entire async context-manager body (including teardown) is caught so
    that anyio ``BaseExceptionGroup`` — raised when internal TaskGroups
    cancel on timeout or process exit — cannot escape the sync wrapper
    and crash the orchestrator session.
    """
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.session import ClientSession

    server_params = StdioServerParameters(
        command=command,
        args=args,
    )

    texts: list[str] = []
    try:
        async with stdio_client(server_params) as (r_stream, w_stream):
            async with ClientSession(
                read_stream=r_stream,
                write_stream=w_stream,
                read_timeout_seconds=timedelta(seconds=timeout),
            ) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=arguments)

                # Parse the MCP tool result into JSON dict (or raw text if unparsable).
                try:
                    if hasattr(result, "content") and result.content:
                        for block in result.content:
                            if hasattr(block, "type"):
                                if getattr(block, "type", "") == "text":
                                    texts.append(str(getattr(block, "text", "")))
                except Exception:
                    pass
    # Catch BaseException (excluding KeyboardInterrupt/SysExit) to handle
    # anyio's BaseExceptionGroup which does NOT inherit from Exception.
    except BaseExceptionGroup as exc:
        logger.warning(
            "_call_tool_async: caught BaseExceptionGroup (%d sub-exc(s)): %s",
            len(exc.exceptions),
            ", ".join(type(e).__name__ for e in exc.exceptions),
        )
        return {}

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

        # Persistent session (avoids per-call subprocess death that crashes ChromaDB)
        self._persistent_session = None

        # Step 0: Check Hermes config.yaml for mcp_servers.mempalace.command override
        self._transport_override: Optional[Dict[str, Any]] = _McpTransportDetector.detect()

        if self._transport_override:
            logger.info(
                "MCP transport: using config.yaml override -> %s",
                self._transport_override.get("resolved_from"),
            )

        # Always discover python_bin as a fallback so _python_bin attribute exists.
        self._python_bin = self._discover_python()

    # Patterns in MemPalace stderr that are informational noise, not errors.
    _NOISE_PATTERNS = [
        "HNSW mtime gap",
        "MemPalace MCP Server starting",
        "Embedding function initialized",
        "stdin EOF -- client disconnected",
    ]

    @classmethod
    def _filter_command(cls, python: str, child_cmd: Optional[list] = None) -> tuple[str, list]:
        """Return a command that runs MemPalace MCP server via a thin stderr filter.

        Spawns a tiny Python -c wrapper that starts mempalace.mcp_server and
        filters out repetitive informational noise from stderr while letting
        real errors / warnings pass through untouched.

        If ``child_cmd`` is None, defaults to [python, "-m", "mempalace.mcp_server"].
        Pass a custom list when a config override (Shape B) specifies different args/env.
        """
        effective_cmd = child_cmd if child_cmd else [python, "-m", "mempalace.mcp_server"]
        # Build the filter script as plain string to avoid f-string escaping issues.
        filtered_patterns = [repr(p) for p in cls._NOISE_PATTERNS]
        patterns_str = ", ".join(filtered_patterns)
        script_lines = [
            'import subprocess, sys;',
            "p=subprocess.Popen(" + repr(effective_cmd) +
            ", stderr=subprocess.PIPE);",
            "for line in p.stderr:",
            "  s=line.decode('utf-8','replace').strip();",
            f"  if not any(pat in s for pat in [{patterns_str}]):",
            "    print(s, file=sys.stderr);",
            "rc=p.wait(); sys.exit(rc)",
        ]
        wrapper = "\n".join(script_lines)
        return (python, ["-c", wrapper])

    def _get_transport(self) -> tuple[str, list]:
        """Return (command, args) for launching the MCP subprocess.

        Priority 0: config.yaml override from ``_McpTransportDetector``.
        Fallback: filtered python command via :meth:`_filter_command` that suppresses
        informational noise while letting real errors through.

        IMPORTANT: All paths MUST go through the stderr filter wrapper, otherwise
        "MemPalace MCP Server starting" and "stdin EOF -- client disconnected" lines
        flood the user's terminal (issue discovered 2026-08-21).
        """
        if self._transport_override:
            cmd = self._transport_override["command"]
            args = list(self._transport_override["args"])
            effective_child = [cmd] + args
            return self._filter_command(self._python_bin, child_cmd=effective_child)
        return self._filter_command(self._python_bin)

    def _discover_python(self) -> str:
        """Discover a Python interpreter for the MCP subprocess.

        Discovery chain (highest to lowest priority):
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
        """Start server subprocess via persistent session (avoids per-call death).

        Returns True when a live, initialized persistent session is ready for tool calls.

        Falls back to the legacy one-shot probe model if persistent mode fails.
        """
        cmd, args = self._get_transport()
        if not Path(cmd).exists():
            logger.warning("MCP transport command does not exist: %s", cmd)
            return False

        is_override = bool(self._transport_override)
        if is_override:
            logger.info(
                "connect: using config.yaml override (command=%r, args=%r)",
                cmd, args,
            )

        # ---- Persistent session mode: one subprocess stays alive across calls ----
        try:
            from memchorus.mempalace_persistent_session import PersistentMcpSession
            self._persistent_session = PersistentMcpSession(
                command=cmd, args=args, timeout=self.timeout
            )
            started = self._persistent_session.start()
            if started:
                logger.info("connect: persistent MCP session initialized (timeout=%ss)",
                            self.timeout)
                self._connected = True
                return True
        except Exception as exc:
            logger.warning(
                "connect: persistent session failed (%s:%s) falling back to per-call model",
                type(exc).__name__, exc,
            )

        # ---- Legacy one-shot probe + per-call subprocess fallback ----
        # Skip entirely during test runs — stdio probes are flaky under CI infra where
        # the MemPalace binary is missing and just blocks on subprocess I/O forever.
        if "PYTEST_CURRENT_TEST" in os.environ:
            self._connected = False
            return False

        try:
            from mcp.client.stdio import StdioServerParameters, stdio_client
            from mcp.client.session import ClientSession
        except ImportError as exc:
            logger.warning(
                "connect: MCP SDK not installed (missing %s) — legacy probe skipped", exc
            )
            self._connected = False
            return False
        server_params = StdioServerParameters(command=cmd, args=args)

        async def _do_init():
            async with stdio_client(server_params) as (r_stream, w_stream):
                async with ClientSession(
                    read_stream=r_stream,
                    write_stream=w_stream,
                    read_timeout_seconds=timedelta(seconds=self.timeout),
                ) as session:
                    await session.initialize()
            return True

        try:
            result = _run_async(asyncio.wait_for(_do_init(), timeout=self.timeout))
            if result is not True:
                self._connected = False
                return False
            logger.info("connect: legacy per-call probe succeeded (persistent unavailable)")
            self._connected = True
            return True
        except BaseExceptionGroup as exc:
            logger.warning(
                "connect: MCP init failed with BaseExceptionGroup (%d sub-exc(s)): %s",
                len(exc.exceptions),
                ", ".join(type(e).__name__ for e in exc.exceptions),
            )
            self._connected = False
            return False
        except Exception as exc:
            logger.warning("connect: legacy probe failed with %s: %s",
                           type(exc).__name__, exc)
            self._connected = False
            return False

    # -- tool calling ---------------------------------------------------------------

    def _call(self, name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Route through persistent session or fall back to per-call subprocess.

        When a live persistent session exists (its ``alive`` flag is True), the
        call goes through that session so ChromaDB gets a stable connection and
        can flush its WAL between operations.  If the persistent session dies
        mid-flight, we tear it down and fall back to the legacy one-shot model
        so the operation still completes.
        """
        # ---- Persistent session path ----
        if self._persistent_session is not None and self._persistent_session.alive:
            result = self._persistent_session.call_tool(name, arguments)
            if result is not None:
                return result
            logger.warning("_call: persistent session died mid-flight — tearing down for reconnect")
            try:
                self._persistent_session.stop()
            except Exception as exc:
                logger.warning("_call: stop persistent session failed: %s", exc)
            finally:
                self._persistent_session = None
                self._connected = False

        # ---- Legacy per-call subprocess fallback ----
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
        except BaseExceptionGroup as exc:
            # anyio TaskGroup raises this when internal reader/flusher tasks
            # are cancelled or crash (does NOT inherit from Exception).
            logger.warning(
                "_call: MCP call raised BaseExceptionGroup (%d sub-exc(s)): %s",
                len(exc.exceptions),
                ", ".join(type(e).__name__ for e in exc.exceptions),
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

    # Textual rejection markers used when an MCP response carries no
    # structured ``success`` field. The word ``"wing"`` itself is deliberately
    # NOT in this list — a real wing name can contain it — but the rejection
    # phrases *"unknown wing" / "does not exist"* are. (#136)
    _DRAWER_ERR_WORDS = (
        "error",
        "failed",
        "failure",
        "not found",
        "no such",
        "does not exist",
        "unknown wing",
        "unknown room",
        "empty",
        "invalid",
        "missing",
        "rejected",
    )

    def _drawer_call_succeeded(self, result: Any) -> bool:
        """Interpret a ``mempalace_add_drawer`` response as success/failure.

        Prefers the structured ``success`` flag the MemPalace MCP server emits
        (``{"success": True, ...}`` / ``{"success": False, "error": ...}``) and
        only falls back to keyword scanning when that field is absent. (#136)
        """
        if isinstance(result, dict):
            if "success" in result:
                return bool(result.get("success"))
            text = str(
                result.get("result", result.get("error", ""))
                if result.get("result") is not None or result.get("error") is not None
                else result
            ).lower()
            return not any(word in text for word in self._DRAWER_ERR_WORDS)

        text = str(result).lower()
        return not any(word in text for word in self._DRAWER_ERR_WORDS)

    def add_drawer(self, wing: str, room: str, content: str) -> bool:
        """mempalace_add_drawer -> True only when the server accepted the write."""
        result = self.call_tool(
            "mempalace_add_drawer",
            {"wing": wing, "room": room, "content": content},
        )
        if result is None:
            return False

        return self._drawer_call_succeeded(result)

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

    def close(self):
        """Shut down the persistent session if one exists."""
        if self._persistent_session is not None:
            try:
                self._persistent_session.stop()
            except Exception as exc:
                logger.warning("_McpClient.close: stop failed: %s", exc)
            finally:
                self._persistent_session = None
                self._connected = False

    @property
    def is_alive(self) -> bool:
        """Return True when either persistent session or legacy probe is live."""
        if self._persistent_session is not None:
            return self._persistent_session.alive
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

    # (#139) Recall-score floor. MemPalace reports ``similarity`` as a
    # [0, 1]-scale figure (cosine distance mapped to similarity, higher =
    # better) — so a *lower-bound* threshold keeps strong hits and drops the
    # weak/off-topic ones, mirroring the siblings' MIN_RECALL_SCORE default.
    MIN_RECALL_SCORE = 0.5

    def _ensure_connected(self) -> bool:
        """Establish MCP connection if it hasn't been established yet.

        Lazy initialization — the subprocess is not spawned until this method
        is called for the first time.  Returns True when a live session exists,
        False when the probe fails (caller should fall back to local cache).

        Honors ``skip_mcp``: when True the connection attempt is skipped
        entirely and False is returned immediately.
        """
        # skip_mcp takes precedence — never try to connect when explicitly disabled.
        if self.config.get("skip_mcp", False):
            return False

        if self._connected and self._client.is_alive:
            return True

        try:
            self._connected = self._client.connect()
        except Exception:
            self._connected = False

        return self._connected

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
            self.config.get("cache_dir", str(hermes_home() / "mempalace_cache"))
        )
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # MCP client — created eagerly but does *not* attempt a connection yet.
        # The actual subprocess spawn is deferred until first data-plane use via
        # _ensure_connected().  This keeps instantiation lightweight so that
        # auto_bootstrap probe-only checks and orchestrator wiring don't pay
        # the fork/exec overhead when MCP is unreachable or unused.
        mcp_timeout = float(self.config.get("mcp_timeout", 10))
        self._client = _McpClient(timeout=mcp_timeout, config=self.config)
        self._connected = False

        # If skip_mcp is explicitly set, skip lazy connection entirely.
        if self.config.get("skip_mcp", False):
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

        if self._ensure_connected() and self._client.is_alive:
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
        """Look up the memory.  Returns cached value when available; None otherwise.

        GAP044 fix: The local JSON cache is authoritative — it stores the exact
        value that ``save()`` received via ``self._cache_locally(key, value)``.
        Since ``save()`` mirrors to both MCP and local cache, a cached JSON file
        is definitive proof the key was genuinely stored through MemChorus.

        We return the cached value directly without querying MCP, which eliminates:

        - **Fabricated data:** MCP returning semantically similar but wrong hits.
        - **Type corruption (dict→string):** MCP ``_from_str`` on non-JSON content
          producing a raw string instead of the original dict type.

        The only path to None is when the key was never saved through this source
        (no cache file). This preserves graceful degradation while guaranteeing
        type and content fidelity on every successful round-trip.
        """
        filepath = self._cache_dir / f"{key}.json"
        if not filepath.exists():
            return None

        try:
            with open(filepath) as f:
                return json.load(f)
        except Exception:
            return None

    def search(self, query: str, limit: int = 10, *, wing: Optional[str] = None, room: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search across MCP + local cache, deduplicating by key.

        §6 AC-R6.3: Optional wing/room filters for targeted recall.
        When neither is specified, searches across all wings (full recall).
        """
        results: List[Dict[str, Any]] = []
        seen_keys: set = set()

        if self._ensure_connected() and self._client.is_alive:
            mp_results = self._client.search(
                query=query, limit=limit, wing=wing, room=room
            )
            if mp_results:
                min_score = self._resolve_min_recall_score()
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
                        try:
                            entry["score"] = float(r["similarity"])
                        except (TypeError, ValueError):
                            entry["score"] = 0.0
                    # (#139) Drop weak / off-topic results before injection.
                    # Entries without a score (no similarity reported) are kept —
                    # the floor only applies to scored MCP hits.
                    if "score" in entry and entry["score"] < min_score:
                        continue
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

    @property
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
            "available": self.is_available,
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

    def _resolve_min_recall_score(self) -> float:
        """Effective recall floor, overridable via ``config['min_recall_score']``.

        Mirrors ``HermesMemorySource`` / ``SessionSearchMemorySource`` so the
        MemPalace source honours the same threshold contract. (#139)
        """
        return self.config.get("min_recall_score", self.MIN_RECALL_SCORE)

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
        if self._ensure_connected() and self._client.is_alive:
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
        if self._ensure_connected() and self._client.is_alive:
            try:
                hits = self._client.search(query=key, limit=5)
                if not isinstance(hits, list):
                    hits = []
            except Exception:  # noqa: BLE001 — graceful degradation on MCP unavailability
                hits = []
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                # Check if this hit matches our key
                hit_key = (hit.get("key", "") or "").lower()
                if key.lower() == hit_key:
                    drawer_id = hit.get("drawer_id") or hit.get("id")
                    if drawer_id:
                        try:
                            result = self._client.call_tool(
                                "mempalace_delete_drawer",
                                {"drawer_id": str(drawer_id)},
                            )
                            if result is not None:
                                deleted = True
                                break
                        except Exception:  # noqa: BLE001 — graceful degradation
                            pass

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
