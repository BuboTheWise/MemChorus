"""Tests for the pre-push board-card gate (IMPL #203).

The gate under test is the decision logic in
``memchorus.pre_push_gate``. We exercise the real code path
(parsing → gate → card lookup) against a *fixture* ``kanban.db`` so the
tests run anywhere (CI included) without the operator's real board.

The four required acceptance cases, and a few regression guards:

  1. PR ref + valid trailer pointing at an existing owned card  -> PASS
  2. PR ref + trailer pointing at a non-existent card          -> FAIL
  3. PR ref but no Board-Card trailer                           -> FAIL
  4. no PR ref (plain dev commit)                               -> PASS

Plus: archived card rejected, unowned card rejected, last-trailer-wins,
trailer-only-in-body, and the pure-function parse helpers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional  # noqa: F401  # used in fixtures

import pytest

from memchorus.pre_push_gate import (
    check_commit,
    evaluate,
    extract_board_card,
    extract_pr_ref,
)

# A real board card id, owned and active — use it for the "valid" cases so
# the fixture mirrors an existing entry.
VALID_CARD_ID = "t_e88e59c3"
# A card id that will NOT exist in the fixture board.
MISSING_CARD_ID = "t_deadbeef"
# A card that exists but is archived.
ARCHIVED_CARD_ID = "t_archived"
# A card that exists but has no assignee.
UNOWNED_CARD_ID = "t_unowned"


# ---------------------------------------------------------------------------
# Fixture kanban.db
# ---------------------------------------------------------------------------
@pytest.fixture()
def board_db(tmp_path: Path) -> Path:
    """Build a minimal canonical.db with a few cards for the lookup to hit."""
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute(
        """
        CREATE TABLE tasks (
            id         TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            body       TEXT,
            assignee   TEXT,
            status     TEXT NOT NULL,
            priority   INTEGER DEFAULT 0,
            created_by TEXT,
            created_at INTEGER
        )
        """
    )
    rows = [
        (VALID_CARD_ID, "IMPL #203", "body", "default", "running"),
        (ARCHIVED_CARD_ID, "old card", "body", "default", "archived"),
        (UNOWNED_CARD_ID, "unowned card", "body", "", "ready"),
        ("t_done_card", "done card", "body", "cthugha", "done"),
    ]
    con.executemany(
        "INSERT INTO tasks (id, title, body, assignee, status, priority) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        rows,
    )
    con.commit()
    con.close()
    return db


# ---------------------------------------------------------------------------
# Pure parse helpers
# ---------------------------------------------------------------------------
class TestExtractPrRef:
    def test_none_when_absent(self):
        assert extract_pr_ref("feat: add hook") is None

    def test_extracts_single(self):
        assert extract_pr_ref("fix: detect synthesis (#202)") == "202"

    def test_last_ref_wins(self):
        assert extract_pr_ref("re: (#1) then rework (#203)") == "203"

    def test_empty_subject(self):
        assert extract_pr_ref("") is None


class TestExtractBoardCard:
    def test_none_when_absent(self):
        assert extract_board_card("feat: add hook (#202)") is None

    def test_trailer_in_body(self):
        msg = "feat: add hook (#202)\n\nBoard-Card: t_e88e59c3"
        assert extract_board_card(msg) == "t_e88e59c3"

    def test_trailer_in_subject_is_unusual_but_matched(self):
        # Trailer regex is multiline; a subject-borne trailer still counts.
        msg = "Board-Card: t_deadbeef"
        assert extract_board_card(msg) == "t_deadbeef"

    def test_last_trailer_wins(self):
        msg = (
            "fix: thing (#202)\n"
            "Board-Card: t_deadbeef\n"
            "superseded above; use the one below\n"
            "Board-Card: t_e88e59c3\n"
        )
        assert extract_board_card(msg) == "t_e88e59c3"


# ---------------------------------------------------------------------------
# The four required acceptance cases (against the real lookup)
# ---------------------------------------------------------------------------
class TestRequiredCases:
    def test_case1_pr_ref_and_valid_trailer_passes(self, board_db: Path):
        r = check_commit(
            "a1b2c3d4",
            "fix: detect synthesis (#202)\n\nBoard-Card: " + VALID_CARD_ID,
            board_db,
        )
        assert r.ok, r.reason
        assert r.pr_ref == "202"
        assert r.board_card == VALID_CARD_ID

    def test_case2_trailer_points_at_missing_card_fails(self, board_db: Path):
        r = check_commit(
            "ffff0011",
            "fix: thing (#205)\n\nBoard-Card: " + MISSING_CARD_ID,
            board_db,
        )
        assert not r.ok
        assert MISSING_CARD_ID in r.reason

    def test_case3_pr_ref_but_no_trailer_fails(self, board_db: Path):
        r = check_commit(
            "00aa11bb",
            "fix: thing (#206)",  # no trailer
            board_db,
        )
        assert not r.ok
        assert r.board_card is None
        assert "trailer" in r.reason

    def test_case4_plain_dev_commit_no_pr_passes(self, board_db: Path):
        r = check_commit(
            "1234abcd",
            "refactor: tidy imports",  # no (#N)
            board_db,
        )
        assert r.ok
        assert r.pr_ref is None


# ---------------------------------------------------------------------------
# Regression guards
# ---------------------------------------------------------------------------
class TestCardValidation:
    def test_archived_card_rejected(self, board_db: Path):
        r = check_commit(
            "aa11bb22",
            "fix: old (#1)\n\nBoard-Card: " + ARCHIVED_CARD_ID,
            board_db,
        )
        assert not r.ok
        assert "archived" in r.reason

    def test_unowned_card_rejected(self, board_db: Path):
        r = check_commit(
            "bb22cc33",
            "fix: orphan (#2)\n\nBoard-Card: " + UNOWNED_CARD_ID,
            board_db,
        )
        assert not r.ok
        assert "assignee" in r.reason

    def test_done_but_owned_card_accepted(self, board_db: Path):
        # A completed+owned card is a normal "wired to a real card" state.
        r = check_commit(
            "cc33dd44",
            "fix: landed (#3)\n\nBoard-Card: t_done_card",
            board_db,
        )
        assert r.ok, r.reason


class TestKanbanDbNotFound:
    def test_pr_ref_trailer_but_no_board_passes_with_note(self):
        # No local board: the trailer exists, existence can't be verified.
        # Design decision: pass (don't block a machine that has no board),
        # but carry a note.
        r = check_commit(
            "dd44ee55",
            "fix: no board here (#9)\n\nBoard-Card: t_deadbeef",
            None,
        )
        assert r.ok
        assert "not verified" in r.reason

    def test_pr_ref_no_trailer_with_no_board_still_fails(self):
        # Missing trailer is a hard condition regardless of board presence.
        r = check_commit("ee55ff66", "fix: missing (#9)", None)
        assert not r.ok


class TestCorruptBoard:
    def test_corrupt_board_returns_clean_reject(self, tmp_path: Path):
        """A board file that exists but is not a valid SQLite database
        must surface as a clean reject (ok=False with a readable reason),
        never as an unhandled sqlite3 traceback through the gate.

        Regression guard: get_kanban_db() gates on ``.exists()``, so a
        corrupt file is a legal input to ``check_commit``.
        """
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(b"this is not a sqlite file")
        r = check_commit(
            "ff00aa11",
            "fix: gate (#203)\n\nBoard-Card: " + VALID_CARD_ID,
            bad,
        )
        assert not r.ok
        assert "could not read" in r.reason
        assert r.board_card == VALID_CARD_ID


class TestEvaluateBatch:
    def test_mixed_commits_reports_each(self, board_db: Path):
        commits = {
            "good1": "fix: good (#1)\n\nBoard-Card: " + VALID_CARD_ID,
            "bad1": "fix: missing trailer (#2)",
            "plain": "chore: no pr",
        }
        results = {sha: r for sha, r in zip(commits.keys(), evaluate(commits, board_db))}
        assert results["good1"].ok, results["good1"].reason
        assert results["good1"].board_card == VALID_CARD_ID
        assert not results["bad1"].ok
        assert results["plain"].ok
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Env override (get_kanban_db precedence)
# ---------------------------------------------------------------------------
def test_env_override_wins(monkeypatch, tmp_path: Path, board_db: Path):
    monkeypatch.setenv("MEMCHORUS_KANBAN_DB", str(board_db))
    from memchorus.pre_push_gate import get_kanban_db

    resolved = get_kanban_db()
    assert resolved == board_db


def test_env_override_missing_file_yields_none(monkeypatch, tmp_path: Path):
    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setenv("MEMCHORUS_KANBAN_DB", str(missing))
    from memchorus.pre_push_gate import get_kanban_db

    assert get_kanban_db() is None


# ---------------------------------------------------------------------------
# Git-plumbing regression: new-branch push must NOT re-gate shared history.
#
# This is the anchor bug found in real-world push (2026-09-10). When a
# brand-new branch is pushed and the remote has no prior ref for it, git
# reports rsha = ZERO on the ref line. The old code anchored at the tip
# SHA (`rev-list <lsha>`) and walked the branch's *entire* history,
# failing on every legacy PR-ref commit that predates the convention.
#
# The fix uses `--not --remotes` for the ZERO case so that shared
# ancestors (already present on any remote ref, e.g. origin/master) are
# excluded and only genuinely-new commits are gated.
# ---------------------------------------------------------------------------
import os
import shutil
import subprocess
import sys

# Repo root (two levels up from tests/) — gives us src/ for PYTHONPATH
# without hardcoding a path.
_REPO = Path(__file__).resolve().parent.parent


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL
    ).strip()


def test_new_branch_push_only_gates_new_commits(tmp_path: Path):
    """New-branch (rsha=ZERO) case must not walk full branch history."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from memchorus.pre_push_gate import collect_new_commits, ZERO_SHA

    local = tmp_path / "local"
    remote = tmp_path / "remote.git"
    local.mkdir()
    subprocess.check_call(
        ["git", "init", "-q", "--bare", str(remote)],
        cwd=tmp_path, stderr=subprocess.DEVNULL,
    )
    subprocess.check_call(
        ["git", "init", "-q", "-b", "master", str(local)],
        cwd=tmp_path, stderr=subprocess.DEVNULL,
    )
    _git(local, "config", "user.email", "test@example.invalid")
    _git(local, "config", "user.name", "Test")
    _git(local, "remote", "add", "origin", str(remote))

    # Seed 10 legacy commits, every other one a PR ref (no trailer).
    # On the REAL board these legacy PR-refs would all FAIL — that's the
    # bug. If the gate walks full history they appear; if it anchors to
    # remote refs they don't.
    for i in range(1, 11):
        _git(local, "commit", "-q", "--allow-empty", "-m", f"feat: legacy ({i})")
    _git(local, "push", "-q", "origin", "master")
    # remote now has the 10 legacy commits on refs/heads/master.

    # Branch off, add 1 new PR-ref commit with a valid trailer.
    _git(local, "checkout", "-qb", "feat-new")
    _git(local, "commit", "-q", "--allow-empty",
         "-m", "feat: new thing (#990)\n\nBoard-Card: t_e88e59c3")
    tip = _git(local, "rev-parse", "HEAD")

    # Drive the FULL gate pipeline the same way git does: run the module
    # in the scratch repo's cwd, pipe the pre-push ref-line on stdin.
    # (collect_new_commits inherits os.getcwd(), so a bare in-process
    #  call here would run git from the *memchorus* checkout and miss
    #  the scratch repo's remote refs entirely.)
    env = dict(os.environ, PYTHONPATH=str(_REPO / "src"),
               MEMCHORUS_KANBAN_DB=os.environ.get("MEMCHORUS_KANBAN_DB",
                                                  str(Path.home() / ".hermes" / "kanban.db")))
    ref_line = f"refs/heads/feat-new {tip} refs/heads/feat-new {ZERO_SHA}"
    proc = subprocess.run(
        [sys.executable, "-m", "memchorus.pre_push_gate"],
        input=ref_line + "\n", text=True, capture_output=True, cwd=str(local),
        env=env,
    )
    out = proc.stdout
    # Gate should look at exactly 1 commit (the tip), not the 10 legacy
    # PR-ref commits already on origin/master.
    assert "gated 1 new commit(s)" in out, (
        f"new-branch case is re-gating shared history. Full output:\n{out}"
    )
    # And that 1 commit (with a valid trailer) should PASS.
    assert "PASS" in out and "BLOCKED" not in out, (
        f"expected the valid-trailer tip to pass. Output:\n{out}"
    )
    assert proc.returncode == 0, f"gate returned {proc.returncode}:\n{out}\n{proc.stderr}"

