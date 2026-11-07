#!/usr/bin/env python3
"""Issue #269 — assert the test suite wrote nothing into the operator's real home.

Used by CI to prove hermetic isolation end-to-end:

  1. ``snapshot`` — capture a bounded record of the real home tree (top two
     levels: paths + mtime_ns + size) *before* pytest runs.
  2. Run the test suite (which, per ``tests/conftest.py``, redirects all home
     resolution into a private temp root).
  3. ``check`` — re-scan the same tree and fail with a non-zero exit if ANY file
     or directory was created or modified under the real home by the run.

The scan is intentionally bounded (top two levels of the real home) so it is
fast on a CI runner yet still wide enough to catch the #269 leak class
(``~/.memchorus-data``, ``~/.mempalace``, ``~/.config/hermes``, ``~/.cache``,
``~/.hermes`` top level). Deeper, unrelated churn elsewhere is out of scope
for this gate and deliberately not flagged.

Exit codes: 0 = clean (no leak), 1 = leak detected, 2 = usage / IO error.

The state file is plain JSON (no third-party deps) so it works identically on
Linux and Windows CI runners without requiring ``find`` / ``PowerShell``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _exclusion_prefixes() -> list[Path]:
    """Roots under the real home that are CI harness scratch, not operator data.

    On GitHub-hosted runners the checkout (``$GITHUB_WORKSPACE``) and the
    scratch dir (``$RUNNER_TEMP``) both live under ``$HOME/work`` (``work/``
    on ``ubuntu``) or under the runner's temp tree (``windows``). ALL CI
    activity — pip cache, pytest tmp, git metadata, and this very gate's own
    state file — lands there, so its churn is not evidence that the *test
    suite* leaked into the operator's data home (``~/.hermes``,
    ``~/.memchorus-data``, ``~/.mempalace``, ...). Excluding these roots keeps
    the gate meaningful (it still guards every operator *data* dir) without
    flagging the runner's own scaffolding.

    We always also exclude ``$HOME/work`` — on ubuntu that *is* the runner
    workspace, and the ``work/_temp`` churn in the #269 CI log came from
    there. Resolution is best-effort: anything missing / non-directory is
    skipped, so this is a no-op on a plain local run (neither env var is set).
    """
    prefixes: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            p = p.resolve()
        except OSError:
            return
        if p not in seen and p.is_dir():
            seen.add(p)
            prefixes.append(p)

    for var in ("GITHUB_WORKSPACE", "RUNNER_TEMP"):
        raw = os.environ.get(var)
        if raw:
            _add(Path(raw))
    # $HOME/work is the runner workspace on ubuntu and is *inside* the home
    # tree we scan — exclude it explicitly.
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if home:
        _add(Path(home) / "work")
    return prefixes


def _in_exclusions(candidate: Path, prefixes: list[Path]) -> bool:
    if not prefixes:
        return False
    c = candidate.resolve()
    for prefix in prefixes:
        try:
            c.relative_to(prefix)
            return True
        except ValueError:
            continue
    return False


def _scan(real_home: Path, max_depth: int, exclude: list[Path] | None = None) -> dict:
    """Record {relative_posix_path: [mtime_ns, size]} for the bounded tree.

    Directories are recorded under a ``\x00``-suffixed key (empty size) so a
    newly-created directory is distinguishable from a newly-created file.

    ``exclude`` (list of resolved roots) is subtracted out: any path that lives
    under one of those roots is neither recorded nor descended into. Used to
    keep the CI harness scratch dir out of the leak scan (see
    :func:`_exclusion_prefixes`).
    """
    record: dict[str, list] = {}
    real_home = real_home.resolve()
    if not real_home.is_dir():
        return {}
    exclude = exclude or []
    # Iterative BFS bounded to ``max_depth`` levels below ``real_home``.
    frontier: list[Path] = [real_home]
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        next_frontier: list[Path] = []
        for base in frontier:
            try:
                items = list(base.iterdir())
            except (PermissionError, OSError):
                continue
            for it in items:
                if _in_exclusions(it, exclude):
                    continue
                rel = it.relative_to(real_home).as_posix()
                try:
                    st = it.stat()
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                if it.is_dir():
                    if depth < max_depth:
                        next_frontier.append(it)
                    record[rel + "\x00"] = [st.st_mtime_ns, 0]
                else:
                    record[rel] = [st.st_mtime_ns, st.st_size]
        frontier = next_frontier
    return record


def cmd_snapshot(args: argparse.Namespace) -> int:
    real_home_str = args.home or os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if not real_home_str:
        print("[#269] ERROR: no home to snapshot (set HOME/USERPROFILE or pass --home)", file=sys.stderr)
        return 2
    real_home = Path(real_home_str)
    exclude = _exclusion_prefixes()
    scan = _scan(real_home, args.max_depth, exclude)
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "real_home": str(real_home),
        "max_depth": args.max_depth,
        "entries": scan,
    }
    with state_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    print(f"[#269] snapshot: {len(scan)} entries under {real_home} -> {state_path}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    with state_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    prior: dict[str, list] = payload["entries"]

    real_home = Path(payload["real_home"])
    # Re-apply the same exclusion rules at check time so the before/after
    # comparison is apples-to-apples (harness scratch excluded in both scans).
    current = _scan(real_home, payload.get("max_depth", args.max_depth), _exclusion_prefixes())

    created = sorted(set(current) - set(prior))
    # A path is "modified" if its mtime moved forward (a re-write).
    modified = sorted(
        key for key in set(current) & set(prior)
        if current[key][0] > prior[key][0]
    )

    if not created and not modified:
        print(f"[#269] OK: no files created or modified under {real_home} during the run")
        return 0

    print(f"[#269] LEAK DETECTED under {real_home}:", file=sys.stderr)
    for p in created:
        label = "(dir)" if p.endswith("\x00") else ""
        print(f"  + created : {p.rstrip(chr(0))} {label}".rstrip(), file=sys.stderr)
    for p in modified:
        print(f"  ~ modified: {p.rstrip(chr(0))}", file=sys.stderr)
    print(
        "  The test suite wrote into the operator's real home. "
        "See tests/conftest.py (#269) for the HOME redirection the suite is "
        "supposed to install.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="path to the JSON state file")
    parser.add_argument("--home", help="optional: explicit real home to guard (default: $HOME / $USERPROFILE). Only used by the snapshot command; check always uses the home recorded at snapshot time.")
    parser.add_argument("--max-depth", type=int, default=2, help="levels below the real home to scan (default 2)")
    subparser = parser.add_subparsers(dest="cmd", required=True)
    subparser.add_parser("snapshot", help="record the pre-run tree")
    subparser.add_parser("check", help="fail if the tree changed")
    args = parser.parse_args(argv)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
