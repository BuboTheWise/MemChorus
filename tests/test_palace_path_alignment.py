"""Regression: reader can see writer output in the same on-disk layout.

Root-cause bug (GH-? / Aug 20 2026): MemPalace's reader resolves
``<palace>/chroma.sqlite3`` verbatim (``mempalace/mcp_server.py:864``),
and mempalace's own ``DEFAULT_PALACE_PATH`` is ``~/.mempalace/palace``
(``mempalace/config.py:221``) — i.e. the *leaf* ``palace/`` directory
that directly holds the sqlite file.  When a profile's ``--palace`` is
pointed one level too shallow (at the *parent* dir ``.mempalace``),
the reader opens the empty shell ``.mempalace/chroma.sqlite3`` and the
real data in ``.mempalace/palace/`` is invisible — status/search/KG all
report 0.  The writer had already put data in the leaf; only the reader
path was out of sync.

This test pins the MemChorus transport-layer normalization that makes
this class of bug impossible: given a ``--palace`` that points at the
parent dir while the real data is at ``<parent>/palace/chroma.sqlite3``,
the resolved MCP server args must be re-pointed at the leaf so the reader
and writer see the same DB.

Layout on disk (fixture):

    <tmp>/
      .mempalace/
        chroma.sqlite3          # empty shell (what --palace accidentally pointed at)
        palace/
          chroma.sqlite3        # real 1-row corpus (what the reader must see)

The fix must:
- rewrite ``--palace <parent>`` → ``--palace <parent>/palace`` when the parent
  has no ``chroma.sqlite3`` but the ``palace/`` child does (the Aug 20 case)
- leave args untouched when the parent already holds the chroma file (the
  default ``~/.mempalace/palace`` convention already has data directly there)
- leave args untouched when there is no ``--palace`` flag at all
- leave args untouched when neither the parent nor the child has a
  chroma file (fresh first-run install — do not invent a path)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memchorus.mempalace_memory_source import _normalize_palace_args


@pytest.fixture
def palace_layout(tmp_path: Path):
    """Create the exact on-disk layout from the Aug 20 bug.

    Returns (parent, leaf) where ``parent/chroma.sqlite3`` is an empty
    shell and ``leaf = parent/palace`` holds the real data.
    """
    parent = tmp_path / ".mempalace"
    leaf = parent / "palace"
    leaf.mkdir(parents=True, exist_ok=True)

    # Empty shell at the parent level — the file the reader was opening.
    (parent / "chroma.sqlite3").write_bytes(b"")
    # Real (but minimal) chroma at the leaf — the file the reader wants.
    (leaf / "chroma.sqlite3").write_bytes(b"real-data-marker")

    return parent, leaf


def test_normalize_repoints_when_data_is_one_level_deeper(palace_layout):
    """RED: ``--palace <parent>`` must become ``--palace <parent>/palace``.

    This is the exact Aug 20 case: parent shell is empty, leaf has the real
    data.  The reader would otherwise open the shell and see 0 drawers.
    """
    parent, leaf = palace_layout
    args = ["--palace", str(parent)]
    out = _normalize_palace_args(args)
    assert out == ["--palace", str(leaf)], (
        f"expected --palace to re-point at the leaf dir; got {out!r}"
    )


def test_normalize_leaves_correct_leaf_alone(palace_layout):
    """No-op: ``--palace <leaf>`` (mempalace's own default convention).

    If the parent already holds a ``chroma.sqlite3`` directly, the reader
    is already reading it — the fix must not move the path.
    """
    _, leaf = palace_layout
    args = ["--palace", str(leaf)]
    out = _normalize_palace_args(args)
    assert out == args, (
        f"expected no rewrite when parent already has chroma; got {out!r}"
    )


def test_normalize_leaves_args_without_palace_flag(tmp_path):
    """No-op: no ``--palace`` flag in the args at all.

    Some profiles may run the MCP server with env-var configuration only,
    no ``--palace`` argv.  The fix must not touch those args.
    """
    args = ["--verbose", "--quiet"]
    out = _normalize_palace_args(args)
    assert out == args


def test_normalize_leaves_fresh_install_alone(tmp_path):
    """No-op: neither parent nor child has a chroma file (fresh install).

    On first run the palace data dir doesn't exist yet — the reader will
    create it.  The fix must not invent a path (e.g. append ``/palace``)
    based on a nonexistent layout.
    """
    parent = tmp_path / "fresh"
    parent.mkdir()
    args = ["--palace", str(parent)]
    out = _normalize_palace_args(args)
    assert out == args, (
        f"must not invent a sub-path when neither layout holds data; got {out!r}"
    )


def test_normalize_preserves_arg_order_and_other_flags(tmp_path):
    """Rewrite only the ``--palace`` value; leave the rest of the argv alone."""
    parent = tmp_path / ".mempalace"
    leaf = parent / "palace"
    leaf.mkdir(parents=True, exist_ok=True)
    (parent / "chroma.sqlite3").write_bytes(b"")
    (leaf / "chroma.sqlite3").write_bytes(b"real")

    args = ["--verbose", "--palace", str(parent), "--quiet"]
    out = _normalize_palace_args(args)
    assert out == ["--verbose", "--palace", str(leaf), "--quiet"]


# --------------------------------------------------------------------------- #
#  Section B — E2E: normalised reader path must see the writer's data
# --------------------------------------------------------------------------- #
#  Contract point 6 (canonical requirement, MemChorus-Requirements.md
#  "MemPalace DB Location Contract (2026-08-31)", point 6):
#  "After writing a drawer through the memchorus memory source, a
#  subsequent MCP reader call (mempalace_status or mempalace_search)
#  in the same profile must see it."
#
#  This is the test that would have caught the Aug 20 split: the writer
#  put data in <parent>/palace/chroma.sqlite3 but the reader was opening
#  <parent>/chroma.sqlite3 (an empty shell).  After normalization the
#  reader resolves to the same leaf file the writer wrote, and the
#  embeddings are queryable there.
# --------------------------------------------------------------------------- #

import os
import sqlite3


class TestE2EReaderSeesWriter:
    """Prove one-path-one-file (contract points 2 + 6): after writing a
    drawer at the leaf, the normalised reader path resolves to the same
    file and sees the row."""

    @staticmethod
    def _make_leaf_with_data(leaf: Path, n: int = 1) -> None:
        """Create a real chroma.sqlite3 at *leaf* with *n* embeddings."""
        chroma = leaf / "chroma.sqlite3"
        con = sqlite3.connect(str(chroma))
        con.execute(
            "CREATE TABLE embeddings ("
            "id TEXT PRIMARY KEY, "
            "embedding BLOB)"
        )
        for i in range(n):
            con.execute(
                "INSERT INTO embeddings (id, embedding) VALUES (?, ?)",
                (f"doc_{i}", b"\x00\x01"),
            )
        con.commit()
        con.close()

    def test_normalised_path_is_identical_to_writer_target(self, tmp_path: Path):
        """Contract point 2 (one-path-one-file) — reader and writer MUST open
        the same chroma.sqlite3.

        Layout: writer puts data at ``<parent>/palace/chroma.sqlite3``; a
        (mis)configured reader is pointing at ``<parent>/chroma.sqlite3``
        (the empty shell from the Aug 20 bug). After ``_normalize_palace_args``
        the reader path must resolve to the exact same directory as the
        writer's target.

        Uses ``os.path.realpath`` to compare canonicalised paths, so symlinks
        or ``..`` in the string form don't hide a real disagreement.
        """
        parent = tmp_path / ".mempalace"
        leaf = parent / "palace"
        leaf.mkdir(parents=True)

        # Writer puts real data at the leaf
        self._make_leaf_with_data(leaf, n=3)
        # Reader (pre-normalisation) was opening this empty shell at the parent
        open(parent / "chroma.sqlite3", "wb")  # 0-byte shell

        normalised = _normalize_palace_args(["--palace", str(parent)])
        reader_path = os.path.realpath(normalised[1])

        # The reader must now resolve to the writer's actual target directory.
        assert reader_path == os.path.realpath(str(leaf)), (
            f"reader path and writer path must be the same directory "
            f"(one-path-one-file contract). reader={reader_path!r} "
            f"writer_target={os.path.realpath(str(leaf))!r}"
        )

    def test_normalised_reader_sees_all_writer_rows(self, tmp_path: Path):
        """Contract point 6 — rows written by the writer are visible to the
        reader through the normalised path.

        This is the minimal, deterministic version of the acceptance test:
        the reader's path resolves to a directory whose ``chroma.sqlite3``
        can be opened and queried, and the rows the writer inserted (3)
        are present.  If the reader had been pointed at the parent (the
        Aug 20 bug state), its path's ``chroma.sqlite3`` would be empty
        and the row count would be 0 — which is exactly the failure mode
        that motivated this fix.
        """
        parent = tmp_path / ".mempalace"
        leaf = parent / "palace"
        leaf.mkdir(parents=True)
        self._make_leaf_with_data(leaf, n=3)
        open(parent / "chroma.sqlite3", "wb")

        # The bug state: the (un-normalised) reader path resolves to a file
        # with zero rows.
        reader_shell = sqlite3.connect(str(parent / "chroma.sqlite3"))
        shell_rows = (
            reader_shell.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            if reader_shell.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='embeddings'").fetchone()
            else 0
        )
        reader_shell.close()
        assert shell_rows == 0, (
            f"parent-level chroma should be the empty shell (bug state); "
            f"got {shell_rows} rows"
        )

        # After normalisation, the reader path resolves to the leaf with data.
        reader_path = Path(_normalize_palace_args(["--palace", str(parent)])[1])
        con = sqlite3.connect(str(reader_path / "chroma.sqlite3"))
        try:
            n = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        finally:
            con.close()
        assert n == 3, (
            f"the normalised reader path must see the 3 writer rows; "
            f"got {n} (path={reader_path!r})"
        )

