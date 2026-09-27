#!/usr/bin/env python3
"""Pre-push board-card gate — enforce "a landing PR is wired to a claimed card."

Purpose
-------
The board (Kanban) and the repo (git) are two separate truths. A recurring
failure mode is a PR that lands on ``master`` with CI / OPSEC / version all
green, yet its board card was never claimed by a worker — the repo wins by
default and the board loses. This is a *mechanical* gate for exactly that class of
leak: at the merge boundary (the pre-push hook), every commit that references a
pull request must also name the board card that owns it, and that card must
exist and be owned by a real worker profile.

Commit convention (new — IMPL #203)
-----------------------------------
A commit whose message contains a parenthesised PR reference — the ``(#N)``
form, where the ``#`` is immediately preceded by ``(`` (e.g. ``(closes #156)``),
``(#202)``) — MUST also carry a trailer:

    Board-Card: t_<8hex>

A commit that has no ``(#N)`` reference is a plain dev commit; no trailer is
required. (Issue references in prose, e.g. ``closes #156``, do NOT trigger the
gate — only the parenthesised ``( #N )`` form does.)

Gate decision (per commit)
--------------------------
  1. no ``( #N )`` in the message             -> PASS (no trailer required)
  2. has ``( #N )`` but no ``Board-Card:``     -> FAIL (trailer missing)
  3a. has both but the card is not found      -> FAIL (card absent in kanban.db)
  3b. has both, card is ``archived``          -> FAIL (card no longer live)
  3c. has both, card assignee is empty        -> FAIL (card unowned)
  3d. has both, assignee not a worker profile -> FAIL (owned by a non-worker)
  3e. has both, card exists + owned by worker -> PASS

This module is pure standard library (sqlite3 + subprocess for ``git``), has no
network calls, and is importable so the test suite exercises the decision logic
directly. The pre-push hook (``.githooks/pre-push``) is a thin launcher that execs
``main()``.

Card lookup
-----------
The gate reads the local Kanban store (never a network service — CI hosts do not
have it, which is why this is a *local, pre-push* gate and not a CI job). Lookup
order:

  1. ``PREPUSH_KANBAN_DB``   (test / explicit override)
  2. ``HERMES_KANBAN_DB``    (set by the dispatcher for the current task)
  3. ``~/.hermes/kanban.db`` (the canonical top-level board store)
  4. ``~/.hermes/kanban/boards/<board>/kanban.db`` (per-board stores; ``_archived``
     excluded)

The first candidate in that order that has a ``tasks`` table is consulted; a card
found in any of them is valid.

Exit codes
----------
  0   every pushed commit passed
  1   one or more commits failed the gate
  2   operational error (not a git repo, git unavailable, bad input)
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

ZERO_SHA = "0" * 40

# ``(#N)`` — a ``#`` immediately preceded by ``(`` and followed by digits + ``)``.
# This is the parenthesised PR-reference form only; prose ``#N`` does not match.
PR_REF_RE = re.compile(r"\(#(\d+)\)")

# Trailer ``Board-Card: <token>`` (value is the card id, e.g. ``t_e88e59c3``).
BOARD_CARD_RE = re.compile(r"Board-Card:\s*(\S+)", re.IGNORECASE)

# Canonical card id shape: ``t_`` + at least 8 hex chars. We accept a bit more
# (other boards use longer ids) so we do not false-positive on valid ids.
CARD_ID_SHAPE_RE = re.compile(r"^t_[0-9a-f]{8,}$", re.IGNORECASE)

# Worker profiles that may own a landing card. ``HERMES_PROFILE`` is added at
# call time when present (the dispatcher sets it for the active worker).
KNOWN_WORKER_PROFILES = frozenset({"cthugha", "default"})


# --------------------------------------------------------------------------- #
# Extraction / decision primitives
# --------------------------------------------------------------------------- #
def extract_pr_refs(message: str) -> List[str]:
    """Return the list of ``(#N)`` PR numbers found in a commit message."""
    return PR_REF_RE.findall(message or "")


def extract_board_card(message: str) -> Optional[str]:
    """Return the ``Board-Card:`` trailer value, or ``None`` if absent."""
    m = BOARD_CARD_RE.search(message or "")
    return (m.group(1).strip() if m else None)


def default_allowed_profiles() -> frozenset:
    """Worker profiles that may own a card — KNOWN set + active ``HERMES_PROFILE`` + ``PREPUSH_ALLOWED_PROFILES``.

    ``PREPUSH_ALLOWED_PROFILES`` is a comma-separated env override (test / CI use
    case where the fixture-DB uses synthetic profile names).
    """
    allowed = set(KNOWN_WORKER_PROFILES)
    cur = os.environ.get("HERMES_PROFILE")
    if cur:
        allowed.add(cur.strip())
    extra = os.environ.get("PREPUSH_ALLOWED_PROFILES")
    if extra:
        allowed.update(p.strip() for p in extra.split(",") if p.strip())
    return frozenset(allowed)


def check_commit(
    message: str,
    db_paths: Optional[List[str]] = None,
    allowed_profiles: Optional[frozenset] = None,
) -> Tuple[bool, str]:
    """Decide PASS/FAIL for a single commit message.

    Returns ``(ok, reason)``. The reason is a human-readable explanation —
    used verbatim in hook output and test assertions.
    """
    allowed = default_allowed_profiles() if allowed_profiles is None else allowed_profiles

    prs = extract_pr_refs(message)
    if not prs:
        return True, "no PR reference — trailer not required"

    card = extract_board_card(message)
    prs_str = ", ".join("#" + p for p in prs)
    if card is None:
        return False, (
            f"references {prs_str} but has no 'Board-Card: t_<8hex>' trailer. "
            "Name the board card that owns this PR, e.g. a card you created and "
            "claimed, then append the line:  Board-Card: t_<8hex>"
        )

    if not CARD_ID_SHAPE_RE.match(card):
        # Still check existence (some boards use other id shapes), but note the
        # shape is not the canonical ``t_<8hex>``.
        shape_note = " (note: id does not match the canonical 't_<8hex>' shape)"
    else:
        shape_note = ""

    row = card_lookup(card, db_paths=db_paths)
    if row is None:
        return False, (
            f"Board-Card trailer references '{card}'{shape_note}, which was not "
            "found in any reachable local kanban.db. Point it at an existing card."
        )
    if row.get("status") == "archived":
        return False, f"card '{card}' is archived — link a live card instead."
    assignee = row.get("assignee")
    if not assignee:
        return False, (
            f"card '{card}' has no assignee. A landing PR must be wired to a "
            "card owned by a worker profile — claim the card, then push."
        )
    if assignee not in allowed:
        return False, (
            f"card '{card}' is assigned to '{assignee}', which is not a known "
            f"worker profile (allowed: {', '.join(sorted(allowed))})."
        )
    return True, (
        f"card '{card}' valid (assignee={assignee}, status={row.get('status')})"
    )


# --------------------------------------------------------------------------- #
# Kanban store lookup (local SQLite only — no network)
# --------------------------------------------------------------------------- #
def _candidate_db_paths() -> List[str]:
    cands: List[str] = []
    for var in ("PREPUSH_KANBAN_DB", "HERMES_KANBAN_DB"):
        p = os.environ.get(var)
        if p and os.path.exists(p):
            cands.append(os.path.abspath(p))
    home = os.path.expanduser("~")
    cands.append(os.path.join(home, ".hermes", "kanban.db"))
    boards = os.path.join(home, ".hermes", "kanban", "boards")
    if os.path.isdir(boards):
        for name in sorted(os.listdir(boards)):
            if name == "_archived":
                continue
            p = os.path.join(boards, name, "kanban.db")
            if os.path.exists(p):
                cands.append(p)
    seen = set()
    out = []
    for p in cands:
        a = os.path.abspath(p)
        if a in seen:
            continue
        seen.add(a)
        out.append(a)
    return out


def _query_card(db: str, card_id: str) -> Optional[Dict[str, str]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT id, assignee, status FROM tasks WHERE id = ? LIMIT 1",
            (card_id,),
        )
        r = cur.fetchone()
    finally:
        con.close()
    if r is None:
        return None
    return {"id": r["id"], "assignee": r["assignee"], "status": r["status"]}


def card_lookup(
    card_id: str, db_paths: Optional[List[str]] = None
) -> Optional[Dict[str, str]]:
    """Find ``card_id`` across the local Kanban stores. Returns the row dict or None."""
    cands = list(db_paths) if db_paths else _candidate_db_paths()
    for db in cands:
        if not os.path.exists(db) or os.path.getsize(db) == 0:
            continue
        try:
            if not _has_tasks_table(db):
                continue
        except sqlite3.DatabaseError:
            continue
        try:
            row = _query_card(db, card_id)
        except sqlite3.DatabaseError:
            continue
        if row is not None:
            return row
    return None


def _has_tasks_table(db: str) -> bool:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        n = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0]
    finally:
        con.close()
    return bool(n)


# --------------------------------------------------------------------------- #
# Git range / evaluation
# --------------------------------------------------------------------------- #
def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )


def _commit_message(sha: str) -> str:
    r = _git("log", "-n", "1", "--format=%B", sha)
    return r.stdout if r.returncode == 0 else ""


def _new_commits(local_sha: str, remote_sha: str, remote_ref: str) -> List[str]:
    """Commits to validate for one ref-push line.

    New ref (remote is all-zero) is diffed against the merge-base with the local
    ``origin/<branch>``; if that base cannot be determined, only the tip commit is
    checked (conservative — never re-litigate full history).
    """
    if remote_sha and remote_sha != ZERO_SHA:
        base = remote_sha
    else:
        branch = remote_ref.rsplit("/", 1)[-1]
        r = _git("merge-base", local_sha, "refs/remotes/origin/" + branch)
        base = r.stdout.strip() if r.returncode == 0 else ""
    if not base:
        return [local_sha]
    r = _git("rev-list", "--first-parent", local_sha, "^" + base)
    if r.returncode == 0:
        shas = [s.strip() for s in r.stdout.splitlines() if s.strip()]
        return shas or [local_sha]
    return [local_sha]


def evaluate(
    refs_text: str,
    db_paths: Optional[List[str]] = None,
    allowed_profiles: Optional[frozenset] = None,
) -> Tuple[int, List[Tuple[str, str]]]:
    """Evaluate every pushed commit. Returns ``(checked_count, failures)`` where
    each failure is ``(sha, reason)``."""
    failures: List[Tuple[str, str]] = []
    checked = 0
    for line in refs_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts[:4]
        if local_sha == ZERO_SHA:
            continue  # branch deletion — nothing to validate
        for sha in _new_commits(local_sha, remote_sha, remote_ref):
            checked += 1
            ok, reason = check_commit(
                _commit_message(sha),
                db_paths=db_paths,
                allowed_profiles=allowed_profiles,
            )
            if not ok:
                failures.append((sha, reason))
    return checked, failures


def main() -> int:
    refs_text = sys.stdin.read()
    checked, failures = evaluate(refs_text)
    if failures:
        print("pre-push board-card gate: FAILED")
        for sha, reason in failures:
            print(f"  {sha[:8]}: {reason}")
        print("Fix the commit message(s) (add 'Board-Card: t_<8hex>') and re-push, "
              "or bypass once with:  git push --no-verify")
        return 1
    print(f"pre-push board-card gate: OK ({checked} commit(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
