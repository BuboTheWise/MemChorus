"""GH-284 Issue B, Scope 2 — precise unavailability diagnostics.

The setup wizard and the dashboard both need a *specific* reason when
``MemChorusMemoryProvider.is_available()`` returns ``False``.  Before this
change the swallowed exception was only in a ``debug`` log line, so the
operator saw a generic "check credentials" hint with no actionable fix.

This module classifies the failure into one of a fixed set of causes and
pairs each with the exact command to run on the current interpreter.  The
output is safe to surface in the CLI, the dashboard, ``hermes doctor``, and
orientation text: no credentials, no raw paths except the canonical ones
already part of the fix string.

Causes (in check order):

1. ``missing-package``   — ``import memchorus`` raised; the SDK is not
   installed for this interpreter.
2. ``version-mismatch``  — the SDK imported but reports no usable
   ``__version__`` (partial / stale copy).
3. ``stale-plugin-dir``  — the plugin dir under
   ``$HERMES_HOME/plugins/memchorus`` is missing or a broken symlink.
4. ``store-path``        — the legacy provider config points at a data path
   that does not exist on this host.
5. ``unknown``           — none of the above matched; a conservative
   diagnostic that still names the provider and the canonical install.

The *comparison* of installed-vs-declared version (and the "refuse unsafe
writes" behaviour) is owned by the Scope-3 version/schema gate.  This module
only needs to tell the operator *which* thing is wrong and *what command*
fixes it on the current interpreter.
"""

from __future__ import annotations

import importlib
import os
import re
from typing import Optional

__all__ = ["unavailable_reason"]

# Provider manifest dir name, as it appears under $HERMES_HOME/plugins/.
_PROVIDER_NAME = "memchorus"

# Canonical install command for the SDK.  This is the same string the
# ``external_dependencies`` block in ``plugin.yaml`` emits, so a user can
# copy-paste it from the diagnostic.  No credentials, no host-specific
# prefix — safe to surface anywhere.
_CANONICAL_INSTALL = (
    'pip install "memchorus @ '
    "git+https://github.com/BuboTheWise/MemChorus.git@master\""
)

# Collapse absolute file paths in exception text into a neutral token so the
# reason never leaks a /home/<user>/... path.  Preserves dotted module names
# and relative paths (integrations/provider).
_PATH_RE = re.compile(r"(/[A-Za-z0-9._\-]+){2,}")


def _sanitize_exception_message(exc: BaseException) -> str:
    """Reduce an exception to a one-line, secret-free message."""
    text = str(exc)
    if not text:
        return type(exc).__name__
    text = _PATH_RE.sub("[path]", text)
    text = " ".join(text.split())
    if len(text) > 160:
        text = text[:157] + "..."
    return text


def _importable() -> Optional[str]:
    """Try ``import memchorus`` in the current interpreter.

    Returns ``None`` when the import succeeds, or a sanitised one-line
    message describing the failure.
    """
    try:
        importlib.import_module("memchorus")  # noqa: F401
        return None
    except BaseException as exc:  # noqa: BLE001 - provider must not crash the wizard
        return _sanitize_exception_message(exc)


def _version_comparable(expected_version: Optional[str]) -> Optional[str]:
    """Compare the installed SDK version against the expected one.

    Returns ``None`` when the installed version matches ``expected_version``
    (or when no expected version was supplied), or a one-line diagnostic.
    Uses ``packaging.version`` (a core dependency) for robust comparison.
    """
    try:
        import memchorus  # noqa: F401
    except BaseException:
        # The missing-package diagnostic will have already fired.
        return None
    installed = getattr(memchorus, "__version__", None)
    if installed is None or not isinstance(installed, str) or not installed.strip():
        return (
            "installed memchorus package has no usable __version__ "
            "(expected e.g. '2.0.80'); the copy on this host does not "
            "expose the version attribute"
        )
    if not expected_version:
        return None
    try:
        from packaging.version import Version
    except ImportError:
        return None
    try:
        if Version(installed.strip()) != Version(expected_version.strip()):
            return (
                f"installed memchorus version {installed!r} does not match "
                f"the expected version {expected_version!r} declared in the "
                "provider manifest — the SDK on this host is a different "
                "build than the one this provider was written against"
            )
    except BaseException:
        # Unparseable version on either side — report as mismatch so the
        # operator re-installs rather than silently proceeding.
        return (
            f"installed memchorus version {installed!r} could not be "
            f"compared against the expected {expected_version!r} — "
            "the two are not comparable under PEP-440"
        )
    return None


def _plugin_dir_ok(hermes_home: str) -> Optional[str]:
    """Verify ``$HERMES_HOME/plugins/memchorus`` is a real, non-broken dir."""
    plugin_dir = os.path.join(hermes_home, "plugins", _PROVIDER_NAME)
    if os.path.islink(plugin_dir) and not os.path.exists(plugin_dir):
        return (
            "provider plugin directory plugins/memchorus/ is a broken "
            "symlink — the target it points at does not exist on this host"
        )
    if not os.path.exists(plugin_dir):
        return (
            "provider plugin directory plugins/memchorus/ is missing under "
            "$HERMES_HOME — the provider manifest is not present on this host"
        )
    return None


def _store_path_ok(hermes_home: str) -> Optional[str]:
    """If legacy provider config sets a data path, verify it exists."""
    config_path = os.path.join(hermes_home, "config.yaml")
    if not os.path.isfile(config_path):
        return None
    try:
        import yaml  # noqa: F401
    except ImportError:
        return None
    try:
        with open(config_path, "r", encoding="utf-8-sig") as fh:
            config = yaml.safe_load(fh) or {}
    except BaseException:  # noqa: BLE001 - a broken config is handled elsewhere
        return None
    memory = config.get("memory") if isinstance(config, dict) else None
    if not isinstance(memory, dict):
        return None
    provider_cfg = memory.get(_PROVIDER_NAME)
    if not isinstance(provider_cfg, dict):
        return None
    data_path = provider_cfg.get("data_path")
    if not isinstance(data_path, str) or not data_path:
        return None
    if not os.path.isdir(data_path):
        return (
            "provider config memory.memchorus.data_path points at a "
            "directory that does not exist on this host — the store was not "
            "restored from the source host"
        )
    return None


def unavailable_reason(
    hermes_home: Optional[str] = None,
    expected_version: Optional[str] = None,
) -> Optional[str]:
    """Return the exact reason MemChorus is unavailable, or ``None``.

    Parameters
    ----------
    hermes_home:
        Root of the Hermes home directory.  Defaults to the ``HERMES_HOME``
        env var, then ``None`` (which makes the plugin-dir and store-path
        checks skip).  Callers with a known home should pass it explicitly.
    expected_version:
        Version declared in the provider manifest (``plugin.yaml``).  Used
        only to make the version-mismatch message concrete.  The actual
        installed-vs-declared comparison is owned by the Scope-3 gate.

    Returns a one-line, secret-free diagnostic when the provider is
    unavailable for a classifiable reason, or ``None`` when every check
    passes (caller then treats the provider as available).
    """
    home = hermes_home or os.environ.get("HERMES_HOME") or None

    # 1. Package import — the primary failure mode after a fresh restore.
    import_msg = _importable()
    if import_msg is not None:
        return (
            "missing-package: the memchorus SDK is not importable by this "
            f"interpreter ({import_msg}). Fix: run `{_CANONICAL_INSTALL}` "
            "and retry."
        )

    # 2. Version surface — package present but version mismatch or surface
    #    broken.
    version_msg = _version_comparable(expected_version)
    if version_msg is not None:
        return (
            f"version-mismatch: {version_msg}. Fix: run "
            f"`{_CANONICAL_INSTALL}` to install the canonical versioned SDK "
            "and retry."
        )

    # 3. Plugin dir integrity.
    if home:
        dir_msg = _plugin_dir_ok(home)
        if dir_msg is not None:
            return (
                f"stale-plugin-dir: {dir_msg}. Fix: re-run `hermes import` "
                "(full) or `hermes profile import --name <profile>` from the "
                f"source host, then `{_CANONICAL_INSTALL}`."
            )
        # 4. Legacy store-path foreign config.
        store_msg = _store_path_ok(home)
        if store_msg is not None:
            return (
                "store-path: "
                f"{store_msg}. Fix: clear memory.memchorus.data_path from "
                "config.yaml or restore the store from the source host, then "
                "retry."
            )

    # Every classifiable check passed.  When the caller passed a hermes_home
    # we have inspected it fully and found no problem — report available.
    # When no hermes_home was available we only verified the SDK import and
    # version, so still report available (the other checks are best-effort).
    return None
