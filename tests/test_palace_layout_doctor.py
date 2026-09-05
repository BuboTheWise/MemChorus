"""Acceptance test (c): memchorus-doctor --palace-layout --strict.

The legacy-leaf layout (reader pointed at parent, data at <parent>/palace)
must be detected as FAIL under --strict.  After ``migrate`` re-points the
effective reader to the canonical leaf, the same layout reported via the
leaf must be canonical -> PASS (exit 0).

This is the acceptance gate for the "doctor blocks the fallback" part of
IMPL #172.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memchorus.install_doctor import (
    FAIL,
    PASS,
    WARN,
    palace_layout_report,
)
from memchorus.palace_path import (
    BRANCH_CANONICAL,
    classify,
    migrate,
)


def _make_legacy_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return (parent, leaf) where parent holds an empty shell and leaf
    holds a real 3-row chroma.sqlite3 — the exact Aug-20 split."""
    parent = tmp_path / ".mempalace"
    leaf = parent / "palace"
    leaf.mkdir(parents=True)
    (parent / "chroma.sqlite3").write_bytes(b"")
    # Real leaf with a single embeddings table + 3 rows.
    import sqlite3
    con = sqlite3.connect(str(leaf / "chroma.sqlite3"))
    con.execute(
        "CREATE TABLE IF NOT EXISTS embeddings "
        "(id TEXT PRIMARY KEY, embedding BLOB)"
    )
    for i in range(3):
        con.execute(
            "INSERT INTO embeddings (id, embedding) VALUES (?, ?)",
            (f"doc_{i}", b"\x00\x01"),
        )
    con.commit()
    con.close()
    return parent, leaf


# ---------------------------------------------------------------------------
# (c) — strict FAILs on the legacy layout reported at the parent
# ---------------------------------------------------------------------------

def test_strict_fails_on_legacy_layout(tmp_path: Path) -> None:
    """--palace-layout --strict with the reader pointed at <parent>
    must FAIL (because the data is at <parent>/palace, not <parent>)."""
    parent, _leaf = _make_legacy_layout(tmp_path)

    results = palace_layout_report(strict=True, explicit_root=str(parent))
    assert len(results) == 1
    assert results[0].status == FAIL, (
        f"expected FAIL under --strict for the legacy-leaf layout; "
        f"got {results[0].status} / {results[0].message!r}"
    )
    # The message must name both the parent and the leaf so the operator
    # can fix it.
    assert str(parent) in results[0].message
    assert "palace" in results[0].message.lower()


def test_nonstrict_warns_on_legacy_layout(tmp_path: Path) -> None:
    """--palace-layout (not --strict) with the legacy layout must be a
    WARN, not a FAIL — CI should not block, but the operator sees the
    exact repoint command."""
    parent, _leaf = _make_legacy_layout(tmp_path)

    results = palace_layout_report(strict=False, explicit_root=str(parent))
    assert len(results) == 1
    assert results[0].status == WARN, (
        f"expected WARN (not FAIL) without --strict; got {results[0].status}"
    )


# ---------------------------------------------------------------------------
# (c) — after migrate, the leaf is canonical -> strict PASS
# ---------------------------------------------------------------------------

def test_migrate_then_strict_passes(tmp_path: Path) -> None:
    """After migrate re-points to the canonical leaf, reporting the
    layout at that leaf must be canonical -> PASS under --strict."""
    parent, leaf = _make_legacy_layout(tmp_path)

    # 1. migrate confirms this is the legacy-leaf case
    result = migrate(parent)
    assert result.needs_repoint is True
    assert os.path.realpath(str(result.canonical)) == os.path.realpath(str(leaf))

    # 2. After migrate, the effective root is the leaf.
    #    Classifying the leaf: it IS the root that holds the data.
    layout = classify(leaf)
    assert layout.branch == BRANCH_CANONICAL, (
        f"leaf should classify as canonical after migrate; "
        f"got branch={layout.branch!r}"
    )

    # 3. --palace-layout --strict with explicit_root = leaf -> PASS
    results = palace_layout_report(strict=True, explicit_root=str(leaf))
    assert len(results) == 1
    assert results[0].status == PASS, (
        f"expected PASS after migrate for the leaf; "
        f"got {results[0].status} / {results[0].message!r}"
    )
