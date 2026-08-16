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
}

# Wing/room routing defaults (§1 + §3 of spec).  Mirrored here so that
# ``mempalace_routing`` can be configured before the Orchestrator is built.
_DEFAULT_MEMPALACE_ROUTING: Dict[str, Any] = {
    "wing_map": {
        "DECISION": "memchorus_decisions",
        "LEARNING": "memchorus_learning",
        "MISTAKE":  "memchorus_learning",
        "RESULT":   "memchorus_general",
        "default":  "memchorus_general",
    },
    "room_map": {
        "DECISION": "decisions",
        "LEARNING": "lessons-learned",
        "MISTAKE":  "corrections",
        "RESULT":   "outcomes",
        "default":  "general",
    },
}


# --- helpers ----------------------------------------------------------------

def _resolve_hermes_profile() -> str:
    """Return the active Hermes profile name.

    Resolution order:
      1. HERMES_PROFILE env var (set by dispatcher at spawn time) — sanitized
      2. Fall back to 'default' when not running under Hermes Kanban

    Returns a sanitized profile string safe for use in filesystem paths.
    """
    from memchorus import _sanitize_profile
    return _sanitize_profile(os.environ.get("HERMES_PROFILE", "default"))


def _load_yaml_config() -> Dict[str, Any]:
    """Read ~/.hermes/memchorus.yaml (or similar) if it exists.

    This is Layer 2 of the config cascade (global defaults). Does NOT include
    profile-specific overrides — those come from _load_profile_yaml_config().

    Returns an empty dict when the file is missing, YAML is unavailable, or
    the file parses to a non-dict value — never raises on external failure.
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
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("Failed to read %s — %s", candidate, exc)
    return {}


def _load_profile_yaml_config(profile: str) -> Dict[str, Any]:
    """Load profile-specific YAML overrides from ~/.hermes/profiles/<profile>/memchorus.yaml.

    This is Layer 3 of the config cascade. The loaded dict will be deep-merged
    onto the global config before env-var resolution (Layer 4).

    Returns an empty dict when the file doesn't exist or is malformed — never raises.
    """
    if not _HAS_YAML:
        return {}

    candidate = os.path.expanduser(f"~/.hermes/profiles/{profile}/memchorus.yaml")
    if not os.path.isfile(candidate):
        logger.debug("No profile-specific config at %s — skipping.", candidate)
        return {}

    try:
        with open(candidate) as fh:  # type: ignore[possibly-unbound-variable]
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            logger.info("Loaded profile-specific YAML config from %s (profile=%s)", candidate, profile)
            return data
        logger.warning(
            "Profile YAML config at %s is not a mapping; skipping.", candidate
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Failed to read profile config %s — %s", candidate, exc)
    return {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge *override* into *base*.

    Nested dicts are merged recursively; non-dict values in *override* replace
    the corresponding keys in *base*. Returns a new dict — neither input is
    mutated.
    """
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


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
    # --- Step 1: Config resolution (four-layer cascade) ---
    # Layer 1: hardcoded _DEFAULTS
    # Layer 2: ~/.hermes/memchorus.yaml (global config)
    # Layer 3: ~/.hermes/profiles/<HERMES_PROFILE>/memchorus.yaml (profile overrides)
    # Layer 4: MEMCHORUS_* env vars / MEMCHORUS_CONFIG JSON

    global_cfg = _load_yaml_config()
    profile_name = _resolve_hermes_profile()
    profile_cfg = _load_profile_yaml_config(profile_name)

    # Merge: defaults <- global YAML <- profile YAML (deep merge for nested values)
    config: Dict[str, Any] = dict(_DEFAULTS)
    config = _deep_merge(config, global_cfg)
    config = _deep_merge(config, profile_cfg)

    # If both global or profile set data_dir/hermes_default_config via their
    # YAML, the deep merge will have resolved it by now. We only need to look
    # at 'config' from this point on — no parallel yaml_cfg indirection.
    yaml_cfg = config  # alias for backward compat with downstream references

    # enforcement toggles — recall (pre-decision memory retrieval) and storage
    # (post-action automatic capture). Both default to True so enforcement is
    # opt-out, not opt-in. The user must explicitly set these to false to disable.
    enforce_on_read = _resolve_boolean(config.get("enforce_on_read", True))
    enforce_on_write = _resolve_boolean(config.get("enforce_on_write", True))

    auto_enabled_raw = config.get("auto_enabled", _DEFAULTS["auto_enabled"])
    config["auto_enabled"] = _resolve_boolean(auto_enabled_raw)

    # Env var layer (Layer 4 — highest priority, overrides everything else)
    env_auto = os.environ.get("MEMCHORUS_AUTO_ENABLED")
    if env_auto is not None:
        config["auto_enabled"] = _resolve_boolean(env_auto)

    for key in ("default_source",):
        env_val = os.environ.get(f"MEMCHORUS_{key.upper()}")
        if env_val is not None:
            config[key] = env_val

    env_hl = os.environ.get("MEMCHORUS_HALF_LIFE_DAYS")
    if env_hl is not None:
        config["half_life_days"] = _resolve_float(env_hl)

    env_ttl = os.environ.get("MEMCHORUS_CACHE_TTL_SECS")
    if env_ttl is not None:
        config["cache_ttl_seconds"] = _resolve_int(env_ttl)

    # Log resolved profile and layered config for debugging
    logger.info(
        "MemChorus bootstrap: profile=%s, global_cfg=%d keys, profile_cfg=%d keys",
        profile_name, len(global_cfg), len(profile_cfg),
    )
    # This lets tests and external callers override hermes_default_config,
    # mempalace_config, etc. without needing to hit YAML.
    import json
    env_json = os.environ.get("MEMCHORUS_CONFIG")
    if env_json:
        try:
            env_cfg = json.loads(env_json)
            if isinstance(env_cfg, dict):
                config.update(env_cfg)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid JSON in MEMCHORUS_CONFIG; ignoring it")

    # Translate nested adapter keys so the orchestrator actually finds them.
    # MEMCHORUS_CONFIG typically sends {"hermes_default": {...}} or {"mempalace": {...}},
    # but orchestrator.__init__ reads "hermes_default_config" and "mempalace_config".
    # Bridge the naming gap so overrides survive into adapter instantiation.
    _ALIAS_MAP = {
        "hermes_default": "hermes_default_config",
        "mempalace":      "mempalace_config",
    }
    for src_key, cfg_key in _ALIAS_MAP.items():
        if src_key in config and cfg_key not in config:
            config[cfg_key] = config.pop(src_key)

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

    logger.debug(
        "Bootstrap config resolved: sources=%s, half_life=%.1f, ttl=%ss",
        default_source, half_life_days, cache_ttl_seconds,
    )

    # Log enforcement toggle state so the user knows what is active at startup.
    logger.info(
        "MEMCHORUS enforcement toggles -- recall: %s, storage: %s",
        "enabled" if enforce_on_read else "disabled",
        "enabled" if enforce_on_write else "disabled",
    )

    # --- Step 3: MemPalace availability probe (import-only) ---
    # With lazy init, constructing MemPalaceMemorySource does NOT spawn a subprocess —
    # the actual connection is deferred until first data operation via _ensure_connected().
    # We only need to verify the class can be imported.
    mp_available = True
    try:
        from memchorus.mempalace_memory_source import MemPalaceMemorySource  # noqa: F401
    except Exception as exc:
        logger.warning(
            "MemPalace module unavailable during bootstrap — will continue with %s only. Error: %s",
            default_source, exc,
        )
        mp_available = False

    # AC-A3: probe failure warning already emitted above in except block.
    # Log bootstrap status without duplicating the warning.
    logger.info(
        "MEMCHORUS auto_bootstrap complete \u2014 source '%s' available=%s",
        default_source, mp_available,
    )

    # --- Step 4: Source wiring (build orchestrator config) ---
    orchestrator_cfg: Dict[str, Any] = {
        "default_source": default_source,
        "half_life_days": half_life_days,
        "cache_ttl_seconds": float(cache_ttl_seconds),
        "enforce_on_read": enforce_on_read,
        "enforce_on_write": enforce_on_write,
        "mempalace_config": {
            "skip_mcp": not mp_available,
            "mempalace_routing": yaml_cfg.get("mempalace_routing", _DEFAULT_MEMPALACE_ROUTING),
        },
    }

    # Allow mempalace_routing from top-level YAML too.
    routing_override = yaml_cfg.get("mempalace_routing")
    if isinstance(routing_override, dict):
        orchestrator_cfg["mempalace_config"]["mempalace_routing"] = routing_override

    # Merge user adapter overrides into the auto-computed dicts instead of
    # clobbering them entirely. A shallow `orchestrator_cfg.update(config)` would
    # replace `mempalace_config` (which carries auto-computed fields like
    # `skip_mcp`) with whatever the user YAML provided, silently dropping those
    # computed defaults. So we pop adapter sections and merge them in place.
    for adapter_key in ("mempalace_config", "hermes_default_config"):
        user_override = config.pop(adapter_key, None)
        if isinstance(user_override, dict):
            existing = orchestrator_cfg.setdefault(adapter_key, {})
            existing.update(user_override)

    # Forward any remaining non-adapter config through to the orchestrator.
    orchestrator_cfg.update(config)

    # --- Step 5: Orchestrator creation & return ---
    try:
        from memchorus.orchestrator import MemoryOrchestrator

        logger.info(
            "MemChorus MemoryOrchestrator bootstrapped (source '%s', MemPalace=%s).",
            default_source, mp_available,
        )
        orchestrator = MemoryOrchestrator(config=orchestrator_cfg)

    except Exception as exc:
        # Log the exception class + full traceback so bugs are diagnosable
        # rather than returning None with a vague "inactive" message.
        logger.error(
            "Failed to create MemoryOrchestrator during bootstrap (%s: %s); "
            "memchorus will be inactive. Traceback below.",
            type(exc).__name__, exc,
            exc_info=True,
        )
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

    # --- Step 6: removed feedback_loop auto-load (v1.9.0) ------------------
    return orchestrator
