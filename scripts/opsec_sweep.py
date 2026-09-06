#!/usr/bin/env python3
"""OPSEC leak sweep — working-tree audit for absolute local paths, e-mail, and handles.

Purpose
-------
Catch the exact class of leak that the ``pre-commit-opsec-sweep`` skill targets,
while the offending text is still in the *working tree* (tracked, staged, unstaged,
or untracked) — i.e. BEFORE a single commit/push. History-based and merge-time
gates miss untracked/new files by construction; this sweep does not.

It searches a fixed, small, **auditable** set of leak patterns over:

  1. tracked files    (``git ls-files`` — respects .gitignore, skips .venv/dist)
  2. untracked files  (``git status --porcelain`` ``??`` — the gap history gates miss)

and reports every hit. Hits are classified by severity (see below) and the
process exits non-zero only on *hard* leaks, so it can run as a CI check and as a
local pre-push gate from the ``post-commit-push-verification`` flow.

Severity model
--------------
Two tiers. The distinction matters: a gate that "fails" on a ``~/.hermes`` docstring
or a ``cthugha`` test fixture would have failed every prior PR, so those are
*reported* (WARN), not failed. The real leak class — an operator's absolute local
home path (``/home/<user>/...``) and e-mail handles — is a HARD failure.

  HARD (fails the gate):
    abs_home   /home/<lowercase|digit>...        operator's real local path (the incident)
    bubo_at    bubo@                             operator e-mail, local-part form
    at_bubo    @bubo                             operator e-mail, domain form
    at_gmail   @gmail                            operator e-mail domain

  WARN (reported, does not fail — use ``--strict`` in deep review to promote):
    tilde_hermes  ~/.hermes                      ``~``-escaped home-escaped convention (portable)
    cthugha       standalone agent-name token     typically a profile-name test fixture / OPSEC self-assertion
    bubo_dot      bubo.<word>                     handle-adjacent fixture string

  The two legitimate synthetic test-fixture absolute paths in the tree are
  allowlisted below (``--list-allow`` dumps the full set). Everything outside the
  allowlist that matches a HARD pattern fails the gate.

Pass rule
---------
  * 0 un-allowlisted HARD hits across tracked + untracked files  ->  exit 0 (clean)
  * >=1 un-allowlisted HARD hit                                  ->  exit 1 (leak)
  * WARN hits are always printed (count + first N) for audit; they do not affect exit code

Traps handled
-------------
* ``.venv`` / ``.git`` / build dirs are excluded (not in ``git ls-files``; gitignored
  ``??`` entries are filtered). Their ``/home/<user>`` shebang/env refs are machine-
  local and should never be gate input.
* Untracked files that are *also* gitignored are skipped (``??`` already excludes them).
* Binary / non-UTF-8 files decode with ``errors='ignore'`` so a stray .png never crashes the sweep.
* Cross-platform: pure ``git`` + re, no bash. Runs on ubuntu + windows runners as-is.

Usage
-----
  python scripts/opsec_sweep.py               # default gate (HARD only fails); CI / pre-push
  python scripts/opsec_sweep.py --strict      # promote WARN (tilde_hermes / cthugha / bubo_dot) to HARD
  python scripts/opsec_sweep.py --json        # machine-readable (post-commit-push-verification)
  python scripts/opsec_sweep.py --verbose     # also list suppressed/allowlisted hits
  python scripts/opsec_sweep.py --list-allow  # dump the allowlist and exit
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 2_000_000  # skip anything bigger than ~2 MB (no large tracked source in this repo)


# --------------------------------------------------------------------------- #
# Pattern catalog
# --------------------------------------------------------------------------- #
# (name, compiled regex, tier, human-readable why)
#   tier == "hard"  -> un-allowlisted hit FAILS the gate
#   tier == "warn"  -> hit is reported; --strict promotes it to a failure
PATTERNS: List[Tuple[str, "re.Pattern[str]", str, str]] = [
    ("abs_home",     re.compile(r"/home/[a-z0-9]"),          "hard",
     "absolute local home path — operator's real machine path (the leak class)"),
    ("bubo_at",      re.compile(r"bubo@"),                   "hard",
     "operator e-mail, local-part form (bubo@...)"),
    ("at_bubo",      re.compile(r"@" + r"bubo"),             "hard",
     "operator e-mail, domain form (@bubo...)"),
    ("at_gmail",     re.compile(r"@" + r"gmail"),            "hard",
     "operator e-mail domain (@gmail...)"),
    ("tilde_hermes", re.compile(r"~/\.hermes"),              "warn",
     "``~``-escaped hermes home convention (portable, never a machine-specific absolute)"),
    ("cthugha",      re.compile(r"\bcthugha\b"),             "warn",
     "agent-name token — typically a profile-name test fixture or an OPSEC self-assertion"),
    ("bubo_dot",     re.compile(r"bubo\."),                  "warn",
     "handle-adjacent fixture string (e.g. a profile-name test case like 'bubo.config')"),
]

# Directory names that, if present, we never gate against (defensive; they should
# not enter the tracked/untracked set anyway, but a stray one must not flip the gate).
SKIP_DIR_NAMES = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".mypy_cache"}


# --------------------------------------------------------------------------- #
# Allowlist — synthetic / test-fixture sites that are NOT real operator PII
# --------------------------------------------------------------------------- #
# Each entry: (file_suffix, pattern_name, reason). A hit on (file ending with file_suffix)
# for pattern_name is suppressed (reported only when --verbose). Use pattern_name="*" to
# suppress ALL patterns for that file — appropriate only for the gate script itself, which
# must be allowed to name its own catalog and fixture examples without tripping on them.
#
# We keep this *minimal* and *reasoned* on purpose — that is what makes the gate
# auditable. Every added entry should be a genuine synthetic/fixture use, never a
# real operator path that "conveniently" matched.
ALLOW: List[Tuple[str, str, str]] = [
    ("src/tests/test_behavioral_trigger_regression.py", "abs_home",
     "synthetic traceback example (/home/user/.local/...) — a placeholder path, not operator PII"),
    ("tests/test_project_record_schema.py", "abs_home",
     "synthetic doc-path fixture (/home/x.md) — exercises absolute-path validation, not a real location"),
    # Documentation placeholders — install/example paths use the generic ``/home/user`` and
    # ``/home/x`` as *any-user* stand-ins (the README even shows ``/home/user/.hermes/...``
    # as a copy-paste command shape). These are not a specific operator's machine. The real
    # leak class remains gated: any *other* ``/home/<name>`` (e.g. the incident's
    # ``/home/bubo``) is NOT in this allowlist and still fails the gate.
    ("README.md", "abs_home",
     "install-doc example commands use the generic ``/home/user`` placeholder, not an operator path"),
    ("README.md", "at_gmail", "names the pattern the gate catches (self-referential doc)"),
    ("README.md", "bubo_at", "names the pattern the gate catches (self-referential doc)"),
    ("README.md", "at_bubo", "names the pattern the gate catches (self-referential doc)"),
    ("CHANGELOG.md", "at_gmail", "names the pattern the gate catches (self-referential doc)"),
    ("CHANGELOG.md", "bubo_at", "names the pattern the gate catches (self-referential doc)"),
    ("CHANGELOG.md", "at_bubo", "names the pattern the gate catches (self-referential doc)"),
    ("docs/REQUIREMENTS.md", "abs_home",
     "``--palace /home/x/...`` example — ``x`` is a placeholder username, not operator PII"),
    # The gate script itself: it must NAME its pattern catalog and its own allow-reasons
    # (bubo@, @bubo, @gmail, cthugha, /home/user, the file paths in ALLOW) to describe what
    # it searches for. Allowing its self-references is not a leak — it is the tool talking
    # about its own rules. (A leak is a code/data path that *points* at an operator machine;
    # a tool documenting that class is not one.)
    ("scripts/opsec_sweep.py", "*",
     "the OPSEC sweep script names its own pattern catalog + allow-reasons (self-referential docs)"),
]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Hit:
    file: str
    line_no: int
    pattern: str
    tier: str            # hard / warn
    snippet: str
    allowlisted: bool = False
    allow_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line_no,
            "pattern": self.pattern,
            "tier": self.tier,
            "snippet": self.snippet,
            "allowlisted": self.allowlisted,
            "allow_reason": self.allow_reason,
        }


@dataclass
class Report:
    root: str
    strict: bool
    files_scanned: int = 0
    files_skipped: List[str] = field(default_factory=list)
    hits: List[Hit] = field(default_factory=list)

    @property
    def hard_failures(self) -> List[Hit]:
        if self.strict:
            # --strict promotes every un-allowlisted hit (hard OR warn) to a failure
            return [h for h in self.hits if not h.allowlisted]
        return [h for h in self.hits if h.tier == "hard" and not h.allowlisted]

    @property
    def clean(self) -> bool:
        return not self.hard_failures


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #
def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.decode("utf-8", errors="ignore")


def _is_skipped_dir(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    return any(p in SKIP_DIR_NAMES for p in parts)


def tracked_files() -> List[str]:
    """Tracked, non-ignored files (respects .gitignore, excludes .venv/dist/etc.)."""
    out = _git("ls-files", "-z")
    files = [f for f in out.split("\0") if f]
    return [f for f in files if not _is_skipped_dir(f)]


def untracked_files() -> List[str]:
    """Untracked, non-ignored files — the gap history-based gates miss."""
    out = _git("status", "--porcelain")
    files: List[str] = []
    for line in out.splitlines():
        if not line.startswith("??"):
            continue
        path = line[2:].strip()
        # `git status` may quote paths with special chars; strip surrounding quotes
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path and not _is_skipped_dir(path):
            files.append(path)
    return files


# --------------------------------------------------------------------------- #
# Per-file scan
# --------------------------------------------------------------------------- #
def _is_allowlisted(rel: str, pattern: str) -> Optional[dict]:
    for file_suffix, pat, reason in ALLOW:
        file_match = rel == file_suffix or rel.endswith("/" + file_suffix) or rel.endswith(file_suffix)
        if file_match and (pat == "*" or pat == pattern):
            return {"pattern": pattern, "reason": reason}
    return None


def scan_file(rel: str, report: Report) -> None:
    abs_path = REPO_ROOT / rel
    try:
        if abs_path.is_dir() or abs_path.stat().st_size > MAX_FILE_BYTES:
            report.files_skipped.append(rel)
            return
        data = abs_path.read_bytes()
    except OSError as exc:
        report.files_skipped.append(f"{rel} (unreadable: {exc.__class__.__name__})")
        return

    report.files_scanned += 1
    text = data.decode("utf-8", errors="ignore")
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, rx, tier, _why in PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            snippet = line.strip()
            if len(snippet) > 120:
                start = max(0, m.start() - 30)
                snippet = ("…" if start > 0 else "") + line[start:start + 120].strip()
            allow = _is_allowlisted(rel, name)
            report.hits.append(Hit(
                file=rel,
                line_no=line_no,
                pattern=name,
                tier=tier,
                snippet=snippet,
                allowlisted=allow is not None,
                allow_reason=(allow or {}).get("reason", ""),
            ))


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(strict: bool, verbose: bool, json_out: bool) -> int:
    report = Report(root=str(REPO_ROOT), strict=strict)
    files = list(dict.fromkeys(tracked_files() + untracked_files()))
    for rel in files:
        scan_file(rel, report)

    if json_out:
        payload = {
            "clean": report.clean,
            "strict": report.strict,
            "root": report.root,
            "files_scanned": report.files_scanned,
            "files_skipped": report.files_skipped,
            "hard_failures": [h.to_dict() for h in report.hard_failures],
            # Machine consumers always see the full hit list (verbose is a text-mode concern)
            "hits": [h.to_dict() for h in report.hits],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_text(report, verbose)

    return 0 if report.clean else 1


def _print_text(report: Report, verbose: bool) -> None:
    hard = report.hard_failures
    warn = [h for h in report.hits if h.tier == "warn" and not h.allowlisted]
    allowed = [h for h in report.hits if h.allowlisted]

    if hard:
        print(f"OPSEC SWEEP — FAIL ({len(hard)} hard leak hit{'s' if len(hard) != 1 else ''})")
        for h in hard:
            print(f"  [{h.pattern}] {h.file}:{h.line_no}  {h.snippet}")
    else:
        print("OPSEC SWEEP — PASS (no hard operator-path / e-mail leaks)")

    if warn:
        print(f"  WARN  ({len(warn)} reported; not failing — promote with --strict for deep review)")
        shown = 0
        for h in warn:
            print(f"        [{h.tier}:{h.pattern}] {h.file}:{h.line_no}  {h.snippet}")
            shown += 1
            if shown >= 12:
                print(f"        … +{len(warn) - shown} more")
                break
    else:
        print("  WARN  (none)")

    if allowed and verbose:
        print(f"  allowlisted & suppressed ({len(allowed)}):")
        for h in allowed:
            print(f"        [{h.pattern}] {h.file}:{h.line_no}  -> {h.allow_reason}")

    print(f"  files scanned: {report.files_scanned}")
    if report.files_skipped and verbose:
        print(f"  files skipped: {report.files_skipped}")
    print(f"  mode: {'STRICT (warn->fail)' if report.strict else 'default (hard-only)'}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _list_allow() -> int:
    print("OPSEC sweep allowlist (suppressed, non-failing hits):")
    for file_suffix, pat, reason in ALLOW:
        print(f"  - {file_suffix}\n        pattern : {pat}\n        reason  : {reason}")
    print(f"  ({len(ALLOW)} entries)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="OPSEC working-tree leak sweep (tracked + untracked). "
                    "See module docstring for the severity model.",
    )
    p.add_argument("--strict", action="store_true",
                   help="promote WARN-tier hits (tilde_hermes/cthugha/bubo_dot) to failures")
    p.add_argument("--json", action="store_true", help="emit a JSON report instead of text")
    p.add_argument("--verbose", action="store_true", help="also list allowlisted/suppressed hits")
    p.add_argument("--list-allow", action="store_true", help="dump the allowlist and exit")
    args = p.parse_args(argv)

    if args.list_allow:
        return _list_allow()

    try:
        return run(strict=args.strict, verbose=args.verbose, json_out=args.json)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"opsec_sweep: git failed: {exc.stderr.decode('utf-8', errors='ignore')}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
