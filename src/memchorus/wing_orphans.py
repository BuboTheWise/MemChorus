"""Wing-orphan detection and reassignment planning (issue #305).

Where :mod:`memchorus.wing_isolation` (#286) audits the *set* of distinct
wings in a palace (a cross-profile misroute is a small number of stray
rows), #305 is a **row-weighted** problem: the overwhelming majority of a
profile's embedding rows land in wing values that no wing-scoped recall
query will ever target — ISO-timestamp strings, empty / ``None`` wings,
case-variants of canonical keys, and bare slugs that match no profile's
namespace.  Those rows are silently invisible: the user writes them and
can never retrieve them.

This module is the pure, testable core shared by all three #305 surfaces:

* **Doctor** — :func:`orphan_report` turns a row-weighted wing census
  (``{wing: row_count}``) into a structured :class:`OrphanReport` the
  install-doctor renders as PASS / WARN / FAIL.
* **Migration** — :func:`plan_reassignment` produces the concrete
  reassignment plan the ``memchorus-wing-migrate`` tool applies (or
  dry-runs) against the palace SQLite store.
* **Write-time enforcement** — :func:`canonical_target` and
  :func:`classify_wing_value` let the ``add_drawer`` boundary coerce an
  out-of-namespace wing to its canonical valid value so *new* writes can
  no longer strand rows.

Classification categories
-------------------------
``empty``            — ``None`` / ``""`` / ``"None"`` / ``"<None>"`` — unroutable by any query.
``timestamp``        — datetime-like wing (write path fell back to a timestamp).
``case-variant``     — matches a valid wing only case-insensitively (e.g.
                       ``cthugha_learning`` vs ``cthugha_LEARNING``).
``unrecognized``     — matches no profile's namespace (bare slugs, foreign
                       tools, test fixtures).

Every orphan gets a **suggested target**: a case-normalised canonical form
when one exists, otherwise the shared fallback wing
``memchorus_general`` (in every profile's namespace).  The reassignment
repoints *metadata only* — the embeddings table is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from . import wing_isolation

__all__ = [
    "CANONICAL_FALLBACK_WING",
    "ORPHAN_EMPTY",
    "ORPHAN_CASE_VARIANT",
    "ORPHAN_TIMESTAMP",
    "ORPHAN_UNRECOGNIZED",
    "ORPHAN_CATEGORIES",
    "OrphanWing",
    "OrphanReport",
    "is_timestamp_wing",
    "canonical_target",
    "classify_wing_value",
    "orphan_report",
    "plan_reassignment",
    "valid_wings_for_profile",
    "warn_on_orphaned_enabled",
    "enforce_on_write_enabled",
]

#: Shared wing that receives orphan rows when nothing more specific applies.
#: In every profile's namespace (``SHARED_WRITER_WINGS``), so a reassignment
#: to it is always in-namespace for the owning profile.
CANONICAL_FALLBACK_WING = "memchorus_general"

ORPHAN_EMPTY = "empty"
ORPHAN_CASE_VARIANT = "case-variant"
ORPHAN_TIMESTAMP = "timestamp"
ORPHAN_UNRECOGNIZED = "unrecognized"
ORPHAN_CATEGORIES = (
    ORPHAN_EMPTY,
    ORPHAN_CASE_VARIANT,
    ORPHAN_TIMESTAMP,
    ORPHAN_UNRECOGNIZED,
)

#: Wing values the writer family has been seen storing that mean "no wing".
_NULL_TOKENS = frozenset({"", "none", "<none>", "(none)", "null", "nil"})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrphanWing:
    """One orphan wing value and what should happen to its rows."""

    wing: str              # as stored (empty/None normalised to "")
    row_count: int         # embedding rows stranded under this wing
    category: str          # one of ORPHAN_CATEGORIES
    suggested_target: str  # canonical wing to reassign rows to


@dataclass(frozen=True)
class OrphanReport:
    """Row-weighted orphan census for one profile's palace."""

    profile: str
    total_rows: int
    mapped_rows: int
    orphan_rows: int
    orphans: Tuple[OrphanWing, ...]  # sorted by row_count desc, then wing

    @property
    def is_clean(self) -> bool:
        return self.orphan_rows == 0

    @property
    def orphan_pct(self) -> float:
        if not self.total_rows:
            return 0.0
        return 100.0 * self.orphan_rows / self.total_rows

    def summary(self) -> str:
        if self.is_clean:
            return (
                f"{self.profile}: 0 of {self.total_rows} embedding rows "
                f"orphaned — all rows sit in in-namespace wings."
            )
        top = ", ".join(
            f"{o.wing or '<empty>'}({o.row_count}, {o.category} -> {o.suggested_target})"
            for o in self.orphans[:5]
        )
        more = (
            f" (+{len(self.orphans) - 5} more)"
            if len(self.orphans) > 5
            else ""
        )
        return (
            f"{self.profile}: {self.orphan_rows} of {self.total_rows} embedding rows "
            f"({self.orphan_pct:.0f}%) stranded in {len(self.orphans)} orphan wing(s): "
            f"{top}{more}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "total_rows": self.total_rows,
            "mapped_rows": self.mapped_rows,
            "orphan_rows": self.orphan_rows,
            "orphan_pct": round(self.orphan_pct, 2),
            "is_clean": self.is_clean,
            "orphans": [
                {
                    "wing": o.wing,
                    "row_count": o.row_count,
                    "category": o.category,
                    "suggested_target": o.suggested_target,
                }
                for o in self.orphans
            ],
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def is_timestamp_wing(wing: Any) -> bool:
    """Return True when *wing* is a datetime-like string.

    The #305 write path sometimes falls back to a timestamp as the wing key
    instead of a category.  We match ISO-formatted values (``2026-11-20T10:17:03``,
    ``2026-11-20 10:17``, ``2026-11-20``, ``20261120`` and common variants) —
    deliberately conservative: a wing must start with a 4-digit year and
    contain only date/time characters.
    """
    s = str(wing or "").strip()
    if not (8 <= len(s) <= 40):
        return False
    if not s[:4].isdigit():
        return False
    if not all(c.isalnum() or c in "-T: ." for c in s):
        return False
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H",
        "%Y-%m-%d %H",
        "%Y-%m-%d",
        "%Y%m%d",
    ):
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    # Fractional seconds / timezone offsets
    try:
        datetime.fromisoformat(s)
        return True
    except ValueError:
        return False
    return False


def canonical_target(wing: Any, valid_wings: FrozenSet[str]) -> str:
    """Return the valid wing value to use for a raw wing *wing*.

    Resolution order:

    1. Exact valid value — returned as-is (no change).
    2. Case-insensitive match against a valid wing — canonical form
       (``cthugha_learning`` -> ``cthugha_LEARNING``).
    3. Otherwise — :data:`CANONICAL_FALLBACK_WING` when it is in the valid
       set, else the first sorted valid wing.

    Never raises; on an empty valid set returns the empty string.
    """
    s = str(wing or "").strip()
    if s in valid_wings:
        return s
    if s.lower() in _NULL_TOKENS:
        s = ""
    for v in sorted(valid_wings):
        if v.lower() == s.lower():
            return v
    if CANONICAL_FALLBACK_WING in valid_wings:
        return CANONICAL_FALLBACK_WING
    return next(iter(sorted(valid_wings)), "")


def classify_wing_value(
    wing: Any,
    valid_wings: FrozenSet[str],
) -> Optional[OrphanWing]:
    """Classify a stored wing value.

    Returns ``None`` when the wing is in-namespace (mapped), otherwise an
    :class:`OrphanWing` (with ``row_count=0`` — the caller fills in the
    census count) carrying the orphan category and the suggested target.
    """
    s = str(wing).strip() if wing is not None else ""
    if s in valid_wings:
        return None
    target = canonical_target(s, valid_wings)
    if s.lower() in _NULL_TOKENS:
        category = ORPHAN_EMPTY
    elif is_timestamp_wing(s):
        category = ORPHAN_TIMESTAMP
    elif any(v.lower() == s.lower() for v in valid_wings):
        category = ORPHAN_CASE_VARIANT
    else:
        category = ORPHAN_UNRECOGNIZED
    return OrphanWing(wing=s, row_count=0, category=category, suggested_target=target)


def orphan_report(
    profile: str,
    wing_row_counts: Mapping[Any, int],
    valid_wings: Optional[FrozenSet[str]] = None,
) -> OrphanReport:
    """Build a :class:`OrphanReport` from a row-weighted wing census.

    *wing_row_counts* maps each stored wing value to its embedding-row count
    (as returned by a read-only ``GROUP BY`` over ``embedding_metadata``).
    Wing keys of ``None`` are normalised to ``""`` (empty) before
    classification.  *valid_wings* defaults to
    :func:`valid_wings_for_profile` for *profile* (the code-derived
    namespace union the profile's ``memchorus.yaml`` wing_map).
    """
    if valid_wings is None:
        valid_wings = valid_wings_for_profile(profile)
    total = 0
    mapped = 0
    orphans: List[OrphanWing] = []
    # Merge census keys that normalise to the same canonical key (e.g. a
    # caller may hand us both ``None`` and ``""``) so row counts are summed,
    # never lost to a dict-collision.
    merged: Dict[str, int] = {}
    for wing, count in wing_row_counts.items():
        try:
            n = max(0, int(count))
        except (TypeError, ValueError):
            n = 0
        key = "" if wing is None else str(wing).strip()
        merged[key] = merged.get(key, 0) + n
    for key, n in merged.items():
        info = classify_wing_value(key, valid_wings)
        if info is None:
            mapped += n
            total += n
            continue
        orphans.append(
            OrphanWing(wing=key, row_count=n, category=info.category,
                       suggested_target=info.suggested_target)
        )
        total += n
    orphans.sort(key=lambda o: (-o.row_count, o.wing))
    orphan_rows = sum(o.row_count for o in orphans)
    return OrphanReport(
        profile=profile,
        total_rows=total,
        mapped_rows=mapped,
        orphan_rows=orphan_rows,
        orphans=tuple(orphans),
    )


def plan_reassignment(report: OrphanReport) -> List[Dict[str, Any]]:
    """Concrete reassignment plan — one entry per non-empty orphan wing.

    Each entry is ``{"source", "target", "rows", "category"}`` and is
    directly consumable by the migration tool (repoint the ``wing`` metadata
    value *source* -> *target* for *rows* rows).
    """
    plan: List[Dict[str, Any]] = []
    for o in report.orphans:
        if o.row_count <= 0:
            continue
        plan.append({
            "source": o.wing,
            "target": o.suggested_target,
            "rows": o.row_count,
            "category": o.category,
        })
    return plan


# ---------------------------------------------------------------------------
# Config flags (issue #305 — doctor + write-time enforcement gates)
# ---------------------------------------------------------------------------
def _wing_isolation_config(profile: str) -> Dict[str, Any]:
    """Read the merged ``wing_isolation:`` section from memchorus.yaml.

    Profile overrides win over global (mirrors the auto_bootstrap cascade).
    Never raises — returns ``{}`` on any failure so callers can fall back to
    their defaults.
    """
    section: Dict[str, Any] = {}
    try:
        from . import auto_bootstrap
        loaders = (
            lambda: auto_bootstrap._load_yaml_config(),
            lambda: auto_bootstrap._load_profile_yaml_config(profile),
        )
        # Merge global first, profile second (profile wins).
        for loader in loaders:
            try:
                data = loader()
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            cfg = data.get("wing_isolation")
            if isinstance(cfg, dict):
                section.update(cfg)
    except Exception:
        pass
    return section


def warn_on_orphaned_enabled(profile: str) -> bool:
    """``wing_isolation.warn_on_orphaned`` flag (default: ``True``).

    When disabled the doctor still reports orphan stats but returns PASS.
    """
    return bool(_wing_isolation_config(profile).get("warn_on_orphaned", True))


def enforce_on_write_enabled(profile: str) -> bool:
    """``wing_isolation.enforce_on_write`` flag (default: ``True``).

    When disabled the write-boundary guard degrades to detection-only
    (issue #286 behaviour): log a warning, write the wing as-is.
    """
    return bool(_wing_isolation_config(profile).get("enforce_on_write", True))


def valid_wings_for_profile(profile: str) -> FrozenSet[str]:
    """The full set of ``wing`` values that are routable for *profile*.

    Issue #305's acceptance is ">70% coverage of each profile's primary
    wings", where "primary wings" are the ones wing-scoped recall actually
    queries.  A wing is routable for a profile when it is either:

    * in the code-derived namespace —
      :func:`memchorus.wing_isolation.expected_wings` (the shared
      ``memchorus_*`` writer family, ``self-improvement``, legacy bare
      category keys, and the profile's own bare / ``wing_`` / prefixed
      forms), or
    * declared in the profile's ``memchorus.yaml``
      ``mempalace_config.mempalace_routing.wing_map`` — as a **key** (the
      wing name the writer routes to) or as a **string value** (a wing
      alias the config references).

    The union is the correct "valid" set for both the doctor census and the
    write-time guard: a wing outside it is, by definition, unreachable by
    wing-scoped recall for this profile — i.e. an orphan.  Config loading is
    best-effort: a missing/malformed ``memchorus.yaml`` falls back to the
    code-derived namespace alone.
    """
    try:
        import memchorus.wing_isolation as _wi

        valid = set(_wi.expected_wings(profile))
    except Exception:  # pragma: no cover — defensive
        valid = set()

    try:
        from . import auto_bootstrap

        configs: List[Dict[str, Any]] = []
        try:
            data = auto_bootstrap._load_profile_yaml_config(profile)
            if isinstance(data, dict):
                configs.append(data)
        except Exception:
            pass
        try:
            data = auto_bootstrap._load_yaml_config()
            if isinstance(data, dict):
                configs.append(data)
        except Exception:
            pass
        for cfg in configs:
            mp = cfg.get("mempalace_config")
            if not isinstance(mp, dict):
                continue
            routing = mp.get("mempalace_routing")
            if not isinstance(routing, dict):
                continue
            wm = routing.get("wing_map")
            if not isinstance(wm, dict):
                continue
            for key, values in wm.items():
                if isinstance(key, str) and key.strip():
                    valid.add(key.strip())
                if isinstance(values, str) and values.strip():
                    valid.add(values.strip())
                elif isinstance(values, (list, tuple)):
                    for v in values:
                        if isinstance(v, str) and v.strip():
                            valid.add(v.strip())
    except Exception:  # pragma: no cover — defensive: fall back to code set
        pass

    return frozenset(valid)
