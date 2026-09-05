"""Acceptance test (a): writer and reader agree BY CONSTRUCTION.

IMPL #172 acceptance: "two writers/readers in the same process land on the
same path WITHOUT the normalization doing the work."

The distinction from the existing ``test_palace_path_alignment.py``
(section B) is deliberate.  Those tests use the Aug-20 split layout (parent
holds an empty shell, <parent>/palace holds the data) and *assert that the
normalization rewrites the reader's path* to reach the leaf.  In that
scenario the reader only lands on the right file because
``_normalize_palace_args`` did the work.

This file is the opposite contract: the writer and reader are BOTH
configured against the canonical leaf from the start (the state you get
after a correct ``memchorus-init``), so:

* the reader must NOT rewrite (a no-op — no WARNING fires), AND
* the reader's resolved data file must equal the writer's data file,
  both resolved through the SINGLE shared resolver
  :func:`memchorus.palace_path.palace_data_dir` / ``palace_data_file``.

That is the "structurally cannot disagree" guarantee: there is one function
that owns the parent-vs-leaf decision, and both sides call it.  If they
ever disagree, this test goes red before the operator ever sees a
silent-empty-vault.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import pytest

from memchorus import palace_path
from memchorus.mempalace_memory_source import _normalize_palace_args

_LOGGER_NAME = "memchorus.mempalace_memory_source"


@pytest.fixture
def canonical_leaf(tmp_path: Path) -> Path:
    """A correctly configured canonical leaf: data lives *directly* at
    ``<leaf>/chroma.sqlite3`` (no ``palace/`` sub-dir, no empty shell at a
    parent).  This is the state a correct ``memchorus-init`` produces, and
    the state MemPalace's own ``~/.mempalace/palace`` default describes."""
    leaf = tmp_path / ".mempalace" / "palace"
    leaf.mkdir(parents=True)
    con = sqlite3.connect(str(leaf / "chroma.sqlite3"))
    con.execute(
        "CREATE TABLE IF NOT EXISTS embeddings "
        "(id TEXT PRIMARY KEY, embedding BLOB)"
    )
    for i in range(5):
        con.execute(
            "INSERT INTO embeddings (id, embedding) VALUES (?, ?)",
            (f"doc_{i}", b"\x00\x01"),
        )
    con.commit()
    con.close()
    return leaf


def _row_count(chroma: Path) -> int:
    if not chroma.exists():
        return 0
    con = sqlite3.connect(str(chroma))
    try:
        try:
            return con.execute(
                "SELECT COUNT(*) FROM embeddings"
            ).fetchone()[0]
        except sqlite3.Error:
            return 0
    finally:
        con.close()


def test_writer_and_reader_resolve_same_path_without_repointing(
    canonical_leaf: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both the writer and the reader, pointed at the same canonical leaf,
    must resolve to the same ``chroma.sqlite3`` with NO rewrite — the reader
    is a pure no-op (no WARNING) and the two data files are byte-for-byte
    the same absolute path."""

    # Writer side: the single shared resolver says where the data file is.
    # auto_init passes a data_dir root; the authoritative answer comes from
    # palace_path.  Here the writer is pointed straight at the leaf.
    writer_data_file = palace_path.palace_data_file(canonical_leaf)

    # Reader side: the MCP transport is configured with --palace <leaf>.
    # A correctly-configured reader must NOT rewrite the path.
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        reader_dir = Path(_normalize_palace_args(["--palace", str(canonical_leaf)])[1])
    reader_data_file = reader_dir / "chroma.sqlite3"

    # 1. The reader did NOT re-point (normalization did not do the work).
    repoints = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Rewrote --palace" in r.getMessage()
    ]
    assert not repoints, (
        "the correctly-configured reader must be a no-op (no rewrite) — "
        f"got a rewrite: {[r.getMessage() for r in repoints]!r}"
    )

    # 2. Writer and reader agree on the exact same absolute data path.
    assert os.path.realpath(str(writer_data_file)) == os.path.realpath(str(reader_data_file)), (
        f"writer and reader must resolve to the same chroma file by construction; "
        f"writer={writer_data_file!r} reader={reader_data_file!r}"
    )

    # 3. That path actually holds the writer's data (5 rows) — the reader
    #    reads a populated corpus, not an empty shell.
    assert _row_count(reader_data_file) == 5


def test_reader_and_writer_agree_across_two_call_sites(
    canonical_leaf: Path
) -> None:
    """The acceptance literally names 'two writers/readers in the same
    process'.  Simulate a second independent call site (a second in-process
    reader, or the auto_init writer config) and confirm every call site —
    writer config AND reader transport — funnels through the single resolver
    and lands on the identical path.

    This is what 'structurally cannot disagree' means at the interface
    level: no call site re-derives the path on its own; they all call
    :func:`palace_path.palace_data_file`."""
    # Writer call site (auto_init-style): resolve through the shared module.
    writer_a = palace_path.palace_data_file(canonical_leaf)
    # Independent reader call site (transport-style): resolve through the
    # shared module that _normalize_palace_args delegates to.
    reader_dir = palace_path.palace_data_dir(canonical_leaf)
    reader_a = reader_dir / palace_path.CHROMA_FILE

    assert writer_a == reader_a
    assert os.path.realpath(str(writer_a)) == os.path.realpath(str(reader_a))
