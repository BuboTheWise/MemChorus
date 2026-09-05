"""Canonical MemPalace layout resolution — the single decision point.

Why this module exists
----------------------
MemPalace's MCP reader opens ``<palace>/chroma.sqlite3`` **verbatim**
(``mempalace/config.py`` -> ``mcp_server``) and never descends into a sub-
directory. MemChorus's writer, given a data/home root ``P``, puts the real
data one level deeper, at ``P/palace/chroma.sqlite3`` (MemPalace's own
``DEFAULT_PALACE_PATH`` is ``~/.mempalace/palace`` — the *leaf*). The
August 2026 bug was that a profile's reader ``--palace`` pointed at the
*parent* ``P`` while the data sat at ``P/palace``, so ``status`` /
``search`` / KG all reported 0 rows against the empty shell
``P/chroma.sqlite3``.

Before this module, the parent-vs-leaf rule lived in two places
(``_normalize_palace_args`` in ``mempalace_memory_source`` and the
``auto_init`` writer config) and could silently diverge again.  Everything
that needs to know "where does the real chroma live (or where will the
writer put it?)" now calls :func:`palace_data_dir` /
:func:`palace_data_file` — one function, one answer, so the reader and the
writer agree *by construction* rather than by coincidence.

The public surface is intentionally tiny and side-effect free (it only
reads the filesystem):

- :func:`is_chroma_empty`   — 0-byte / rowless / unreadable => True.
- :func:`chroma_row_count`  — row count of ``embeddings`` (or ``None``).
- :func:`palace_data_dir`   — the ONE resolver (a directory).
- :func:`palace_data_file`  — the ONE resolver (the chroma file path).
- :func:`classify`          — a structured :class:`PalaceLayout` for the
                              doctor report.
- :func:`migrate`           — re-points a reader config at the canonical
                              leaf so the runtime fallback stops firing.

Branch vocabulary used by :func:`classify`:

- ``canonical``    — the resolved dir is where the caller already points
                     and holds the data (the fixed / first-run state).
- ``legacy-leaf``  — caller points at the *parent* while the data sits at
                     ``<parent>/palace`` (the Aug 20 state). The reader
                     must descend; ``--strict`` treats this as a failure
                     until ``migrate`` re-points the config.
- ``fresh``        — neither layout holds a chroma file yet (first run).
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

__all__ = [
    "CHROMA_FILE",
    "PALACE_SUBDIR",
    "BRANCH_CANONICAL",
    "BRANCH_LEGACY_LEAF",
    "BRANCH_FRESH",
    "PalaceLayout",
    "MigrateResult",
    "is_chroma_empty",
    "chroma_row_count",
    "palace_data_dir",
    "palace_data_file",
    "classify",
    "migrate",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fixed leaf filename the reader opens (``mempalace/mcp_server``).
CHROMA_FILE = "chroma.sqlite3"

#: Fixed sub-directory name MemPalace uses for the actual leaf — the
#: directory that directly contains ``chroma.sqlite3``.  MemPalace's own
#: default (``~/.mempalace/palace``) is a leaf named ``palace``; we do not
#: change that default, we just share the name so "canonical leaf" has one
#: meaning across the writer, reader, doctor and tests.
PALACE_SUBDIR = "palace"

# Branch labels — stable strings the doctor, CLI and tests match on.
BRANCH_CANONICAL = "canonical"
BRANCH_LEGACY_LEAF = "legacy-leaf"
BRANCH_FRESH = "fresh"


# ---------------------------------------------------------------------------
# Pure filesystem probes
# ---------------------------------------------------------------------------

def is_chroma_empty(path: Path) -> bool:
    """Return ``True`` if *path* is an "empty shell" ``chroma.sqlite3``.

    Mirrors, byte-for-byte, the semantics of the previous inlined
    ``mempalace_memory_source._chroma_is_empty`` (now a thin delegate), so
    the relocation is observationally identical to callers:

    - file missing               -> ``True``
    - size is 0 (never opened)   -> ``True``
    - valid sqlite, 0 rows, or no
      ``embeddings`` table       -> ``True``
    - unreadable / corrupt       -> ``True`` (conservative — never rewrite
                                    on a file we can't vouch for)
    - valid sqlite, >=1 row      -> ``False`` (there IS data here)
    """
    if not path.exists():
        return True
    try:
        if path.stat().st_size == 0:
            return True
    except OSError:
        return True
    try:
        con = sqlite3.connect(str(path))
        try:
            try:
                n = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
                return n == 0
            except sqlite3.Error:
                # No ``embeddings`` table (or not a valid sqlite header) —
                # treat as an empty shell for the layout decision.
                return True
        finally:
            con.close()
    except Exception:
        return True
    return False


def chroma_row_count(path: Path) -> Optional[int]:
    """Number of rows in *path*'s ``embeddings`` table, else ``None``.

    ``None`` (distinct from ``0``) is an intentional signal for
    "no usable file there yet" — the doctor needs to tell a fresh install
    apart from an empty shell.
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        con = sqlite3.connect(str(path))
        try:
            try:
                return con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            except sqlite3.Error:
                return None
        finally:
            con.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The single resolver
# ---------------------------------------------------------------------------

def palace_data_dir(root: Union[str, Path]) -> Union[str, Path]:
    """The ONE code path that says "where is the chroma directory?".

    Given a configured root (``--palace`` / mempalace home), return the
    directory the **reader and writer must both use** for
    ``chroma.sqlite3``.

    Returns the caller's value **as-given** on the no-descent branch — a
    ``str`` stays a ``str`` and a ``Path`` stays a ``Path`` — and a
    ``Path`` on the canonical-leaf branch.  This is a type-preserving
    passthrough, never a re-normalised :class:`~pathlib.Path`: on the leaf
    branch a fresh ``Path(root)`` carries no extra segments (the root holds
    no data), so ``str(Path("/a/b/c")) == "/a/b/c"`` on the native platform
    and both a string input and a ``Path`` input land on the identical
    result.  That identity is what lets the writer record the configured
    directory *verbatim* instead of ``os.sep``-rewriting it.

    Rules (idempotent, single-level, no path invention — identical to the
    pre-refactor ``_normalize_palace_args`` rewrite rule, extracted so it
    can't drift):

    - ``<root>/palace/chroma.sqlite3`` **exists** AND
      ``<root>/chroma.sqlite3`` is an empty shell (or absent)
      -> return ``<root>/palace`` (the canonical leaf; the Aug 20 case).
    - otherwise (root already holds the data, or this is a fresh install
      with no chroma anywhere) -> return ``<root>`` untouched.

    A fresh install must be returned unchanged: we do not invent a
    ``/palace`` sub-path the reader doesn't yet have
    (``tests.test_palace_path_alignment::test_normalize_leaves_fresh_install_alone``).

    Cross-platform note: the no-descent branch returns the input as-given
    and so is never re-split by the platform separator.  A placeholder like
    ``/opt/mem palace/data`` (a path under ``/opt/`` on a Windows CI box)
    has no ``/palace`` subdirectory, so we must NOT invent one — the
    writer has to record the dir it was given, verbatim.
    """
    probe = Path(root)
    leaf_dir = probe / PALACE_SUBDIR
    leaf_chroma = leaf_dir / CHROMA_FILE
    root_chroma = probe / CHROMA_FILE
    # Only descend when the leaf dir AND its chroma file exist on disk and
    # the root chroma is (or isn't) an empty shell per the contract above.
    # Otherwise fall through and pass the caller's value back as-given.
    if leaf_dir.is_dir() and leaf_chroma.exists() and is_chroma_empty(root_chroma):
        return leaf_dir
    return root


def palace_data_file(root: Union[str, Path]) -> Path:
    """The ONE code path that says "where is the chroma **file**?".

    Returns :func:`palace_data_dir(root) / chroma.sqlite3`.  Both the
    writer (``auto_init`` generated config and the shared MCP transport)
    and the reader (the ``_normalize_palace_args`` shim) use this so they
    cannot disagree about the on-disk location of the data.

    :func:`palace_data_dir` may legitimately return a verbatim ``str``
    (the no-descent case), so the arithmetic is wrapped in ``Path(...)`` to
    guarantee a real :class:`~pathlib.Path` result either way.
    """
    return Path(palace_data_dir(root)) / CHROMA_FILE


# ---------------------------------------------------------------------------
# Structured layout for the doctor
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class PalaceLayout:
    """Outcome of :func:`classify` — the ``--palace-layout`` doctor report."""

    configured_dir: Path
    resolved_dir: Path
    resolved_data_file: Path
    branch: str  # BRANCH_CANONICAL | BRANCH_LEGACY_LEAF | BRANCH_FRESH
    row_count: Optional[int]
    needs_repoint: bool

    def as_dict(self) -> dict:
        return {
            "configured_dir": str(self.configured_dir),
            "resolved_dir": str(self.resolved_dir),
            "resolved_data_file": str(self.resolved_data_file),
            "branch": self.branch,
            "row_count": self.row_count,
            "needs_repoint": self.needs_repoint,
        }


def classify(root: Union[str, Path]) -> PalaceLayout:
    """Produce a :class:`PalaceLayout` for :func:`memchorus-doctor
    --palace-layout`.

    Branching:

    - ``resolved != root``              -> ``legacy-leaf``, ``needs_repoint``
    - ``resolved == root`` and root has data -> ``canonical``
    - ``resolved == root`` and no data         -> ``fresh``

    ``row_count`` is taken from the resolved (leaf) data file so it always
    reflects the corpus the reader will actually open.
    """
    root = Path(root)
    # ``root`` is a concrete :class:`~pathlib.Path` here, so the resolver's
    # no-descent branch returns it as-given (still a ``Path``); the ``Path``
    # wrap is a runtime no-op that keeps the static type honest.
    resolved = Path(palace_data_dir(root))
    resolved_file = resolved / CHROMA_FILE
    root_has_data = (
        (root / CHROMA_FILE).exists() and not is_chroma_empty(root / CHROMA_FILE)
    )
    rows = chroma_row_count(resolved_file)

    if resolved != root:
        branch = BRANCH_LEGACY_LEAF
        needs_repoint = True
    elif root_has_data:
        branch = BRANCH_CANONICAL
        needs_repoint = False
    else:
        branch = BRANCH_FRESH
        needs_repoint = False

    return PalaceLayout(
        configured_dir=root,
        resolved_dir=resolved,
        resolved_data_file=resolved_file,
        branch=branch,
        row_count=rows,
        needs_repoint=needs_repoint,
    )


# ---------------------------------------------------------------------------
# Migration (re-point the reader at the canonical leaf)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class MigrateResult:
    """Outcome of :func:`migrate`."""

    root: Path
    canonical: Path
    needs_repoint: bool
    repointed_configs: List[str]
    dry_run: bool


def _rewrite_palace_token(text: str, root: Path, canonical: Path) -> str:
    """Rewrite ``--palace <root>`` / ``--palace=<root>`` -> ``canonical``.

    Preserves the surrounding argv/token (either space or equals form).
    Only rewrites an exact path match on *root*, so an already-canonical
    or already-leaf value is left alone.
    """
    root_s = str(root)
    canon_s = str(canonical)
    # Space form:  --palace <root>
    text = re.sub(
        r"(--palace\s+)(" + re.escape(root_s) + r")",
        lambda m: m.group(1) + canon_s,
        text,
    )
    # Equals form: --palace=<root>
    text = re.sub(
        r"(--palace=)" + re.escape(root_s),
        lambda m: m.group(1) + canon_s,
        text,
    )
    # env form: MEMPALACE_PALACE_PATH=<root>  (best effort)
    text = re.sub(
        r"(MEMPALACE_PALACE_PATH=)" + re.escape(root_s),
        lambda m: m.group(1) + canon_s,
        text,
    )
    return text


def migrate(
    root: Union[str, Path],
    config_path: Optional[Union[str, Path]] = None,
    *,
    dry_run: bool = False,
) -> MigrateResult:
    """Re-point a reader config at the canonical leaf so the runtime
    fallback (``_normalize_palace_args``) stops firing.

    The canonical layout keeps the data at the leaf (``<root>/palace``) —
    the writer already writes there, and REQUIREMENTS point 1 requires
    ``--palace`` to point at the directory that *contains*
    ``chroma.sqlite3``.  So the durable fix is to make the *reader* point
    at the leaf, not to shuffle the data file around (which would re-split
    the moment the writer added another row).

    Behaviour:

    - ``palace_data_dir(root) == root`` (already canonical / fresh) ->
      ``needs_repoint`` is ``False``, nothing is written.
    - otherwise, if *config_path* is given and exists -> rewrite its
      ``--palace <root>`` / ``--palace=<root>`` token to the canonical
      leaf (space, equals and ``MEMPALACE_PALACE_PATH=`` forms).  The
      rewritten path is recorded in ``repointed_configs``.
    - *config_path* ``None`` (or no token matched) -> the result's
      ``canonical`` field tells the operator the exact dir to point at.

    ``dry_run=True`` computes everything but leaves *config_path* untouched
    (it is still reported in ``repointed_configs`` so a caller can assert
    the intended change).
    """
    root = Path(root)
    # ``root`` is a concrete :class:`~pathlib.Path`, so the resolver returns
    # it as-given on the no-descent branch (a ``Path``); the wrap is a
    # runtime no-op that keeps the ``canonical: Path`` field type honest.
    canonical = Path(palace_data_dir(root))

    if canonical == root:
        return MigrateResult(
            root=root,
            canonical=canonical,
            needs_repoint=False,
            repointed_configs=[],
            dry_run=dry_run,
        )

    repointed: List[str] = []
    cfg = Path(config_path) if config_path is not None else None
    if cfg is not None and cfg.exists():
        before = cfg.read_text()
        after = _rewrite_palace_token(before, root, canonical)
        if after != before and not dry_run:
            cfg.write_text(after)
        if after != before:
            repointed.append(str(cfg))
    elif cfg is not None and not cfg.exists():
        # Unknown config path — don't fabricate a success.
        pass

    return MigrateResult(
        root=root,
        canonical=canonical,
        needs_repoint=True,
        repointed_configs=repointed,
        dry_run=dry_run,
    )
