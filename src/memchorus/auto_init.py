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
import re
import sys
import textwrap
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from memchorus import palace_path
from memchorus.hermes_home import hermes_home, profile_home, profile_config_path

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

# Issue #283 item 2: fresh installs default state to live *inside the profile
# home* rather than a sibling ~/.mempalace tree. The wing_isolation convention
# (commit 86995b5) established ``.mempalace/palace`` as the per-profile leaf;
# all profiles use this layout for data isolation.
DEFAULT_DATA_DIR = str(Path(hermes_home()) / ".mempalace" / "palace")

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

    Reader-side contract (GH-297): ``MemPalaceMemorySource._resolve_wing()``
    resolves the prefixed keys when constructed with the matching
    ``profile_name`` config (injected by ``auto_bootstrap``).  Values are room
    *lists* — the reader normalises them to the canonical shared writer wings
    (``memchorus_decisions`` / ``memchorus_learning`` / ``memchorus_general``)
    before the MCP save call, so drawers land in the shared ``memchorus_*``
    namespace that ``wing_isolation`` audits.
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

    The ``data_dir`` recorded below is computed by calling the single shared
    resolver :func:`memchorus.palace_path.palace_data_dir` on the configured
    root — the exact same function the MCP reader
    (:func:`mempalace_memory_source._normalize_palace_args`) delegates to when
    it opens ``chroma.sqlite3``. Both call sites therefore funnel through one
    code path for the parent-vs-leaf decision, so writer and reader agree
    structurally rather than by two independent constants coinciding.

    Returns
    -------
    str
        YAML ready for ``Path.write_text()`` or CLI stdout emission.
    """
    profile = (profile
               or os.environ.get("HERMES_KANBAN_PROFILE")
               or "default")

    # Issue #283 item 2 + wing_isolation convention: scope state per profile
    # inside that profile's home at ``.mempalace/palace`` (the per-profile
    # leaf established by commit 86995b5). The default profile lands at the
    # same dir as the DEFAULT_DATA_DIR constant above; named profiles are
    # isolated under their own ``profiles/<name>/.mempalace/palace`` tree.
    data_dir = (data_dir or str(profile_home(profile) / ".mempalace" / "palace"))
    data_dir = str(os.path.expanduser(data_dir)) if "~" in str(data_dir) else str(data_dir)
    # Single decision point: resolve parent-vs-leaf through the shared
    # resolver so the recorded config and the reader's own resolution can
    # never disagree (see palace_path.palace_data_dir for the rules).
    resolved_ddir = str(palace_path.palace_data_dir(data_dir))

    mp_cfg: dict[str, object] = {
        "python_bin": "{hermes_venv}/bin/python3",
        "mcp_timeout": 15,
        "skip_mcp": False,
    }

    config_dict: dict[str, object] = {}
    if profile != "default":
        config_dict["profile_name"] = profile

    # --- mempalace_config section ------------------------------------------
    # GH-325: ``routing.source`` is the required marker that makes the
    # ``mempalace_routing`` section *active*.  Without it the section is
    # treated as a silent no-op fallback — wing-scoped writes succeed but
    # are unresolvable by drawer/room queries.  Always inject it.
    config_dict["mempalace_config"] = {
        "data_dir": resolved_ddir,
        "mempalace_routing": {
            "routing.source": "chroma",
            "wing_map": _build_wing_map(profile),
        },
        "runtime": {"mempalace_home": resolved_ddir},
    }

    # --- hermes_default_config section -------------------------------------
    config_dict["hermes_default_config"] = mp_cfg.copy()

    # --- balance section (#209 working-state surface (b)): corpus-imbalance
    #      diagnostic.  OFF by default — enabling it makes on_pre_llm_call
    #      inject a single one-line note INSIDE the [MemChorus Memory Recall]
    #      block whenever the corpus is classified "all-lessons, no-current-work".
    #      Keys (spec §8, mirrored in corpus_balancer defaults):
    #        enabled             gate (OFF by default — opt-in)
    #        mode                "archive" suppresses the diagnostic for a
    #                            deliberately lessons-only profile
    #        project_min_ratio   M1 floor (fraction of corpus that must be
    #                            active-project working-state)
    #        settled_threshold   M2 ceiling (fraction that is settled
    #                            knowledge before the "past-dominant" flag)
    #        settled_wings       whitelist of "settled" wings
    #        settled_rooms       whitelist of "settled" rooms
    config_dict["balance"] = {
        "enabled": False,
        "mode": "",
        "project_min_ratio": 0.05,
        "settled_threshold": 0.50,
        "settled_wings": [
            "memchorus_learning",
            "memchorus_decisions",
            "memchorus_general",
            "self-improvement",
        ],
        "settled_rooms": [
            "lessons-learned",
            "corrections",
            "diary",
            "evolution-reports",
            "research-archival",
            "pattern-recognition",
        ],
    }

    return yaml.dump(config_dict, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Filesystem writers — idempotent, profile-aware
# ---------------------------------------------------------------------------

def _resolve_config_path(profile: str) -> Path:
    """Return the target config path for *profile*."""
    if profile == "default":
        return hermes_home() / "memchorus.yaml"
    dst = profile_home(profile) / "memchorus.yaml"
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

#: A plugin name is an identifier: starts with a letter, then letters, digits,
#: ``.``, ``_`` or ``-`` (e.g. ``memchorus``, ``mempalace-mcp``).  This is a
#: secondary guard — the primary rejection of YAML booleans/numbers/None is a
#: *type* check, because ``true`` would otherwise match this pattern as a string.
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")

#: Strings that are almost certainly a YAML 1.1 boolean-coercion artifact
#: (``true``/``false`` in any case), not a real plugin name.  A plugin literally
#: called "true" is indistinguishable from the config bug this guard exists to
#: catch (``import true``), so reject them case-insensitively (GH-#335).
_BOOL_LIKE_STRINGS = {"true", "false"}


def validate_enabled_entries(entries) -> Tuple[List[str], List[object]]:
    """Split a raw ``plugins.enabled`` list into valid plugin names and the
    entries that are not.

    A valid entry is a non-empty string that matches :data:`_PLUGIN_NAME_RE`
    and is not a boolean-like word (``true``/``false``, any case).  Everything
    else — bare YAML booleans, numbers, ``None``, nested collections, and empty
    / whitespace-only strings — is invalid.  These are YAML *type* errors, not
    plugin names, and loading one as a module (``import true``) is what raised
    the runtime error in GH-#335.

    Returns ``(valid, invalid)`` where ``valid`` is a list of clean plugin-name
    strings and ``invalid`` preserves the offending raw values (in order) for
    reporting.
    """
    valid: List[str] = []
    invalid: List[object] = []
    for entry in (entries or []):
        # bool must be tested before the ``str`` branch: it is a distinct YAML
        # type that must never be treated as a name, even though ``True``
        # stringifies to ``"true"`` which would pass the pattern above.
        if isinstance(entry, bool):
            invalid.append(entry)
        elif not isinstance(entry, str):
            invalid.append(entry)
        else:
            stripped = entry.strip()
            if (
                stripped
                and stripped.lower() not in _BOOL_LIKE_STRINGS
                and _PLUGIN_NAME_RE.fullmatch(stripped)
            ):
                valid.append(stripped)
            else:
                invalid.append(entry)
    return valid, invalid


def _coerce_enabled_list(raw) -> List[object]:
    """Normalise a raw ``plugins.enabled`` value to a list of entries.

    Handles the three shapes the config may take: a proper list, a bare scalar
    (``enabled: true`` / ``enabled: false`` / ``enabled: memchorus``), or an
    absent / null value.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]


def _resolve_hermes_config(profile: str) -> Path:
    if profile == "default":
        return hermes_home() / "config.yaml"
    return profile_config_path(profile)


def enable_plugin(profile: Optional[str] = None) -> bool:
    """Ensure ``memchorus`` appears in the ``plugins.enabled`` list.

    The existing ``enabled`` list is validated before anything is written
    (GH-#335): bare YAML booleans, numbers, ``None``, and empty strings are
    rejected and dropped — only clean plugin-name strings are kept.  A bare
    boolean such as ``enabled: true`` is normalised to a proper list instead
    of being treated as a single (unresolvable) entry.

    Returns ``True`` if the file was modified, ``False`` if the plugin is
    already correctly enabled (idempotent, file left untouched).
    """
    profile = (profile
               or os.environ.get("HERMES_KANBAN_PROFILE")
               or "default")

    cfg_path = _resolve_hermes_config(profile)
    if not cfg_path.exists():
        print(f"[info] skipping plugin enable — {cfg_path} not found",
              file=sys.stderr)
        return False

    text = cfg_path.read_text()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        print(f"[error] {cfg_path} is not valid YAML: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not isinstance(data, dict):
        data = {}

    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}

    raw_enabled = plugins.get("enabled")
    entries = _coerce_enabled_list(raw_enabled)
    valid, invalid = validate_enabled_entries(entries)

    for bad in invalid:
        print(f"[warn] plugins.enabled: rejecting {bad!r} — not a valid "
              f"plugin name (GH-#335)", file=sys.stderr)

    already = "memchorus" in valid
    if not already:
        valid.append("memchorus")

    if already and not invalid:
        print(f"[skip] memchorus already enabled in {cfg_path}")
        return False

    plugins["enabled"] = valid
    data["plugins"] = plugins
    new_text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

    tmp = cfg_path.with_suffix(".tmp")
    try:
        tmp.write_text(new_text)
        os.replace(tmp, cfg_path)
    except OSError as exc:
        print(f"[error] failed to update {cfg_path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"[ok] memchorus enabled in plugins.enabled in {cfg_path}")
    return True


# ---------------------------------------------------------------------------
# CLI entry-point (wired in setup.py)
# ---------------------------------------------------------------------------

def _self_repair_launchers() -> None:
    """Best-effort self-repair of console-script shebangs (GH #306).

    On some boxes, ``pip install`` leaves the console scripts
    (``memchorus-init``, ``memchorus-doctor``, ``memchorus-recall``, ...)
    with a launch line naming a nonexistent doubled interpreter path
    (``.../venv/bin/.../venv/bin/python``), so they fail with
    ``No such file or directory``.  ``memchorus-init`` is the first of
    those scripts to be run after install, so it is the natural moment to
    self-heal — and it is running, which makes its own ``sys.executable``
    the correct interpreter to write into every launcher in that venv.

    Best-effort by contract: any failure here is printed (stderr) and
    swallowed — the config-generation flow must never be blocked by it.
    """
    try:
        from memchorus.shebang_repair import repair_entrypoints
    except Exception as exc:  # noqa: BLE001 — self-repair must never block init
        print(f"[init] launcher self-repair skipped: {exc}", file=sys.stderr)
        return
    try:
        results = repair_entrypoints()
        for r in results:
            if r.action == "repaired":
                print(
                    f"[init] repaired launcher shebang: {r.name}: "
                    f"{r.found_interpreter} -> {r.set_interpreter}"
                )
    except Exception as exc:  # noqa: BLE001 — self-repair must never block init
        print(f"[init] launcher self-repair skipped: {exc}", file=sys.stderr)


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
    parser.add_argument("--migrate", metavar="ROOT", default=None,
                        help="Re-point this profile's reader config: rewrite --palace ROOT → "
                             "the canonical leaf (pair with -p <profile>; --dry-run to preview)")  # noqa: E501

    args = parser.parse_args()
    profile = args.profile or os.environ.get("HERMES_KANBAN_PROFILE") or "default"

    # GH #306 — best-effort self-repair of console-script launchers whose
    # launch line names a nonexistent (doubled) interpreter path.  This runs
    # under whatever interpreter owns the venv we're installing into, so it
    # repairs exactly the scripts in that venv's bin dir.  Best-effort by
    # contract: a broken launcher must never block config generation, and a
    # repair failure must never fail an otherwise successful init.
    if not args.dry_run:
        _self_repair_launchers()

    if args.migrate is not None:
        # --migrate ROOT  →  re-point this profile's reader config at the
        # canonical leaf.  Delegates to :func:`palace_path.migrate` — the
        # same module the doctor and reader share — so the "single
        # canonical answer" guarantee is preserved (see palace_path for
        # the full contract).
        #
        # The ``--palace`` token may live in the profile's ``memchorus.yaml``
        # (auto_init writer config) OR in the hermes ``config.yaml`` mcp_servers
        # args (where the reader is actually booted).  Re-point both candidate
        # files and report whichever matched.
        root = Path(args.migrate)
        candidates = [
            _resolve_config_path(profile),            # profile memchorus.yaml
            _resolve_hermes_config(profile),          # mcp_servers config.yaml
        ]
        rewrote_any = False
        for path in candidates:
            if not path.exists():
                continue
            result = palace_path.migrate(
                root,
                config_path=path,
                dry_run=args.dry_run,
            )
            if not result.needs_repoint:
                print(f"[skip] {root} is already canonical — nothing to re-point")
                break
            if result.repointed_configs:
                prefix = "[dry-run] would " if args.dry_run else "[ok] "
                for repointed in result.repointed_configs:
                    print(f"{prefix}re-pointed {root} → {result.canonical} in {repointed}")
                rewrote_any = True
        else:
            # exhausted the for-loop without a single [skip]
            if not rewrote_any:
                canonical = palace_path.palace_data_dir(root)
                if canonical != root:
                    print(
                        f"[info] no '--palace {root}' token in this profile's "
                        f"config — point at the canonical leaf directly: "
                        f"--palace {canonical}"
                    )
                else:
                    print(f"[skip] {root} is already canonical — nothing to re-point")
        return 0

    # ── Issue #283 item 2: one-time move of legacy out-of-home state ──────
    # Runs before write_config so the canonical dir is populated first,
    # then the new config is written pointing at the same dir.
    from memchorus.state_migrate import migrate_profile_state
    try:
        migration = migrate_profile_state(profile=profile, dry_run=args.dry_run)
        if migration.already_migrated:
            print("[info] state already migrated — nothing to move")
        if migration.moved:
            for src, dst in migration.moved:
                prefix = "[dry-run] would move " if args.dry_run else "[ok] moved "
                print(f"{prefix}{src} → {dst}")
        if migration.config_rewritten:
            for cfg in migration.config_rewritten:
                print(f"[ok] legacy config re-pointed: {cfg}")
        if migration.preserved:
            for item in migration.preserved:
                print(f"[warn] preserved (not moved): {item}")
    except Exception as exc:
        # Migration is best-effort on first run — never block config write.
        print(f"[warn] state migration skipped: {exc}", file=sys.stderr)

    if not args.dry_run:
        write_config(profile=profile, data_dir=args.data_dir)
        if not args.disable_plugin:
            enable_plugin(profile=profile)
    else:
        print(generate_config(profile=profile, data_dir=args.data_dir))

    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
