"""Self-repair for malformed console-script launchers (GH #306).

Some install pipelines write MemChorus's console scripts (``memchorus-doctor``,
``memchorus-recall``, ...) with a **doubled interpreter path** — either as a
direct Python shebang or inside the ``#!/bin/sh`` long-path wrapper (the
``uv``/``pip``-style launcher fallback) whose ``exec`` line references a
doubled ``.../venv/bin/.../venv/bin/python``.  The script file exists but
cannot launch::

    <venv>/bin/memchorus-recall: line 2: <venv>/bin/<venv>/bin/python:
    No such file or directory

This module detects and rewrites such launchers, using the interpreter that
*owns the bin directory* (or the interpreter running this repair, by
default) as the single source of truth.

Contract
--------
* **Byte-surgical.**  Only the launch line (line 1 for a direct shebang, the
  ``exec`` line for a shell wrapper) is rewritten; every other byte —
  including the closing ``' '''`` line and the module body — is preserved.
* **Conservative.**  A launcher is only rewritten when its interpreter token
  is missing (the #306 doubled path), or when it is an absolute path that
  resolves to a Python outside the bin directory being repaired.  Healthy
  launchers — including ``uv`` relocatable wrappers whose interpreter is a
  ``$(dirname "$0")`` expression — are left byte-identical.
* **Mode-preserving.**  The executable bit is retained (the rewrite truncates
  and rewrites the same inode; no symlink dance, no copy/rename).

:func:`repair_entrypoints` is the public entry; the doctor wires it to
``memchorus-doctor --repair`` and ``memchorus-init`` calls it best-effort
after ``pip install`` so the documented install flow self-heals.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# Console scripts owned by this distribution (pyproject [project.scripts]).
MEMCHORUS_SCRIPT_NAMES: Tuple[str, ...] = (
    "memchorus-doctor",
    "memchorus-init",
    "memchorus-recalibrate",
    "memchorus-recall",
    "mempalace-mcp",
)

# The ``exec`` launch line of a ``#!/bin/sh`` wrapper, e.g.
#   '''exec' ~/.hermes/installs/<id>/environments/<env>/venv/bin/python "$0" "$@"
#   '''exec' "$(dirname -- "$(realpath -- "$0")")"/'python' "$0" "$@"  (uv 0.13.x relocatable)
#   '''exec' "<venv>/bin/python" "$0" "$@"                             (distlib style)
# Anchored at the canonical ``"$0" "$@"`` argument tail so the token group
# keeps the full relocatable expression (nested quotes included).
_EXEC_LINE_RE = re.compile(
    r"""^\s*(?:['\"]{1,3})?exec['\"]?\s+"""
    r"""(?P<token>.+?)\s+"""
    r"""(?P<args>["']\$0["']\s+["']\$@["'])\s*$"""
)

_SHELL_INTERPRETERS = {
    "/bin/sh", "/bin/bash", "/bin/dash", "/bin/ash",
    "/usr/bin/sh", "/usr/bin/bash",
}


@dataclass
class LauncherInfo:
    """Classification of one console-script file's launch line."""

    path: Path
    kind: str                      # "python-shebang" | "sh-wrapper" | "unreadable" | "unrecognized"
    interpreter: Optional[str]     # the raw interpreter token, verbatim (quotes stripped)
    ok: bool                       # True when the token resolves to an existing executable
    note: str = ""


@dataclass
class RepairResult:
    """Outcome for one named script under a bin directory."""

    name: str
    path: str
    action: str                    # "ok" | "repaired" | "unrecognized" | "unreadable" | "missing"
    found_interpreter: Optional[str]
    set_interpreter: Optional[str] = None

    def describe(self) -> str:
        if self.action == "repaired":
            return (
                f"[ok] repaired {self.name} ({self.path}): "
                f"{self.found_interpreter} -> {self.set_interpreter}"
            )
        if self.action == "ok":
            return f"[ok] {self.name} launcher healthy — no change"
        return (
            f"[warn] {self.name} ({self.path}) not repaired "
            f"(action={self.action}; found={self.found_interpreter})"
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def resolve_token(token: Optional[str], script: Path) -> Optional[Path]:
    """Resolve a launcher's interpreter token to a candidate path.

    Plain tokens pass through (user-expanded).  Relocatable expressions —
    the ``$(dirname ...)`` / ``$0`` / ``$BASH_SOURCE`` shapes that ``pip``
    and ``uv`` use for relocatable launchers, including the nested
    ``$(dirname -- "$(realpath -- "$0")")`` form — are resolved against
    the script's own directory: the interpreter must sit alongside the
    script.  Returns ``None`` when the token is not statically resolvable;
    callers treat such tokens as healthy (never rewrite what cannot be
    judged).
    """
    if not token:
        return None
    if "$" not in token:
        return Path(token).expanduser()
    if "$(" not in token and "$0" not in token and "$BASH_SOURCE" not in token:
        return None
    rest = re.sub(r"\$\(.*\)", "", token)
    rest = re.sub(r"\$(0|BASH_SOURCE)", "", rest)
    # uv embeds the binary name with mixed shell quoting (e.g. ``"/'python'``),
    # so drop *every* quote char, not just the leading/trailing ones.
    rest = re.sub(r"['\"]", "", rest).strip()
    parts = [part for part in rest.split("/") if part]
    return script.parent / parts[-1] if parts else None


def _token_ok(token: Optional[str], script: Path) -> bool:
    """True when the interpreter token is usable as-is."""
    if not token:
        return False
    if token in _SHELL_INTERPRETERS:
        # The *wrapper* interpreter; usability is decided by the exec token.
        return True
    if "$" in token:
        resolved = resolve_token(token, script)
        if resolved is None:
            # Unresolvable shell expression — conservative: healthy.
            return True
        return resolved.exists()
    p = Path(token).expanduser()
    if not p.is_absolute():
        # ``#!python`` / ``#!env python`` style — cannot be judged without
        # resolving PATH; treat as healthy (pip's fix_script owns that case).
        return True
    return p.exists()


def classify(script: Path) -> LauncherInfo:
    """Classify one console-script file without modifying it."""
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return LauncherInfo(script, "unreadable", None, False, str(exc))

    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].startswith("#!"):
        return LauncherInfo(script, "unrecognized", None, False, "no shebang line")

    head = lines[0][2:].strip()
    parts = head.split(None, 1)
    interp = parts[0] if parts else None

    if interp is None:
        return LauncherInfo(script, "unrecognized", None, False, "empty shebang")

    if interp in _SHELL_INTERPRETERS or interp.endswith(("/sh", "/bash", "/dash")):
        # Shell wrapper: find the exec line among the next few lines.
        for line in lines[1:5]:
            m = _EXEC_LINE_RE.match(line.strip())
            if m:
                token = m.group("token").strip("\"'")
                return LauncherInfo(
                    script, "sh-wrapper", token, _token_ok(token, script)
                )
        return LauncherInfo(
            script, "sh-wrapper", None, False,
            "shell wrapper without a recognizable exec line",
        )

    return LauncherInfo(
        script, "python-shebang", interp, _token_ok(interp, script)
    )


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def repair_script(script: Path, interpreter: str) -> bool:
    """Rewrite only the malformed launch line of *script*.

    Returns ``True`` when the file was rewritten, ``False`` when its launch
    line was not recognizable enough to rewrite safely (the file is left
    untouched).
    """
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return False
    info = classify(script)

    new_lines = text.splitlines(keepends=True)
    if info.kind == "python-shebang" and new_lines:
        new_lines[0] = f"#!{interpreter}\n"
    elif info.kind == "sh-wrapper":
        for i, line in enumerate(new_lines[:5]):
            if _EXEC_LINE_RE.match(line.strip()):
                # Rewrite the exec line wholesale in the canonical wrapper
                # form; the closing '' ''' line and the body stay untouched.
                new_lines[i] = f"'''exec' \"{interpreter}\" \"$0\" \"$@\"\n"
                break
        else:
            return False
    else:
        return False

    script.write_bytes("".join(new_lines).encode("utf-8"))
    return True


def _script_should_be_repointed(info: LauncherInfo, bin_dir: Path,
                                interpreter: str) -> bool:
    """Decide whether *info* describes a launcher that must be rewritten."""
    if info.ok:
        # Healthy — but an absolute token pointing at a *different* venv's
        # Python is the "moved/rebuilt environment" breakage class: a script
        # living in this bin dir should run on this bin dir's interpreter.
        token = info.interpreter
        if token and os.path.isabs(token):
            try:
                same = Path(token).resolve() == Path(interpreter).resolve()
            except OSError:
                same = False
            if not same and Path(token).parent != bin_dir:
                return True
        return False
    return info.kind in ("python-shebang", "sh-wrapper", "unrecognized")


def repair_entrypoints(
    bin_dir: Optional[Path] = None,
    interpreter: Optional[str] = None,
    script_names: Sequence[str] = MEMCHORUS_SCRIPT_NAMES,
    dry_run: bool = False,
) -> List[RepairResult]:
    """Repair MemChorus console-script launchers in one bin directory.

    Parameters
    ----------
    bin_dir:
        The ``venv/bin`` (or ``venv/Scripts``) directory to repair.
        Defaults to the bin directory of the interpreter running this
        repair (``sys.executable``).
    interpreter:
        The absolute Python path to write into the launchers.  Defaults to
        ``<bin_dir>/python`` when that file exists, else ``sys.executable``.
    script_names:
        Names to consider (defaults to this distribution's console scripts).
    dry_run:
        Classify and report without writing.

    Returns one :class:`RepairResult` per script found (a script missing
    from the bin dir yields ``action="missing"`` and no write).
    """
    if bin_dir is None:
        bin_dir = Path(sys.executable).parent
    bin_dir = Path(bin_dir)
    if interpreter is None:
        cand = bin_dir / "python"
        interpreter = str(cand) if cand.exists() else sys.executable

    results: List[RepairResult] = []
    for name in script_names:
        p = bin_dir / name
        if not p.exists():
            results.append(RepairResult(name, str(p), "missing", None, None))
            continue
        info = classify(p)
        if not _script_should_be_repointed(info, bin_dir, interpreter):
            results.append(
                RepairResult(name, str(p), "ok", info.interpreter, None)
            )
            continue
        if dry_run:
            results.append(
                RepairResult(name, str(p), "unrecognized",
                             info.interpreter, interpreter)
            )
            results[-1].action = "dry-run"
            continue
        rewritten = repair_script(p, interpreter)
        results.append(
            RepairResult(
                name,
                str(p),
                "repaired" if rewritten else "unrecognized",
                info.interpreter,
                interpreter if rewritten else None,
            )
        )
    return results


def repair_entrypoints_verbose(
    bin_dir: Optional[Path] = None,
    interpreter: Optional[str] = None,
    dry_run: bool = False,
) -> List[RepairResult]:
    """:func:`repair_entrypoints` with a human-readable line per script."""
    results = repair_entrypoints(
        bin_dir=bin_dir, interpreter=interpreter, dry_run=dry_run
    )
    for r in results:
        if r.action in ("repaired", "dry-run"):
            prefix = "[dry-run] would repair " if dry_run else ""
            print(f"{prefix}{r.name}: {r.found_interpreter} -> {r.set_interpreter}")
        elif r.action not in ("missing",):
            print(f"[ok] {r.name} launcher healthy — no change")
    return results
