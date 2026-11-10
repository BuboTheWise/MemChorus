"""Wing-namespace isolation audit (issue #286).

Pure, testable detection logic that decides whether a profile-scoped MemPalace
palace contains *foreign* wings — rows whose ``wing`` value was not written by
this profile's own MemChorus instance.

Background (issue #286)
-----------------------
MemChorus writes a fixed set of *native* wings per profile
(:data:`NATIVE_WINGS`): the shared ``memchorus_*`` writer family (every profile
writes the same four — see
:data:`memchorus.auto_bootstrap._DEFAULT_MEMPALACE_ROUTING`), the canonical
``self-improvement`` wing, the legacy ``DECISION`` / ``LEARNING`` / ``MISTAKE``
/ ``RESULT`` category keys (palaces written by a pre-#268 install), and a
named profile additionally owns ``wing_<profile>`` — its own cross-profile
wing.  That keeps the profiles' data disjoint even when they share one palace
directory.

The bug in #286: a row whose ``wing`` was a bare project slug (``aletheia``) —
a value that belongs to *no* profile's MemChorus namespace — landed inside a
profile-scoped palace.  From the string shape alone we cannot always tell a
genuine cross-profile misroute from a legitimate shared wing that another tool
owns (``self-improvement``, ``void-scanner-nethervoid``), so this module is a
**detection / audit** surface, not an enforcement gate.  It surfaces every
foreign wing for human review; the install-doctor decides PASS / WARN / FAIL and
``--strict`` controls escalation.

Design notes
------------
* **default is the shared profile.**  Its palace is expected to hold wings from
  many sources, so the audit treats it as *un-scoped* and does not flag foreign
  wings there (that is the "default → PASS (no scoping)" behaviour validated
  against the real host).  Named profiles are *scoped*: their palaces should
  contain only their own namespaced wings.
* **Two foreign categories.**  A foreign wing that matches a *known sibling
  profile's* namespace is ``cross-profile`` (a misroute between profiles).  A
  foreign wing that matches *no* known profile's namespace is
  ``out-of-namespace`` — a bare project slug or a wing owned by another tool.
  Both are reported; the doctor surfaces them for review rather than deleting
  anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, List, Optional, Set

__all__ = [
    "NATIVE_WINGS",
    "expected_wings",
    "WingIssue",
    "WingAudit",
    "audit_palace_wings",
    "classify_wing",
]

# The canonical MemChorus writer wings.  The live routing
# (:data:`memchorus.auto_bootstrap._DEFAULT_MEMPALACE_ROUTING` and
# :data:`memchorus.corpus_balancer.DEFAULT_SETTLED_WINGS`) maps every category
# onto this shared ``memchorus_*`` family — it is *not* profile-prefixed; the
# same four wings are written by every profile.  Kept in lock-step with the
# writer's source of truth so the audit's "in-namespace" notion cannot drift.
SHARED_WRITER_WINGS: FrozenSet[str] = frozenset({
    "memchorus_decisions",
    "memchorus_learning",
    "memchorus_general",
    "memchorus_workspace",
})

# A small set of wings MemChorus itself treats as settled/legitimately shared
# (they appear in ``DEFAULT_SETTLED_WINGS``).  A profile that owns one of these
# should not have it flagged as foreign.
SHARED_CANONICAL_WINGS: FrozenSet[str] = frozenset({
    "self-improvement",
})

# Legacy category keys the older ``auto_init._WING_MAP_TEMPLATE`` used, kept
# for backward-compatibility so a palace written by a pre-#268 install is not
# misclassified as foreign by a profile that wrote it.
LEGACY_CATEGORY_WINGS: FrozenSet[str] = frozenset({
    "DECISION",
    "LEARNING",
    "MISTAKE",
    "RESULT",
})

#: Convenience union — the full set a single profile's MemChorus may write.
NATIVE_WINGS: FrozenSet[str] = (
    SHARED_WRITER_WINGS | SHARED_CANONICAL_WINGS | LEGACY_CATEGORY_WINGS
)


def _norm_profile(profile: Optional[str]) -> str:
    name = (profile or "").strip()
    return name or "default"


def expected_wings(profile: Optional[str]) -> FrozenSet[str]:
    """Return the set of ``wing`` values a profile's own MemChorus writes.

    Every profile writes the shared ``memchorus_*`` family, the canonical
    ``self-improvement`` wing, and the legacy category keys (for palaces
    written by a pre-#268 install).  In addition a named profile legitimately
    owns ``wing_<profile>`` — its own cross-profile wing — so that is in
    namespace for that profile and *only* that profile.  ``default`` is the
    shared profile and simply owns the shared set.

    Mirrors :data:`memchorus.auto_bootstrap._DEFAULT_MEMPALACE_ROUTING` and
    :data:`memchorus.corpus_balancer.DEFAULT_SETTLED_WINGS` so the audit's
    "in-namespace" notion cannot drift from the writer's.
    """
    name = _norm_profile(profile)
    wings = set(NATIVE_WINGS)
    if name != "default":
        wings.add(f"wing_{name}")
    return frozenset(wings)


@dataclass(frozen=True)
class WingIssue:
    """One foreign wing found inside a profile-scoped palace."""

    wing: str
    kind: str                 # "cross-profile" | "out-of-namespace"
    owner: Optional[str]      # the known profile that owns it (cross-profile)
    detail: str

    def as_dict(self) -> dict:
        return {
            "wing": self.wing,
            "kind": self.kind,
            "owner": self.owner,
            "detail": self.detail,
        }


@dataclass
class WingAudit:
    """Outcome of :func:`audit_palace_wings`."""

    profile: str
    scoped: bool
    total_wings: int
    own_wings: List[str] = field(default_factory=list)
    issues: List[WingIssue] = field(default_factory=list)

    # -- derived properties -------------------------------------------------
    @property
    def is_clean(self) -> bool:
        """True when no foreign wings were found."""
        return not self.issues

    @property
    def has_leaks(self) -> bool:
        """Alias for the audit's "something foreign is present" signal."""
        return bool(self.issues)

    @property
    def cross_profile_count(self) -> int:
        return sum(1 for i in self.issues if i.kind == "cross-profile")

    @property
    def out_of_namespace_count(self) -> int:
        return sum(1 for i in self.issues if i.kind == "out-of-namespace")

    @property
    def summary(self) -> str:
        if not self.scoped:
            return (
                f"{self.profile}: shared profile — {self.total_wings} wing(s) "
                f"observed, all expected in an un-scoped (default) palace."
            )
        if self.is_clean:
            return (
                f"{self.profile}: all {self.total_wings} wing(s) are within its "
                f"MemChorus namespace — no foreign wings."
            )
        cp = self.cross_profile_count
        oon = self.out_of_namespace_count
        parts = [
            f"{self.profile}: {self.total_wings} wing(s) observed, "
            f"{len(self.own_wings)} in-namespace, {len(self.issues)} foreign"
        ]
        if cp:
            parts.append(f"{cp} cross-profile")
        if oon:
            parts.append(f"{oon} out-of-namespace")
        return "; ".join(parts) + "."

    @property
    def suggested_fix(self) -> str:
        if self.is_clean:
            return ""
        return (
            "Re-file the foreign wings under the owning profile's palace, or add "
            "the producing tool to that profile's wing map so its namespace "
            "includes them. Detection only — nothing was moved (see issue #286)."
        )

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "scoped": self.scoped,
            "total_wings": self.total_wings,
            "own_wings": sorted(self.own_wings),
            "cross_profile_count": self.cross_profile_count,
            "out_of_namespace_count": self.out_of_namespace_count,
            "issues": [i.as_dict() for i in self.issues],
            "summary": self.summary,
            "suggested_fix": self.suggested_fix,
        }


def audit_palace_wings(
    wings: Iterable[str],
    active_profile: Optional[str],
    known_profiles: Optional[Iterable[str]] = None,
    scoped: Optional[bool] = None,
) -> WingAudit:
    """Audit a palace's distinct wings for foreign (misrouted) entries.

    Parameters
    ----------
    wings:
        The distinct ``wing`` values currently stored in the palace (as
        returned by a read-only Chroma query or a directory listing).
    active_profile:
        The profile whose palace is under audit (e.g. ``"cthugha"``).
        ``None`` is treated as ``"default"``.
    known_profiles:
        The set of Hermes profile names that exist on this host.  Used to tell a
        *cross-profile* misroute (a wing that belongs to a known sibling
        profile's namespace) from an *out-of-namespace* wing (a bare slug or a
        value no profile owns).  ``None`` / empty means "no sibling profiles
        known" — every foreign wing is then reported as out-of-namespace.
    scoped:
        Whether to flag foreign wings at all.  Defaults to ``True`` for named
        profiles and ``False`` for ``default`` (the shared profile, whose
        palace legitimately holds many sources).  Pass explicitly to override.

    Returns
    -------
    WingAudit
        A structured, serialisable result.  Never raises on a valid input set —
        this is a pure classifier so tests can drive it with synthetic wings.
    """
    name = _norm_profile(active_profile)
    if scoped is None:
        scoped = name != "default"

    wing_set: Set[str] = {w for w in wings if w and str(w).strip()}
    wing_set = {str(w).strip() for w in wing_set}
    own = expected_wings(name)

    own_wings = sorted(w for w in wing_set if w in own)

    if not scoped:
        # Shared profile: foreign wings are expected, report clean.
        return WingAudit(
            profile=name,
            scoped=False,
            total_wings=len(wing_set),
            own_wings=own_wings,
            issues=[],
        )

    known = {(p or "").strip() for p in (known_profiles or ())
             if p and (p or "").strip() and (p or "").strip() != name}

    issues: List[WingIssue] = []
    for w in sorted(wing_set):
        if w in own:
            continue
        owner = None
        for q in sorted(known):
            if w in expected_wings(q):
                owner = q
                break
        if owner is not None:
            issues.append(WingIssue(
                wing=w,
                kind="cross-profile",
                owner=owner,
                detail=(
                    f"wing '{w}' is written by profile '{owner}', not '{name}' — "
                    f"likely misrouted into {name}'s palace."
                ),
            ))
        else:
            issues.append(WingIssue(
                wing=w,
                kind="out-of-namespace",
                owner=None,
                detail=(
                    f"wing '{w}' matches no known profile's MemChorus namespace — "
                    f"a bare project slug or a wing owned by another tool. "
                    f"Cannot be distinguished from a legitimate shared wing by "
                    f"string shape alone (issue #286)."
                ),
            ))

    return WingAudit(
        profile=name,
        scoped=True,
        total_wings=len(wing_set),
        own_wings=own_wings,
        issues=issues,
    )


def classify_wing(
    wing: str,
    active_profile: Optional[str] = None,
    known_profiles: Optional[Iterable[str]] = None,
) -> Optional[WingIssue]:
    """Classify a *single* wing written to the active profile's palace.

    Companion to :func:`audit_palace_wings` for the write path (issue #286):
    a write-side guard can ask "is this one wing in-namespace for the profile
    I'm writing under?" without loading the whole store.

    Returns a :class:`WingIssue` (``cross-profile`` or ``out-of-namespace``)
    when the wing is foreign to a *scoped* profile, and ``None`` when the write
    is expected (the wing is in-namespace, or the profile is the shared
    ``default`` where any wing is legitimate).  ``None`` therefore means "no
    boundary concern" — not "verified clean".
    """
    wing = str(wing or "").strip()
    if not wing:
        return None
    audit = audit_palace_wings(
        [wing],
        active_profile=active_profile,
        known_profiles=known_profiles,
    )
    if not audit.scoped or not audit.issues:
        return None
    return audit.issues[0]
