"""MemChorus <-> Hermes Agent integration plugin (LEGACY SHIM — GH-270 L2).

This package is the *old* standalone-plugin install path for MemChorus. Its
implementation was moved into the canonical :mod:`memchorus.hooks` module and
the Hermes entry point now points directly at it
(``memchorus = "memchorus.hooks"`` in ``pyproject.toml``). Everything this
module used to do — lazy bootstrap, hook handlers, ``register(ctx)`` — now
lives there, and the save_triggers / lifecycle-hook wiring is driven from
``integrations/hermes/plugin.yaml`` via the canonical loader.

This shim exists ONLY so existing installs / import sites that still reference
``integrations.hermes`` keep working. It forwards each legacy symbol to its
canonical counterpart:

    register(ctx)            -> memchorus.hooks.register(ctx)
    _bootstrap()             -> memchorus.get_orchestrator()
    _on_pre_llm_call(**kw)   -> memchorus.hooks.pre_llm_call(**kw)
    _on_post_tool_call(**kw) -> memchorus.hooks.post_tool_call(**kw)
    __version__              -> memchorus.__version__

It no longer builds its own orchestrator or wires its own handlers — doing so
would double-register the hooks and double-fire the save_triggers merge that
the canonical :func:`memchorus.hooks.register` already performs (GH-270 L1).

Use ``memchorus.hooks`` directly in new code; this package is slated for
removal once no external reference to ``integrations.hermes`` remains.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "register",
    "bootstrap",
    "_bootstrap",
    "_on_pre_llm_call",
    "_on_post_tool_call",
    "__version__",
]

# One-time notice so a legacy caller knows this path is a forwarder, not the
# real implementation. Emitted the first time a legacy symbol is touched.
_DEPRECATION_NOTE = (
    "integrations.hermes is a legacy deprecation shim (GH-270). "
    "Use memchorus.hooks (canonical) directly — this module only forwards."
)
_WARNED = {"flag": False}


def _warn_deprecated(name: str) -> None:
    if _WARNED["flag"]:
        return
    _WARNED["flag"] = True
    logger.warning("%s (referenced legacy symbol: %s)", _DEPRECATION_NOTE, name)


# Per-context registration guard so calling the legacy ``register(ctx)`` more
# than once for the same plugin context is a no-op (the canonical register
# itself does not track this, and double-wiring would double-fire hooks).
_REGISTERED_CTX_IDS: "set[int]" = set()


def register(ctx: Any) -> None:  # noqa: ANN001
    """Legacy ``register`` — delegates to the canonical ``memchorus.hooks.register``.

    Idempotent per ``ctx``: a second call for the same plugin context is a
    no-op so the canonical register's save_triggers merge / hook wiring /
    atexit flush are applied exactly once.
    """
    _warn_deprecated("register")
    ctx_id = id(ctx)
    if ctx_id in _REGISTERED_CTX_IDS:
        return
    # Lazy-import the canonical module so this shim can still be imported
    # before the pip install has completed.
    try:
        import memchorus.hooks as _hooks
    except ImportError as exc:
        logger.warning(
            "integrations.hermes.register: memchorus.hooks unavailable — %s", exc
        )
        return
    try:
        _hooks.register(ctx)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("integrations.hermes.register: canonical register failed: %s", exc)
        raise
    _REGISTERED_CTX_IDS.add(ctx_id)


# --- Compatibility aliases -------------------------------------------------


def _bootstrap():
    """Legacy orchestrator accessor — forwards to ``memchorus.get_orchestrator``.

    Returns the MemoryOrchestrator instance for the current profile, or ``None``
    when bootstrap has not yet run / the package is not installed. Matches the
    old return contract of the inline bootstrap that used to live here.
    """
    _warn_deprecated("_bootstrap")
    try:
        import memchorus
    except ImportError:
        return None
    getter = getattr(memchorus, "get_orchestrator", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _on_pre_llm_call(*_args: Any, **kw: Any) -> Optional[Dict[str, Any]]:
    """Legacy pre_llm_call handler — forwards to the canonical handler."""
    _warn_deprecated("_on_pre_llm_call")
    try:
        import memchorus.hooks as _hooks
    except ImportError:
        return None
    try:
        return _hooks.pre_llm_call(**kw)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("integrations.hermes._on_pre_llm_call: %s", exc)
        return None


def _on_post_tool_call(*_args: Any, **kw: Any) -> Optional[Dict[str, Any]]:
    """Legacy post_tool_call handler — forwards to the canonical handler."""
    _warn_deprecated("_on_post_tool_call")
    try:
        import memchorus.hooks as _hooks
    except ImportError:
        return None
    try:
        return _hooks.post_tool_call(**kw)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("integrations.hermes._on_post_tool_call: %s", exc)
        return None


def __getattr__(name: str) -> Any:  # PEP 562
    """Expose ``__version__`` (and any other legacy names) as a passthrough.

    Keeps ``from integrations.hermes import __version__`` working against the
    canonical package version without re-hardcoding it here.
    """
    if name == "__version__":
        try:
            import memchorus
        except ImportError:
            # Fall back to the plugin.yaml version so the shim is always importable.
            return "1.0.0"
        return getattr(memchorus, "__version__", "1.0.0")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
