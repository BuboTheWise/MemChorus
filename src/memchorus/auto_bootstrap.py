"""auto_bootstrap.py \u2014 MemChorus v1.2 auto-bootstrap subsystem.

Implements the 5-step auto-bootstrap sequence defined in the
MemChorus v1.2 spec (AC-A1 through AC-A4).

Key properties
--------------\n* ``MEMCHORUS_AUTO_ENABLED=false`` prevents *all* bootstrap side effects;
  import still succeeds silently.
* MemPalace MCP unreachability (probe-step failure) degrades to
  HermesDefault only with a single warning log line \u2014 no exception leaks.
* ``memchorus._instance`` is ``None`` until first symbol access after load,
  then cached as a singleton (AC-A4).

Config precedence: env var > ~/.hermes/memchorus.yaml > hardcoded defaults.
"""

# stdlib -----------------------------------------------------------------
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# --- third-party (optional) ------------------------------------------------
try:
    import yaml  # type: ignore[import-not-found]
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# --- hardcoded defaults (the zero layer) ------------------------------------

_DEFAULTS: Dict[str, Any] = {
    "auto_enabled": True,
    "default_source": "hermes_default",
    "half_life_days": 30.0,
    "cache_ttl_seconds": 60,
    "custom_loops_dir": str(Path.home() / ".hermes" / "custom_loops"),
}


# --- helpers ----------------------------------------------------------------

def _load_yaml_config() -> Dict[str, Any]:
    """Read ~/.hermes/memchorus.yaml (or similar) if it exists.

    Returns an empty dict when the file is missing, YAML is unavailable, or
    the file parses to a non-dict value \u2014 never raises on external failure.
    """
    if not _HAS_YAML:
        return {}

    for candidate in (
        os.path.expanduser("~/.hermes/memchorus.yaml"),
        os.path.expanduser("~/.memchorus.yaml"),
    ):
        if os.path.isfile(candidate):
            try:
                with open(candidate) as fh:  # type: ignore[possibly-unbound-variab]
                    data = yaml.safe_load(fh)
                if isinstance(data, dict):
                    logger.debug("Loaded YAML config from %s", candidate)
                    return data
                logger.warning(
                    "YAML config at %s is not a mapping; skipping.", candidate
                )
            except Exception as exc:  # pragma: no cover \u2014 defensive
                logger.debug("Failed to read %s \u2014 %s", candidate, exc)
    return {}


def _resolve_boolean(raw: Any) -> bool:
    """Normalise any truthy / falsy source to a strict Python boolean."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(raw)


def _resolve_int(raw: Any) -> int:
    """Cast *raw* to int; fall back to 0 when the value is unusable."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _resolve_float(raw: Any) -> float:
    """Cast *raw* to float; fall back to 30.0 when the value is unusable."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 30.0


# --- main bootstrap function ------------------------------------------------

def _bootstrap() -> Optional[Any]:
    """Execute the v1.2 auto-bootstrap sequence and return a MemoryOrchestrator
    on success, or ``None`` when disabled (`auto_enabled=False`).

    The 5-step sequence (from the spec):

    1. **Config resolution** \u2014 merge env vars + YAML + defaults (high \u2192 low priority)
    2. **Enabled gate** \u2014 short-circuit to ``None`` when auto is disabled
    3. **MemPalace probe** \u2014 attempt MCP connectivity check; record status only
    4. **Source wiring** \u2014 build the orchestrator config dict with resolved sources
    5. **Orchestrator create** \u2014 instantiate *MemoryOrchestrator* and return it

    Every step is separately try/except'd (graceful degradation).
    """
    # --- Step 1: Config resolution ---
    yaml_cfg = _load_yaml_config()
    config: Dict[str, Any] = dict(_DEFAULTS)

    # YAML layer (medium priority)
    for key in ("default_source", "half_life_days", "cache_ttl_seconds"):
        if key in yaml_cfg:
            config[key] = yaml_cfg[key]  # type: ignore[typeddict-item]

    # custom_loops_dir from YAML (expand ~ manually since yaml returns str)
    if "custom_loops_dir" in yaml_cfg:
        raw_dir = str(yaml_cfg["custom_loops_dir"])
        config["custom_loops_dir"] = os.path.expanduser(raw_dir)

    auto_enabled_raw = yaml_cfg.get("auto_enabled", _DEFAULTS["auto_enabled"])
    config["auto_enabled"] = _resolve_boolean(auto_enabled_raw)

    # Env var layer (high priority — overrides everything else)
    env_auto = os.environ.get("MEMCHORUS_AUTO_ENABLED")
    if env_auto is not None:
        config["auto_enabled"] = _resolve_boolean(env_auto)

    for key in ("default_source",):
        env_val = os.environ.get(f"MEMCHORUS_{key.upper()}")
        if env_val is not None:
            config[key] = env_val  # type: ignore[typeddict-item]

    env_hl = os.environ.get("MEMCHORUS_HALF_LIFE_DAYS")
    if env_hl is not None:
        config["half_life_days"] = _resolve_float(env_hl)

    env_ttl = os.environ.get("MEMCHORUS_CACHE_TTL_SECS")
    if env_ttl is not None:
        config["cache_ttl_seconds"] = _resolve_int(env_ttl)

    # custom_loops_dir from env var (highest priority — overrides YAML + default)
    env_loops = os.environ.get("MEMCHORUS_CUSTOM_LOOPS_DIR")
    if env_loops is not None:
        config["custom_loops_dir"] = os.path.expanduser(env_loops)

    # --- Step 2: Enabled gate ---
    enabled = config.pop("auto_enabled")
    if not enabled:
        logger.info(
            "MemChorus auto-bootstrap is disabled (MEMCHORUS_AUTO_ENABLED=false). "
            "No hooks or instances will be registered."
        )
        return None

    default_source = config.pop("default_source")
    half_life_days = config.pop("half_life_days")
    cache_ttl_seconds = config.pop("cache_ttl_seconds")
    custom_loops_dir = config.pop("custom_loops_dir")

    logger.debug(
        "Bootstrap config resolved: sources=%s, half_life=%.1f, ttl=%ss, "
        "loops_dir=%s",
        default_source, half_life_days, cache_ttl_seconds, custom_loops_dir,
    )

    # --- Step 3: MemPalace probe ---
    mp_available = False
    try:
        from memchorus.mempalace_memory_source import MemPalaceMemorySource  # noqa: F401
        
        _mp_src = MemPalaceMemorySource()
        mp_available = True
    except Exception as exc:
        logger.warning(
            "MemPalace MCP server unreachable during bootstrap probe \u2014 "
            "will continue with %s only. Error: %s",
            default_source, exc,
        )

    logger.info(
        "MEMCHORUS auto_bootstrap complete \u2014 source '%s' available=%s",
        default_source, mp_available,
    )
    if not mp_available:
        logger.warning(
            "mempalace unavailable at bootstrap time; falling back to %s only.",
            default_source,
        )

    # --- Step 4: Source wiring (build orchestrator config) ---
    orchestrator_cfg: Dict[str, Any] = {
        "default_source": default_source,
        "half_life_days": half_life_days,
        "cache_ttl_seconds": float(cache_ttl_seconds),
        "mempalace_config": {"skip_mcp": not mp_available},
    }

    # --- Step 5: Orchestrator creation & return ---
    try:
        from memchorus.orchestrator import MemoryOrchestrator

        logger.info(
            "MemChorus MemoryOrchestrator bootstrapped (source '%s', MemPalace=%s).",
            default_source, mp_available,
        )
        orchestrator = MemoryOrchestrator(config=orchestrator_cfg)

    except Exception as exc:
        logger.error("Failed to create MemoryOrchestrator during bootstrap: %s", exc)
        return None

    # --- Step 5b: propagate orientation config ----------------------------
    # Make the orchestrator's resolved TTL available so orientation.search()
    # doesn't hard-code 60 s when a different value was chosen.
    orient_ttl = float(cache_ttl_seconds) if cache_ttl_seconds else 60.0
    try:
        import memchorus.orientation as orient_mod
        orient_mod.DEFAULT_CACHE_TTL_SECONDS = orient_ttl
    except Exception:
        # Orientation not installed yet → harmless
        pass

    # --- Step 6: feedback loop auto-load ----------------------------------
    try:
        from memchorus.feedback_loop.integration import auto_load_custom_loops

        _loop_diag = auto_load_custom_loops(loop_dir=custom_loops_dir)
        logger.info(
            "Feedback loops loaded: %d definitions, %d skipped files, "
            "%d warnings, errors=%s (dir=%s)",
            _loop_diag.get("loaded", 0),
            _loop_diag.get("skipped_files", 0),
            len(_loop_diag.get("warnings", [])),
            _loop_diag.get("error") or "none",
            custom_loops_dir,
        )
    except Exception as exc:
        logger.warning(
            "Feedback loop auto-load failed (non-fatal; hooks degrade gracefully): %s",
            exc,
        )
    return orchestrator
