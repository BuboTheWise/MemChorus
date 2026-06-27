#!/usr/bin/env python3
"""Pre-flight parallel work check (check_merged_state.py)

Scans local git history to detect whether task-related commits, stale branches,
or branch divergence already exists before an agent starts implementing a Kanban task.

stdlib-only: subprocess, shlex, re, argparse, datetime, pathlib, sys.
No gitpython or other third-party dependencies.
"""

import argparse
import datetime
import json
import pathlib
import re
import shlex
import subprocess
import sys


# ── Subprocess helpers ──────────────────────────────────────────────

def _git(repo_path, *args):
    """Run a git command. stderr captured (never leaked)."""
    cmd = ["git", "-C", repo_path] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def parse_oneline_date(line):
    """Extract committer date from `git log --oneline` line. Returns naive UTC or None."""
    m = re.search(r"Date:\s*(\S+)", line)
    if not m:
        return None
    s = m.group(1)
    # Normalize missing seconds in timezone offsets like +08 -> +08:00
    if re.search(r"[+-]\d{2}$", s):
        s += ":00"
    try:
        dt = datetime.datetime.fromisoformat(s).astimezone(datetime.timezone.utc)
        return dt.replace(tzinfo=None)
    except Exception:
        return None


_WORDS_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")


def _tokenize(text):
    """Lowercase word tokens (alphabetic, numeric, hyphen)."""
    return set(_WORDS_RE.findall(text.lower()))


# ── check_task_against_commits ───────────────────────────────────────

def check_task_against_commits(task_keywords, git_repo_path):
    r"""Scan ``git log --oneline -50`` in *git_repo_path*, matching each keyword
    against recent commit messages. Returns a dict mapping keyword -> best match.

    Result shape per keyword::

        {"sha": "<8-char>", "message": "...", "date": "2026-06-15T...", "score": 0.75}

    score = fraction of keyword tokens found in commit message (simple recall).
    """
    repo = pathlib.Path(git_repo_path).resolve()
    if not (repo / ".git").exists():
        raise NotADirectoryError(f"Not a git repo: {repo}")

    out = _git(str(repo), "-c", "core.diffAlgo=minimal", "--no-pager", "log",
               "--date=short", "--format=%h|%ai|%s%n-", "-50")
    if out.returncode != 0:
        raise RuntimeError(f"git log failed: {out.stderr.strip()}")

    # Output format: "<sha>|<iso_date>|<message>\n-" (separator ensures no bleed-through)
    commits = []
    for raw in out.stdout.splitlines():
        if not raw or raw == "-":
            continue
        parts = raw.replace("\n", "").replace("\r", "").split("|", 2)
        if len(parts) < 3:
            continue
        sha = parts[0][:8]
        date_str_raw = parts[1].strip()
        msg = parts[2].strip().lstrip("-").lstrip(":").strip()

        # Parse ISO date to datetime
        ds_date = date_str_raw
        if re.search(r"[+-]\d{2}$", ds_date):
            ds_date += ":0"  # +08 -> +08:00 for fromisoformat
        try:
            date_dt = datetime.datetime.fromisoformat(ds_date)
        except ValueError:
            date_dt = None

        commits.append({"sha": sha, "message": msg, "date_dt": date_dt, "date_str": ds_date})

    # For each keyword find the best matching commit
    result = {}
    for kw in task_keywords:
        kw_tokens = _tokenize(kw)
        if not kw_tokens:
            result[kw] = {"sha": None, "message": "", "date": "", "score": 0.0}
            continue

        best_score = -1.0
        best_comm = None
        for c in commits:
            overlap = len(kw_tokens & _tokenize(c["message"]))
            score = float(overlap) / max(len(kw_tokens), 1)
            if score > best_score:
                best_score = score
                best_comm = c

        if best_comm and best_score > 0:
            date_iso = best_comm["date_dt"].isoformat() if best_comm["date_dt"] else "unknown"
            result[kw] = {
                "sha": best_comm["sha"],
                "message": best_comm["message"],
                "date": date_iso,
                "score": round(best_score, 4),
            }
        else:
            result[kw] = {"sha": None, "message": "", "date": "", "score": 0.0}

    return result


# ── find_stale_branches ────────────────────────────────────────────────

def find_stale_branches(repo_path, older_than_days=7):
    """Find branches whose last commit is older than *older_than_days* days.

    Returns a list of dicts with keys: branch, last_commit (8-chars), date, days_ago.
    Uses ``git for-each-ref`` to get accurate dates without relying on ``git branch``'s
    limited output.
    """
    repo = pathlib.Path(repo_path).resolve()
    # Use for-each-ref with NUL separators (%00) so branch names or dates
    # that contain '|' or spaces never break parsing.
    fmt = "%(refname:short)%00%(objectname:short)%00%(committerdate:iso8601-strict)"
    out = _git(str(repo), "for-each-ref", "--sort=-committerdate", f"--format={fmt}", "refs/heads/")
    if out.returncode != 0:
        raise RuntimeError(f"git for-each-ref failed: {out.stderr.strip()}")

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=older_than_days)
    stale = []

    _ZERO_PAD = re.compile(r'([+-])(\d{2}) (\d{2}):(\d{2})$')

    for line in out.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("%00")  # %00 is the NUL we inserted as separator
        if len(parts) != 3:
            continue
        branch_name = parts[0].strip()
        sha = parts[1].strip()
        ds = parts[2].strip()

        # Parse the ISO-8601 format from %committerdate:iso8601-strict.
        # Example: "2025-05-10 12:00:00 +0000" -- pad 2h offset to 02:00 as needed.
        if re.search(r'[+-]\d{2} (\d{2}):(\d{2})$', ds):
            ds = _ZERO_PAD.sub(r'\1\2 \3:\4', ds)  # "+02 12:30" -> "+02 12:30" already good? No, it might be "+00 12:00" need to ensure format
        try:
            s = ds.strip()
            if re.search(r'[+-]\d{2}$', s):
                s += ":00"
            if re.search(r'[+-]\d{4}$', s):
                s = s[:-2] + ":" + s[-2:]
            commit_dt = datetime.datetime.fromisoformat(s).astimezone(datetime.timezone.utc)
        except Exception:
            # Fallback: try parsing as-is with colons on timezone
            continue

        diff = now - commit_dt
        days_ago = diff.days
        if commit_dt < cutoff:
            stale.append({
                "branch": branch_name,
                "last_commit": sha,
                "date": ds,
                "days_ago": days_ago,
            })

    return stale


# ── compare_branch_to_master ─────────────────────────────────────────────────

def compare_branch_to_master(branch_name, repo_path):
    """Compare *branch_name* against master/main/master. Returns ahead/behind counts
    and mergeability status (dry-run merge).
    """
    repo = pathlib.Path(repo_path).resolve()

    # Determine target branch name (prefer 'main', fallback to 'master')
    remotes_out = _git(str(repo), "branch", "-a")
    remote_branches = remotes_out.stdout.lower().strip().splitlines()
    main_names = []
    for rb in remote_branches:
        stripped = rb.strip()
        if re.match(r"^  \*(?:master|main)\b", stripped):
            continue
        bname = stripped.lstrip("*+ ").split("/", 1)[-1].split(" ")[0] if stripped else ""
        if bname and (bname == "main" or bname == "master"):
            main_names.append(bname)

    target = "main" if "main" in main_names else ("master" if "master" in main_names else "master")

    # rev-list ahead/behind counts:
    #   ahead  = commits in branch but not in target     -> target..branch
    #   behind = commits in target but not in branch      -> branch..target
    ahead_out = _git(str(repo), "rev-list", "--count", f"{target}..{branch_name}")
    behind_out = _git(str(repo), "rev-list", "--count", f"{branch_name}..{target}")

    ahead = int(ahead_out.stdout.strip()) if ahead_out.returncode == 0 else None
    behind = int(behind_out.stdout.strip()) if behind_out.returncode == 0 else None

    # Mergeability: dry-run merge (--abort afterwards)
    merge_result = _git(
        str(repo), "merge", "--no-commit", "--no-ff", "-m", "_preflight-check",
        branch_name, target,
    )
    # Always abort regardless of outcome
    _git(str(repo), "merge", "--abort")

    mergeable = merge_result.returncode == 0
    merge_conflicts = []
    if not mergeable:
        unmerged_out = _git(str(repo), "diff", "--name-only", "--diff-filter=U")
        if unmerged_out.returncode == 0 and unmerged_out.stdout.strip():
            merge_conflicts = [f.strip() for f in unmerged_out.stdout.strip().splitlines()]

    return {
        "branch": branch_name,
        "target": target,
        "ahead": ahead,
        "behind": behind,
        "mergeable": mergeable,
        "conflicts": merge_conflicts,
    }


# ── CLI entry point ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pre-flight parallel work check -- scan git history for already-done work.",
    )
    parser.add_argument("--task", type=str, help='Task keywords as a string (e.g. "Implement feature X")')
    parser.add_argument("--keywords", nargs="+", default=None, help="Explicit keyword tokens to match")
    parser.add_argument("--repo", type=str, required=True, help="Path to local git repo")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output results as JSON and skip printing.")
    args = parser.parse_args()

    # Validate repository
    repo_git = pathlib.Path(args.repo) / ".git"
    if not repo_git.exists():
        print(f"ERROR: {args.repo} is not a git repository.", file=sys.stderr)
        sys.exit(1)

    output = {}

    # 1. Task keyword matching
    if args.task or args.keywords:
        ks = list(_tokenize(args.task)) if args.task else (list(set(args.keywords)) if args.keywords else [])
        matched = check_task_against_commits(ks, args.repo)
        output["matched_commits"] = matched

    # 2. Stale branches
    stale = find_stale_branches(args.repo, older_than_days=7)
    output["stale_branches"] = stale if stale else []

    # 3. Branch divergence (all non-current branches vs main/master)
    all_branches_out = _git(args.repo, "branch", "--format=%(refname:short)")
    current_out = _git(args.repo, "rev-parse", "--abbrev-ref", "HEAD")
    current = current_out.stdout.strip() if current_out.returncode == 0 else ""

    ahead_behind_results = []
    for b in all_branches_out.stdout.strip().splitlines():
        bn = b.strip()
        if not bn or bn == current:
            continue
        cb = compare_branch_to_master(bn, args.repo)
        ahead_behind_results.append(cb)
    output["ahead_behind"] = ahead_behind_results

    if args.as_json:
        print(json.dumps(output, indent=2))
    else:
        _pprint_output(output)


def _pprint_output(output):
    """Pretty-print the raw output dict."""
    # Task keyword matches
    mc = output.get("matched_commits", {})
    if mc:
        print("\n=== Commit Matches for Task Keywords ===")
        for kw, info in mc.items():
            if info["date"]:
                print(f"  Keyword: {kw}")
                print(f"    sha:   {info['sha']}")
                print(f"    msg:   {info['message']}")
                print(f"    date:  {info['date']}")
                print(f"    score: {info['score']:.2f}")
            else:
                print(f"  Keyword '{kw}': no matching commit found.")
            print()

    # Stale branches
    stale = output.get("stale_branches", [])
    if stale:
        print("\n=== Warning: Stale Branches (>7 days old) ===")
        for sb in stale:
            days_field = f"({sb['days_ago']} days ago, date: {sb['date']})"
            line_str = f"  {sb['branch']:<30} {sb['last_commit']} {days_field}"
            print(line_str)
        print()
    else:
        print("\nNo stale branches (>7 days).")

    # Ahead/behind
    ab = output.get("ahead_behind", [])
    if ab:
        print("\n=== Branch Divergence vs main/master ===")
        for b in ab:
            merge_tag = "MERGEABLE" if b["mergeable"] else f"CONFLICTS: {b['conflicts']}"
            ahead_str = f"+{b['ahead']}" if b["ahead"] is not None else "?"
            behind_str = f"-{b['behind']}" if b["behind"] is not None else "?"
            line_str = f"  {b['branch']:<30} ahead={ahead_str:>3} behind={behind_str:>3} [{merge_tag}]"
            print(line_str)
        print()


if __name__ == "__main__":
    main()
