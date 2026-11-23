"""Wing-orphan migration (issue #305).

Repoints the ``wing`` metadata of orphan rows (timestamp / empty /
case-variant / unrecognized wing values) in a profile's palace SQLite store
to canonical in-namespace wings — **without touching the ``embeddings``
table** (vector data is byte-identical before and after).

Safety properties
-----------------
* **Read-only census first** — the plan is computed from a ``GROUP BY`` over
  ``embedding_metadata`` before anything is written.
* **Backup** — ``chroma.sqlite3`` is copied to ``chroma.sqlite3.bak-<ts>``
  in the same directory before the apply (disable with ``--no-backup``).
* **Idempotent** — after an apply, a re-run finds zero rows to move.
* **Dry-run by default** — ``--apply`` is required to write.
* **Metadata-only writes** — a single
  ``UPDATE embedding_metadata SET string_value = ...`` per orphan wing.

Usage (console script ``memchorus-wing-migrate``)::

    memchorus-wing-migrate                 # dry-run on the active profile
    memchorus-wing-migrate --json          # machine-readable dry-run
    memchorus-wing-migrate --apply         # apply the reassignment
    memchorus-wing-migrate --profile cthugha --apply
    memchorus-wing-migrate --palace-dir ~/.hermes/profiles/cthugha/.mempalace

Exit codes: 0 = nothing to do / apply clean; 1 = apply left orphans;
2 = resolution or apply error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import wing_orphans
from .hermes_home import active_profile_name, hermes_home, profile_home

__all__ = [
    "MigrationAction",
    "MigrationResult",
    "wing_row_counts",
    "resolve_palace_dir",
    "plan_migration",
    "apply_migration",
    "main",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MigrationAction:
    """One repoint: source wing -> target wing for N rows."""

    source_wing: str
    target_wing: str
    row_count: int
    category: str


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of a dry-run or apply."""

    profile: str
    palace_dir: str
    dry_run: bool
    total_rows: int
    orphan_rows_before: int
    orphan_rows_after: int
    actions: Tuple[MigrationAction, ...]
    backup_path: Optional[str] = None
    error: Optional[str] = None

    @property
    def rows_moved(self) -> int:
        return sum(a.row_count for a in self.actions)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "palace_dir": self.palace_dir,
            "dry_run": self.dry_run,
            "total_rows": self.total_rows,
            "orphan_rows_before": self.orphan_rows_before,
            "orphan_rows_after": self.orphan_rows_after,
            "rows_moved": self.rows_moved,
            "backup_path": self.backup_path,
            "error": self.error,
            "actions": [
                {
                    "source": a.source_wing,
                    "target": a.target_wing,
                    "rows": a.row_count,
                    "category": a.category,
                }
                for a in self.actions
            ],
        }

    def human_summary(self) -> str:
        lines: List[str] = []
        mode = "DRY RUN" if self.dry_run else "APPLY"
        lines.append(f"wing-orphan migration — {mode} (profile: {self.profile})")
        lines.append(f"palace: {self.palace_dir}")
        if self.error:
            lines.append(f"ERROR: {self.error}")
            return "\n".join(lines)
        if self.total_rows == 0:
            lines.append("palace has no wing metadata rows — nothing to do.")
            return "\n".join(lines)
        lines.append(
            f"rows: {self.orphan_rows_before} of {self.total_rows} "
            f"({100.0 * self.orphan_rows_before / self.total_rows:.0f}%) orphaned"
            if self.total_rows
            else "rows: 0 of 0 orphaned"
        )
        if not self.actions:
            lines.append("no orphan wings detected — store is clean.")
            return "\n".join(lines)
        lines.append("")
        lines.append(f"reassignment plan ({len(self.actions)} wing(s)):")
        for a in self.actions:
            src = a.source_wing or "<empty>"
            lines.append(
                f"  {src}  ->  {a.target_wing}   [{a.category}, {a.row_count} rows]"
            )
        if self.dry_run:
            lines.append("")
            lines.append(
                "dry run only — no changes written. Re-run with --apply to "
                "reassign (a backup is made first)."
            )
        else:
            lines.append("")
            if self.backup_path:
                lines.append(f"backup: {self.backup_path}")
            lines.append(
                f"orphan rows after: {self.orphan_rows_after} "
                f"(moved {self.rows_moved})"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Palace resolution + read-only census
# ---------------------------------------------------------------------------
def _config_data_dir(profile: str) -> Optional[Path]:
    """A configured ``data_dir`` (profile overrides global), if it holds a store."""
    try:
        from . import auto_bootstrap
        global_cfg = auto_bootstrap._load_yaml_config()
        profile_cfg = auto_bootstrap._load_profile_yaml_config(profile)
    except Exception:
        return None
    data_dir = (
        profile_cfg.get("data_dir")
        or global_cfg.get("data_dir")
    )
    if not data_dir:
        return None
    candidate = Path(str(data_dir)).expanduser()
    if (candidate / "chroma.sqlite3").exists():
        return candidate
    if (candidate / "palace" / "chroma.sqlite3").exists():
        return candidate / "palace"
    return None


def resolve_palace_dir(profile: Optional[str] = None) -> Optional[Path]:
    """Resolve the palace directory (the dir holding ``chroma.sqlite3``).

    Order: configured ``data_dir`` -> the profile's ``.mempalace`` tree
    -> the global ``~/.hermes/.mempalace`` tree (shared store).
    Returns ``None`` when no live store is found.
    """
    name = profile or active_profile_name()
    configured = _config_data_dir(name)
    if configured is not None:
        return configured
    home = hermes_home()
    if name == "default":
        bases = [home / ".mempalace"]
    else:
        bases = [profile_home(name) / ".mempalace", home / ".mempalace"]
    for base in bases:
        for cand in (base, base / "palace"):
            if (cand / "chroma.sqlite3").exists():
                return cand
    return None


def wing_row_counts(palace_dir: Path) -> Dict[str, int]:
    """Row-weighted wing census (read-only).

    Maps each stored wing value to its embedding-row count via
    ``GROUP BY`` over ``embedding_metadata``.  ``None`` values are
    reported under the ``""`` key so the classifier sees them as empty.
    Returns ``{}`` when the store is missing or unreadable.
    """
    chroma = palace_dir / "chroma.sqlite3"
    if not chroma.exists():
        chroma = palace_dir / "palace" / "chroma.sqlite3"
    if not chroma.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{chroma}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute(
            "SELECT COALESCE(string_value, ''), COUNT(*) "
            "FROM embedding_metadata WHERE key = 'wing' "
            "GROUP BY COALESCE(string_value, '')"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {w: n for w, n in rows}


# ---------------------------------------------------------------------------
# Planning + apply
# ---------------------------------------------------------------------------
def plan_migration(
    profile: str,
    wing_row_counts: Dict[str, int],
) -> Tuple[wing_orphans.OrphanReport, Tuple[MigrationAction, ...]]:
    """Compute the orphan report and the concrete reassignment actions."""
    report = wing_orphans.orphan_report(profile, wing_row_counts)
    actions = tuple(
        MigrationAction(
            source_wing=o.wing,
            target_wing=o.suggested_target,
            row_count=o.row_count,
            category=o.category,
        )
        for o in report.orphans
        if o.row_count > 0
    )
    return report, actions


def apply_migration(
    palace_dir: Optional[Path] = None,
    profile: Optional[str] = None,
    dry_run: bool = True,
    make_backup: bool = True,
) -> MigrationResult:
    """Run the migration (dry-run by default).

    On apply, each orphan wing is repointed with a single
    ``UPDATE embedding_metadata SET string_value = <target> WHERE key='wing'
    AND string_value = <source>`` (empty sources match
    ``string_value IS NULL OR TRIM(string_value) = ''``).  The
    ``embeddings`` table is never written.
    """
    name = profile or active_profile_name()
    dir_ = palace_dir or resolve_palace_dir(name)
    if dir_ is None:
        return MigrationResult(
            profile=name,
            palace_dir="<unresolved>",
            dry_run=dry_run,
            total_rows=0,
            orphan_rows_before=0,
            orphan_rows_after=0,
            actions=(),
            error="could not resolve a palace directory with chroma.sqlite3",
        )
    dir_ = Path(dir_)
    counts = wing_row_counts(dir_)
    if not counts:
        return MigrationResult(
            profile=name,
            palace_dir=str(dir_),
            dry_run=dry_run,
            total_rows=0,
            orphan_rows_before=0,
            orphan_rows_after=0,
            actions=(),
        )
    report, actions = plan_migration(name, counts)
    if not actions or dry_run:
        return MigrationResult(
            profile=name,
            palace_dir=str(dir_),
            dry_run=True,
            total_rows=report.total_rows,
            orphan_rows_before=report.orphan_rows,
            orphan_rows_after=report.orphan_rows,
            actions=actions,
        )

    chroma = dir_ / "chroma.sqlite3"
    backup_path: Optional[str] = None
    if make_backup and chroma.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = chroma.with_name(f"chroma.sqlite3.bak-{ts}")
        try:
            shutil.copy2(chroma, backup)
            backup_path = str(backup)
        except OSError as exc:
            return MigrationResult(
                profile=name,
                palace_dir=str(dir_),
                dry_run=False,
                total_rows=report.total_rows,
                orphan_rows_before=report.orphan_rows,
                orphan_rows_after=report.orphan_rows,
                actions=actions,
                error=f"backup failed: {exc}",
            )
    try:
        con = sqlite3.connect(str(chroma), timeout=30)
        try:
            con.execute("BEGIN IMMEDIATE")
            for a in actions:
                if a.source_wing == "":
                    cur = con.execute(
                        "UPDATE embedding_metadata SET string_value = ? "
                        "WHERE key = 'wing' "
                        "AND (string_value IS NULL OR TRIM(string_value) = '')",
                        (a.target_wing,),
                    )
                else:
                    cur = con.execute(
                        "UPDATE embedding_metadata SET string_value = ? "
                        "WHERE key = 'wing' AND string_value = ?",
                        (a.target_wing, a.source_wing),
                    )
                if cur.rowcount != a.row_count:
                    # Census and apply raced, or a partial match — report it.
                    return MigrationResult(
                        profile=name,
                        palace_dir=str(dir_),
                        dry_run=False,
                        total_rows=report.total_rows,
                        orphan_rows_before=report.orphan_rows,
                        orphan_rows_after=report.orphan_rows,
                        actions=actions,
                        backup_path=backup_path,
                        error=(
                            f"expected to repoint {a.row_count} row(s) for "
                            f"wing {a.source_wing!r} but the UPDATE touched "
                            f"{cur.rowcount}; rolling back"
                        ),
                    )
            con.commit()
        except sqlite3.Error as exc:
            try:
                con.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
    except sqlite3.Error as exc:
        return MigrationResult(
            profile=name,
            palace_dir=str(dir_),
            dry_run=False,
            total_rows=report.total_rows,
            orphan_rows_before=report.orphan_rows,
            orphan_rows_after=report.orphan_rows,
            actions=actions,
            backup_path=backup_path,
            error=f"apply failed: {exc}",
        )

    after = wing_row_counts(dir_)
    report_after = wing_orphans.orphan_report(name, after)
    return MigrationResult(
        profile=name,
        palace_dir=str(dir_),
        dry_run=False,
        total_rows=report.total_rows,
        orphan_rows_before=report.orphan_rows,
        orphan_rows_after=report_after.orphan_rows,
        actions=actions,
        backup_path=backup_path,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memchorus-wing-migrate",
        description=(
            "Repoint orphan wing metadata (issue #305) in a profile's palace "
            "SQLite store. Dry-run by default; --apply writes. The embeddings "
            "table is never modified."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="profile to migrate (default: the active profile)",
    )
    parser.add_argument(
        "--palace-dir",
        default=None,
        help="explicit palace directory holding chroma.sqlite3",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the reassignment (default is dry-run)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the pre-apply chroma.sqlite3 backup",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    args = parser.parse_args(argv)

    palace_dir = Path(args.palace_dir).expanduser() if args.palace_dir else None
    result = apply_migration(
        palace_dir=palace_dir,
        profile=args.profile,
        dry_run=not args.apply,
        make_backup=not args.no_backup,
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(result.human_summary())
    if result.error:
        return 2
    if not result.dry_run and result.orphan_rows_after > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
