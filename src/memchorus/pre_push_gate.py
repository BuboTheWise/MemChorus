"""Pre-push board-card gate.

Enforces a local commit convention at the push boundary:

    Every commit whose subject references a PR (``(#N)``) MUST also
    carry a ``Board-Card: t_<hex>`` trailer pointing at a real, owned
    Kanban card.

This is the mechanical counterpart to the discipline rule
"claim the card before implementing". The repo and the board are two
sources of truth; without this gate the repo has been winning by
default (IMPL #201 landed with an unowned card).

The gate reads the operator's local board at
``~/.hermes/kanban.db`` (overridable via ``$MEMCHORUS_KANBAN_DB``,
which is how the tests point at a fixture). CI hosts don't ship the
operator's board, so this is a **pre-push** gate on the operator's
box, not a CI step. ``.githooks/pre-push`` is a thin dispatcher.

Run modes
---------
  python -m memchorus.pre_push_gate        # reads git ref lines from stdin
  MEMCHORUS_KANBAN_DB=/path/to/canonical.db ...  # override for tests

Exit codes
----------
  0  gate passes (all commits OK, OR no PR-referencing commits)
  1  gate blocks (a PR-referencing commit is missing a trailer, or
     points at a card that's missing / archived / unowned)
  2  configuration error (no python interpreter, not a git repo, etc.)

Design notes
------------
* ``check_commit`` is a pure function ``(sha, full_message, kanban_db_path)``
  so it can be unit-tested without touching git or the real board.
* The trailer is matched in the *full commit message*, not just the
  subject — git trailers live in the body.
* ``$MEMCHORUS_KANBAN_DB`` takes precedence over the default candidates;
  any missing path is returned as ``None`` which is treated as "gate
  applies but cannot verify" — pass with a note, not a hard-fail.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------
# PR reference: ``(#N)`` where N is one or more digits.
PR_REF_RE = re.compile(r"\(#(\d+)\)")

# Board-Card trailer: a line like ``Board-Card: t_e88e59c3``.
# Matches anywhere in the message (multiline), takes the *last* match
# so that trailers that appear at the bottom of the body win over
# any earlier, superseded ones.
BOARD_CARD_RE = re.compile(r"^\s*Board-Card:\s*(\S+)\s*$", re.MULTILINE)

# Candidate kanban.db locations, in priority order.
DEFAULT_KANBAN_DB_CANDIDATES = (
    Path.home() / ".hermes" / "kanban.db",
    Path.home() / ".config" / "hermes" / "kanban.db",
)


def get_kanban_db() -> Optional[Path]:
    """Resolve the canonical kanban.db path, or None.

    Priority:
      1. ``$MEMCHORUS_KANBAN_DB`` if set and pointing at an existing file.
      2. ``~/.hermes/kanban.db`` (the default worker layout).
      3. ``~/.config/hermes/kanban.db``.

    Returns None if none exists — meaning "gate applies but cannot
    verify" (tests point at a fixture; a fresh machine without any
    board is still allowed to push plain commits).
    """
    env = os.environ.get("MEMCHORUS_KANBAN_DB")
    if env:
        p = Path(env)
        return p if p.exists() else None
    for cand in DEFAULT_KANBAN_DB_CANDIDATES:
        if cand.exists():
            return cand
    return None


# ---------------------------------------------------------------------------
# Parsing primitives
# ---------------------------------------------------------------------------
def extract_pr_ref(subject: str) -> Optional[str]:
    """Return the last ``(#N)`` PR reference in ``subject``, or None.

    We take the last occurrence because PR references typically appear
    at the tail of a commit subject (``fix: ... (#202)``).
    """
    if not subject:
        return None
    matches = PR_REF_RE.findall(subject)
    return matches[-1] if matches else None


def extract_board_card(message: str) -> Optional[str]:
    """Return the last ``Board-Card:`` trailer in ``message``, or None.

    Trailer value is a single whitespace-delimited token
    (``t_e88e59c3``). Matching in the full message (not just the
    subject) is what makes this real git-trailer-compatible.
    """
    if not message:
        return None
    matches = BOARD_CARD_RE.findall(message)
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Card validation
# ---------------------------------------------------------------------------
@dataclass
class GateResult:
    """Outcome of a single-commit gate check."""

    ok: bool
    pr_ref: Optional[str]
    board_card: Optional[str]
    reason: str = ""


def _validate_card(kanban_db: Path, card_id: str) -> Tuple[bool, str]:
    """Check that ``card_id`` points at a real, owned, non-archived card.

    Read-only; opens and closes the connection per call (short-lived,
    the gate is a one-shot pre-push check).

    ``kanban_db`` is expected to exist (``get_kanban_db`` filters on
    ``.exists()``); we still guard the query so a corrupt or lock-held
    board surfaces as a clean reject rather than a traceback through
    the push gate.
    """
    con = None
    try:
        try:
            con = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True)
        except sqlite3.Error:
            con = sqlite3.connect(str(kanban_db))
        row = con.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?",
            (card_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        return False, f"could not read card {card_id!r} from {kanban_db.name}: {exc}"
    finally:
        if con is not None:
            con.close()

    if row is None:
        return False, f"card {card_id!r} not found in {kanban_db.name}"
    status = (row[0] or "").strip()
    assignee = (row[1] or "").strip()
    if status == "archived":
        return False, f"card {card_id!r} is archived"
    if not assignee:
        return False, f"card {card_id!r} has no assignee"
    return True, ""


def check_commit(
    sha: str,
    full_message: str,
    kanban_db: Optional[Path],
) -> GateResult:
    """Run the gate against one commit.

    ``full_message`` is the *complete* commit message (subject + body);
    the PR ref is parsed from the subject, the trailer from the whole
    message. Pure function: no I/O beyond the optional card lookup.
    """
    lines = full_message.splitlines() if full_message else []
    subject = lines[0] if lines else ""
    pr_ref = extract_pr_ref(subject)

    if pr_ref is None:
        # Plain dev commit (no PR reference). Gate does not apply.
        return GateResult(ok=True, pr_ref=None, board_card=None)

    board_card = extract_board_card(full_message)
    if board_card is None:
        return GateResult(
            ok=False,
            pr_ref=pr_ref,
            board_card=None,
            reason=(
                f"commit {sha[:8]} references PR #{pr_ref} but carries no "
                f"Board-Card: trailer. Create/claim the card first, then "
                f"amend or cherry-pick with a trailer e.g. `Board-Card: "
                f"t_e88e59c3`."
            ),
        )

    if kanban_db is None:
        return GateResult(
            ok=True,
            pr_ref=pr_ref,
            board_card=board_card,
            reason="kanban.db not found locally; card existence not verified",
        )

    ok, why = _validate_card(kanban_db, board_card)
    return GateResult(ok=ok, pr_ref=pr_ref, board_card=board_card, reason=why)


def evaluate(
    commits: Dict[str, str],
    kanban_db: Optional[Path],
) -> List[GateResult]:
    """Run the gate over a set of commits. Returns one result per commit."""
    return [
        check_commit(sha, msg, kanban_db) for sha, msg in commits.items()
    ]


# ---------------------------------------------------------------------------
# Git-side plumbing (only used from main())
# ---------------------------------------------------------------------------
ZERO_SHA = "0" * 40


def _git_output(args: List[str]) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git"] + args, stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def collect_new_commits(push_refs: List[Tuple[str, str, str, str]]) -> Dict[str, str]:
    """For each ref on stdin, collect new commits (sha → full message).

    ``push_refs`` has the shape git uses for pre-push:
    ``<local_ref> <local_sha> <remote_ref> <remote_sha>``.
    Deletions (local_sha all-zero) are skipped.

    Two anchor cases, matching ``git for-each-ref`` semantics:

    * Remote already tracks this branch (``rsha != ZERO``):
      gate the delta ``rsha..lsha`` — exactly the commits the push
      adds on top of what remote already has.

    * New branch on remote (``rsha == ZERO``):
      the historical commits on this branch are shared ancestor state
      with some existing remote ref (e.g. ``origin/master``), and
      re-gating them would fail on commits made before the convention
      existed. Use ``<lsha> --not --remotes`` to anchor against *every
      existing remote ref* — leaves only the genuinely-new commits.

    This is the standard "what is new on this push" definition and is
    what ``git count-objects -vH --all`` family of commands use.
    """
    out: Dict[str, str] = {}
    for _lref, lsha, _rref, rsha in push_refs:
        if lsha == ZERO_SHA:
            continue  # branch deletion — nothing to gate
        if rsha == ZERO_SHA:
            rev_args = ["rev-list", lsha, "--not", "--remotes"]
        else:
            rev_args = ["rev-list", f"{rsha}..{lsha}"]
        shas = _git_output(rev_args) or ""
        for sha in shas.split():
            if sha in out:
                continue
            out[sha] = _git_output(["log", "-1", "--format=%B", sha]) or ""
    return out


def parse_push_refs() -> List[Tuple[str, str, str, str]]:
    refs = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) == 4:
            refs.append(tuple(parts))
    return refs  # type: ignore[return-value]


def main(argv: Optional[List[str]] = None) -> int:  # noqa: ARG001
    kanban_db = get_kanban_db()
    push_refs = parse_push_refs()
    if not push_refs:
        print("pre-push: no push refs read (pass; nothing to gate)")
        return 0

    commits = collect_new_commits(push_refs)
    if not commits:
        print("pre-push: no new commits to check (pass)")
        return 0

    results = evaluate(commits, kanban_db)
    failures = [r for r in results if not r.ok]

    print(f"pre-push: gated {len(results)} new commit(s) against "
          f"{kanban_db if kanban_db else '<no local board>'}")
    for r in results:
        marker = "PASS" if r.ok else "FAIL"
        pr = f"  PR #{r.pr_ref}" if r.pr_ref else ""
        card = f"  board-card={r.board_card}" if r.board_card else "  board-card=<none>"
        print(f"  {marker}  {pr}{card}")
        if r.reason:
            print(f"         {r.reason}")

    if failures:
        print(f"pre-push: BLOCKED — {len(failures)} of {len(results)} commit(s) failed")
        print("pre-push: fix the commits above (add a valid Board-Card: trailer) and retry.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
