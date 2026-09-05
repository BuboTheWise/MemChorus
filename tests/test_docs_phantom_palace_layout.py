"""Docs grep-regression lock: the phantom palace layout must stay gone.

IMPL #172 acceptance: "add a docs grep-regression test that fails if the
phantom `workspace/mempalace` path reappears."

Background: MemChorus once documented the per-profile palace as
``profiles/<name>/workspace/mempalace/palace/`` — a layout that no install
ever used and that never existed on disk.  The canonical leaf is
``profiles/<name>/.mempalace/palace/`` (stated in
``docs/ARCHITECTURE.md``).  This test walks the public human-facing docs and
fails if ``workspace/mempalace`` re-appears as a literal path, so the
documentation drift is caught at the grep level, not by an operator hitting
a silently-empty vault.

Why only docs + README (not the whole repo):

* ``src/`` legitimately *names* the old path in comments and in the
  historical anatomy table, and the historical REQUIREMENTS row describes
  the bug the fix resolved — that is intentional context, not a live layout
  claim.
* ``tests/`` legitimately *assert* against the phantom string (this very
  lock names it in its own docstring and the existing path-alignment tests
  reference the layout they pin), so including test files in the exclusion
  scope is the one place it has to appear to be asserted against.

We therefore scan the **human-facing documentation surface** — every
``*.md`` under the repo root, ``README.md`` included — and treat any match
of ``workspace/mempalace`` as a regression.  We deliberately exclude
``tests/`` (the grep lock is *about* that string) and ``.git`` / ``build``
(noise).

The phrase must not be the raw path *in live prose*.  Test assertions and
historical anatomy are allowed — this test scans docs, not tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The exact phrase the grep lock looks for.  This is the path that no install
# ever used and that the fix removed from the docs.  If you are reading this
# test, this string is the phrase you must NOT see in any human-facing doc.
_PHANTOM_PALACE_PATH = "workspace/mempalace"

# Directories we deliberately do NOT scan:
#   - tests/   : the grep lock + path-alignment tests are allowed to name the
#                phantom path in their own assertions (that is the point of
#                asserting against it).
#   - build/   : packaging artifact from a prior install — noise.
#   - .git/    : noise.
_EXCLUDE_DIRS = {".git", "build", "tests", "node_modules", "dist"}

# Human-facing doc files we MUST scan.
_DOC_SUFFIXES = {".md"}


def _iter_doc_files(repo_root: Path):
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in _DOC_SUFFIXES:
            continue
        parts = path.relative_to(repo_root).parts
        if any(part in _EXCLUDE_DIRS for part in parts[:-1]):
            continue
        yield path


def test_no_phantom_palace_path_in_human_facing_docs() -> None:
    """The live docs must not reintroduce the phantom palace layout.

    Scans every ``*.md`` file at the repo root and under non-test
    directories; any occurrence of ``workspace/mempalace`` is a regression
    of the layout drift this IMPL closed.
    """
    repo_root = _find_repo_root()
    offenders: list[str] = []

    for doc in _iter_doc_files(repo_root):
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PHANTOM_PALACE_PATH in line:
                # We deliberately use .mempalace/palace/ (canonical) or the
                # <profile> placeholder form in live docs.  The phantom path
                # is a bug, not a layout.
                rel = doc.relative_to(repo_root)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "docs reintroduced the phantom palace layout "
        f"'{_PHANTOM_PALACE_PATH}' — the canonical leaf is "
        "profiles/<name>/.mempalace/palace/.  Offenders:\n" + "\n".join(offenders)
    )


def test_canonical_leaf_is_present_in_canonical_docs() -> None:
    """Positive control: the *correct* path (the canonical leaf) IS named in
    the public docs we depend on.  This keeps the grep lock from degrading
    into a 'we just deleted the whole layout' vacuous pass — we must still
    point operators at the right place.

    The canonical leaf is documented in:
    * README.md (Known on-disk layout section)
    * docs/ARCHITECTURE.md (per-profile palaces table row)
    * docs/ONBOARDING.md (MEMPALACE_PALACE_PATH examples)

    At least one of these must still name the canonical path explicitly.
    """
    repo_root = _find_repo_root()
    canonical = ".mempalace/palace"

    checked = [
        repo_root / "README.md",
        repo_root / "docs" / "ARCHITECTURE.md",
        repo_root / "docs" / "ONBOARDING.md",
    ]
    found_anywhere = False
    for f in checked:
        if not f.is_file():
            continue
        if canonical in f.read_text(encoding="utf-8", errors="replace"):
            found_anywhere = True
            break

    assert found_anywhere, (
        "at least one of README.md / docs/ARCHITECTURE.md / docs/ONBOARDING.md "
        f"must still name the canonical leaf '{canonical}' — check each file"
    )


def _find_repo_root() -> Path:
    """Locate the MemChorus repo root from this file's position."""
    here = Path(__file__).resolve()
    # This file lives in tests/ at the repo root, so the parent is the root.
    root = here.parent.parent
    for marker in ("pyproject.toml", "setup.py", "README.md"):
        if (root / marker).exists():
            return root
    raise FileNotFoundError(
        f"could not locate MemChorus repo root from {here}; "
        "expected a pyproject.toml / README.md two levels up"
    )
