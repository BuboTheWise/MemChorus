"""Auto-generate MemChorus routing configuration for new agents / humans.

This module removes the manual YAML-editing step so a fresh install needs
nothing more than ``pip install memchorus[mcp]`` to become fully operational.

Usage
-----
CLI (wired via setup.py entry_points): ::

    memchorus-init --profile my_agent

Python API: ::

    from memchorus.auto_init import generate_config, enable_plugin
    yaml_text = generate_config(profile="my_agent")
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from typing import Optional

from memchorus.hermes_home import hermes_home

# ---------------------------------------------------------------------------
# PyYAML guard — it is in core install_requires so this should never fire,
# but keep the message in case someone does a bare ``pip install memchorus``.
# ---------------------------------------------------------------------------

try:
    import yaml  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - install_requires guarantees pyyaml
    raise RuntimeError(
        "PyYAML is required for config generation.\n"
        "Install via: pip install 'memchorus>=2.0.0'"
    ) from None

# ---------------------------------------------------------------------------
# Constants — sensible defaults that work for 95 % of agents out of the box.
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".mempalace")

_WING_MAP_TEMPLATE: dict[str, list[str]] = {
    "DECISION": ["project", "design", "architecture"],
    "LEARNING": ["session_meta", "patterns", "workflows"],
    "MISTAKE": ["error_patterns", "debugging", "lessons"],
    "RESULT": ["outcomes", "deliverables"],
}


# ---------------------------------------------------------------------------
# Core generators
# ---------------------------------------------------------------------------

def _build_wing_map(profile_name: str) -> dict[str, list[str]]:
    """Build a namespaced wing map so profiles don't collide.

    The ``default`` profile keeps the canonical un-prefixed wing keys for
    backward-compatibility; every other profile gets ``<slug>_PREFIXED`` keys.
    """
    wmap: dict[str, list[str]] = {}
    prefix = "" if profile_name == "default" else f"{profile_name}_"
    for base_wing, rooms in _WING_MAP_TEMPLATE.items():
        wmap[f"{prefix}{base_wing}"] = list(rooms)  # defensive copy
    return wmap


def generate_config(profile: Optional[str] = None,
                    data_dir: Optional[str] = None) -> str:
    """Return a complete ``memchorus.yaml`` as a YAML string.

    Parameters
    ----------
    profile :
        Agent/human slug (e.g. ``"my_agent"``). Falls back to
        ``$HERMES_KANBAN_PROFILE`` env var, then ``"default"``.
    data_dir :
        Absolute path on disk where MemPalace stores its durable files.
        Defaults to ``~/.mempalace/<profile>``.

    Writer/reader agreement, by construction:

    The ``data_dir`` recorded here is the **root / parent** the reader
    (MCP ``--palace``) and operator would hold.  The *leaf* that contains
    ``chroma.sqlite3`` is decided by
    :func:`memchorus.palace_path.palace_data_dir` (root itself if the data is
    already at the root, else ``<root>/palace`` for the MemPalace
    ``DEFAULT_PALACE_PATH`` layout).  ``mempalace_memory_source``, the drawer
    writer, calls that resolver at transport time, so writer and reader can
    not structurally disagree: both route through
    :mod:`memchorus.palace_path`.

    Returns
    -------
    str
        YAML ready for ``Path.write_text()`` or CLI stdout emission.
    """
    profile = (profile
               or os.environ.get("HERMES_KANBAN_PROFILE")
               or "default")

    data_dir = (data_dir
                or os.path.join(DEFAULT_DATA_DIR, profile))
    resolved_ddir = os.path.expanduser(data_dir) if "~" in str(data_dir) else data_dir

    mp_cfg: dict[str, object] = {
        "python_bin": "{hermes_venv}/bin/python3",
        "mcp_timeout": 15,
        "skip_mcp": False,
    }

    config_dict: dict[str, object] = {}
    if profile != "default":
        config_dict["profile_name"] = profile

    # --- mempalace_config section ------------------------------------------
    config_dict["mempalace_config"] = {
        "data_dir": resolved_ddir,
        "mempalace_routing": {"wing_map": _build_wing_map(profile)},
        "runtime": {"mempalace_home": resolved_ddir},
    }

    # --- hermes_default_config section -------------------------------------
    config_dict["hermes_default_config"] = mp_cfg.copy()

    return yaml.dump(config_dict, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Filesystem writers — idempotent, profile-aware
# ---------------------------------------------------------------------------

def _resolve_config_path(profile: str) -> Path:
    """Return the target config path for *profile*."""
    if profile == "default":
        return hermes_home() / "memchorus.yaml"
    dst = hermes_home() / "profiles" / profile / "memchorus.yaml"
    return dst


def write_config(profile: Optional[str] = None,
                 data_dir: Optional[str] = None) -> Path:
    """Generate *and* write a config file. Returns the destination path.

    Prints an ``[ok]`` or ``[warn]`` message to stdout/stderr so callers
    (CLI hooks, scripts) can inspect exit code alone for success/failure.
    """
    profile = (profile
               or os.environ.get("HERMES_KANBAN_PROFILE")
               or "default")

    dst = _resolve_config_path(profile)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)

    existing = dst.read_text() if dst.exists() else ""
    desired = generate_config(profile=profile, data_dir=data_dir)

    if existing and existing.strip() == desired.strip():
        print(f"[skip] {dst} already up-to-date")
        return dst

    # Atomic write — temp file then rename prevents partial corruption on crash.
    tmp = dst.with_suffix(".tmp")
    try:
        tmp.write_text(desired)
        os.replace(tmp, dst)
    except OSError as exc:
        print(f"[error] failed to write {dst}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"[ok] routing config written to {dst}")
    return dst


# ---------------------------------------------------------------------------
# Plugin enablement helper (edits ~/.hermes/config.yaml safely)
# ---------------------------------------------------------------------------

def _resolve_hermes_config(profile: str) -> Path:
    if profile == "default":
        return hermes_home() / "config.yaml"
    return hermes_home() / "profiles" / profile / "config.yaml"


def enable_plugin(profile: Optional[str] = None) -> bool:
    """Ensure ``memchorus`` appears in the ``plugins.enabled`` list.

    Returns ``True`` if the file was modified, ``False`` if it already had
    the plugin listed (idempotent).
    """
    profile = (profile
               or os.environ.get("HERMES_KANBAN_PROFILE")
               or "default")

    cfg_path = _resolve_hermes_config(profile)
    if not cfg_path.exists():
        print(f"[info] skipping plugin enable — {cfg_path} not found",
              file=sys.stderr)
        return False

    with open(cfg_path, "r") as f:
        lines = f.readlines()

    # Look for the ``enabled:`` line and add memchorus if absent.
    wrote_it = False
    for idx, line in enumerate(lines):
        stripped = line.strip().lower()
        if stripped.startswith("enabled:"):
            items_part = stripped.split(":", 1)[1].strip()
            # Handle both inline list ``[a, b]`` and empty brackets ``[]``
            inner = items_part.strip("[]")
            entries = [e.strip() for e in inner.split(",") if e.strip()]
            if "memchorus" not in entries:
                entries.append("memchorus")
                lines[idx] = f"  enabled: [{', '.join(entries)}]{lines[idx][len(line.rstrip()):]}"
                wrote_it = True
            break

    if not wrote_it and all(not l.strip().startswith("enabled:") for l in lines):
        # No ``enabled:`` line at all — append one near the bottom.
        lines.append("\nplugins:\n  enabled: [memchorus]\n")
        wrote_it = True

    if not wrote_it:
        print(f"[skip] memchorus already enabled in {cfg_path}")
        return False

    tmp = cfg_path.with_suffix(".tmp")
    try:
        tmp.write_text("".join(lines))
        os.replace(tmp, cfg_path)
    except OSError as exc:
        print(f"[error] failed to update {cfg_path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"[ok] memchorus added to plugins.enabled in {cfg_path}")
    return True


# ---------------------------------------------------------------------------
# CLI entry-point (wired in setup.py)
# ---------------------------------------------------------------------------

def cli_main() -> int:
    """Entry point for the ``memchorus-init`` command."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="memchorus-init",
        description="Bootstrap MemChorus routing configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              %(prog)s -p my_agent               # generate + write config
              %(prog)s -p my_agent --dry-run       # print YAML to stdout only
              %(prog)s --data-dir /opt/data       # custom data root
        """),
    )
    parser.add_argument("-p", "--profile", default=None,
                        help="Agent/human profile slug (default from $HERMES_KANBAN_PROFILE or 'default')")  # noqa: E501
    parser.add_argument("-d", "--data-dir", default=None,
                        help=f"Absolute data directory (default: {{DEFAULT_DATA_DIR}}/<profile>)")  # noqa: E501
    parser.add_argument("--dry-run", action="store_true",
                        help="Print generated YAML to stdout instead of writing a file")
    parser.add_argument("--disable-plugin", action="store_true", default=False,
                        help="Do NOT add memchorus to plugins.enabled (default: add it)")

    args = parser.parse_args()
    profile = args.profile or os.environ.get("HERMES_KANBAN_PROFILE") or "default"

    if not args.dry_run:
        write_config(profile=profile, data_dir=args.data_dir)
        if not args.disable_plugin:
            enable_plugin(profile=profile)
    else:
        print(generate_config(profile=profile, data_dir=args.data_dir))

    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
