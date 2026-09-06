"""Project record schema — the two recall channels: ``location`` and ``standard``.

Issue #163 (IMPL #163.1, the schema card).

This module is *structure-only*: it defines the record key namespace, the shape
and validation contract of the two channels, the default/fallback pointers,
and the save-time attachment step that keeps the channels out of the
free-text ranking path.  It does **not** wire ``PROJECT_START``,
``resolve_project_record()`` fallback semantics, session-start rendering, CLI,
or doctor surfacing — those live on the follow-on cards (#163.2–#163.4).

Design spec (authoritative):
``<workspace>/Bubo_Wisdom/Projects/MemChorus/
MemChorus-Recall-LocationStandard-Design-Spec.md``
— §2 (public schema), §2.3 (validation), §2.4 (defaults + ``verified_at``
semantics), §3.1 (stable key form), §3.2 (placement / isolation).

Independence invariant — held throughout.
``location`` and ``standard`` are **two independent optional** structured
fields on a single ``project:<name>`` record.  **Neither implies the other.**
A record may carry one, both, or neither.  Each is validated and defaulted by
its **own** code path; changing one never changes the other.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# §3.1 — Stable key namespace
# ---------------------------------------------------------------------------

#: Namespace prefix for keyed project records.
PROJECT_KEY_PREFIX = "project:"

#: Allowed record-key segment: 1-64 chars of a relaxed ASCII-slug alphabet.
# Non-ASCII / punctuation / whitespace collapse to '-' so that ``MemChorus``
# -> ``memchorus``, ``Mem Pal`` -> ``mem-pal`` (a case-only miss becomes a
# byte-identical key instead of a silent lookup miss). Slug built with
# ``re.sub(r"[^a-z0-9]+", "-")`` in :func:`normalize_project_key`.

# ---------------------------------------------------------------------------
# §2.1 — location field constraints
# ---------------------------------------------------------------------------

#: ``location.source`` must be ``ssot:<doc>#<anchor>`` or ``derived:<rule>``.
LOCATION_SOURCE_RE = re.compile(r"^(ssot|derived):.+$")

# ISO-8601 UTC instant.  Accepts a trailing ``Z`` (the spec's canonical form)
# or an explicit offset; bare naive instants are rejected (ambiguous time-zone).
_ISO8601_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)

# ---------------------------------------------------------------------------
# §2.2 — standard field constraints
# ---------------------------------------------------------------------------

#: ``standard.skill`` — non-empty slug (lowercase, letters/digits/-/_, max 64).
STANDARD_SKILL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,63})$")

#: Absolute-path shape that must not appear in ``standard.doc_path``.
#: Matches a leading ``/`` or ``~`` (POSIX home) or a Windows drive absolute.
_DOC_PATH_ABS_RE = re.compile(r"^(/|~|[A-Za-z]:[/\\])")

#: Per-field caps (mirrors ``locator.py`` GEST/TOPICS caps).
GIST_MAX_CHARS = 120
TOPICS_MAX = 6
TOPIC_MAX_CHARS = 24

# Explicit field sets — unknown keys in either object are rejected (§2.3).
_LOCATION_FIELDS = ("canonical_root", "source", "verified_at")
_STANDARD_FIELDS = ("skill", "doc_path", "gist", "topics")


class ProjectRecordError(ValueError):
    """A ``project:<name>`` record failed the §2.3 schema contract.

    Raised on *save* (fail-loud) so an invalid structured channel never
    silently pollutes the record and later masquerades as a canonical root.
    Message is actionable: it names the field, the offending value, and the
    rule that was violated.
    """


# ---------------------------------------------------------------------------
# §3.1 — key normalization (store AND retrieve)
# ---------------------------------------------------------------------------

def normalize_project_key(name: Optional[str] = None, key: Optional[str] = None) -> str:
    """Return the stable ``project:<name>`` key for a project name or key.

    **Normalization (§3.1):** the segment after the colon is canonicalized to
    lowercase ASCII so that ``project:memchorus``, ``project:MemChorus`` and
    ``project:MEMCHORUS`` all resolve to the **same** record.  Non-alphanumeric
    characters are collapsed to ``-``.  This must be applied on *both* store and
    retrieve; a case-mismatch without it is a silent lookup miss.

    Args:
        name: project name (``"MemChorus"``).
        key:  an already-namespaced key (``"project:MemChorus"``) to normalize.

    Returns:
        str: the stable ``project:<slug>`` key.

    Raises:
        ProjectRecordError: if neither *name* nor a usable *key* segment is
            given (empty input).
    """
    if name is None and key is None:
        raise ProjectRecordError("normalize_project_key: provide name or key")
    if name is not None:
        raw = str(name)
    else:
        # Accept an existing namespaced key.
        k = str(key)
        if k.startswith(PROJECT_KEY_PREFIX):
            raw = k[len(PROJECT_KEY_PREFIX):]
        else:
            raw = k

    lowered = raw.strip().lower()
    # Keep ASCII alphanumerics; collapse everything else (spaces, dots,
    # underscores, accents) to a single '-' and trim the ends.
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    # Collapse runs of '-' and trim ends.
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        raise ProjectRecordError(
            "project key segment normalizes to empty (name=%r)" % (name,)
        )
    return PROJECT_KEY_PREFIX + slug


def is_project_key(key: Optional[str]) -> bool:
    """True if *key* is (or namespacing-normalizes to) a ``project:<name>`` key."""
    if not isinstance(key, str):
        return False
    k = key.strip()
    if k.startswith(PROJECT_KEY_PREFIX):
        return True
    return False


# ---------------------------------------------------------------------------
# §2.3 — validators
# ---------------------------------------------------------------------------

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ProjectRecordError(msg)


def _validate_str(v: Any, field: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ProjectRecordError(
            "%s: expected non-empty string, got %r" % (field, v)
        )
    return v


def _validate_verified_at(v: Any) -> None:
    """§2.3 / §2.4 — ISO-8601 UTC timestamp **or ``None`` (null)**, nothing else.

    ``None`` is the *derived-by-rule, unverified* sentinel (spec §2.4).  A
    non-null value is parsed and rejected if it is not a valid instant.
    """
    if v is None:
        return  # null semantics — allowed
    if not isinstance(v, str):
        raise ProjectRecordError(
            "location.verified_at: expected ISO-8601 UTC string or null, got %r" % (v,)
        )
    if not _ISO8601_UTC_RE.match(v):
        raise ProjectRecordError(
            "location.verified_at: not ISO-8601 UTC instant %r" % (v,)
        )
    try:
        # Defensive: prove the instant parses to a real date/time, not just
        # that it matches the regex shape.
        if v.endswith("Z"):
            datetime.fromisoformat(v[:-1] + "+00:00")
        else:
            datetime.fromisoformat(v)
    except ValueError as exc:  # pragma: no cover - regex already gates
        raise ProjectRecordError(
            "location.verified_at: unparseable instant %r" % (v,)
        ) from exc


def _validate_location(location: Any) -> None:
    """Validate a ``location`` object against §2.1 / §2.3 (fail-loud)."""
    if not isinstance(location, dict):
        raise ProjectRecordError(
            "location: expected an object/dict, got %s" % type(location).__name__
        )
    unknown = set(location.keys()) - set(_LOCATION_FIELDS)
    if unknown:
        raise ProjectRecordError(
            "location: unknown key(s) %s — allowed: %s"
            % (sorted(unknown), list(_LOCATION_FIELDS))
        )

    root = _validate_str(location.get("canonical_root"), "location.canonical_root")
    # Spec §2.1/§2.4: canonical_root is a non-empty root path string.  It may be
    # absolute, ``~``-expanded, or the workspace-relative default form
    # (``<workspace>/Code/<project>/``), so NO absolute/trailing-slash constraint
    # is imposed on *location* (the non-abs constraint applies only to
    # standard.doc_path).  Rejecting the spec's own default here would be a
    # contradiction with §2.4.

    src = _validate_str(location.get("source"), "location.source")
    if not LOCATION_SOURCE_RE.match(src):
        raise ProjectRecordError(
            "location.source: must match ^s(ot|derived):.+$ — got %r" % src
        )

    if "verified_at" in location:
        _validate_verified_at(location["verified_at"])


def _validate_standard(standard: Any) -> None:
    """Validate a ``standard`` object against §2.2 / §2.3 (fail-loud)."""
    if not isinstance(standard, dict):
        raise ProjectRecordError(
            "standard: expected an object/dict, got %s" % type(standard).__name__
        )
    unknown = set(standard.keys()) - set(_STANDARD_FIELDS)
    if unknown:
        raise ProjectRecordError(
            "standard: unknown key(s) %s — allowed: %s"
            % (sorted(unknown), list(_STANDARD_FIELDS))
        )

    skill = _validate_str(standard.get("skill"), "standard.skill")
    if not STANDARD_SKILL_RE.match(skill):
        raise ProjectRecordError(
            "standard.skill: must be a non-empty slug (lowercase -/_/digits) — got %r" % skill
        )

    doc_path = _validate_str(standard.get("doc_path"), "standard.doc_path")
    if _DOC_PATH_ABS_RE.match(doc_path):
        raise ProjectRecordError(
            "standard.doc_path: must be a relative path (no leading /, ~, or drive) — got %r" % doc_path
        )

    gist = standard.get("gist")
    if gist is not None:
        if not isinstance(gist, str):
            raise ProjectRecordError("standard.gist: expected string — got %r" % (gist,))
        if len(gist) > GIST_MAX_CHARS:
            raise ProjectRecordError(
                "standard.gist: exceeds %d chars (%d)" % (GIST_MAX_CHARS, len(gist))
            )

    topics = standard.get("topics")
    if topics is not None:
        if not isinstance(topics, (list, tuple)):
            raise ProjectRecordError(
                "standard.topics: expected a list — got %r" % (topics,)
            )
        if len(topics) > TOPICS_MAX:
            raise ProjectRecordError(
                "standard.topics: exceeds %d items (%d)" % (TOPICS_MAX, len(topics))
            )
        for t in topics:
            if not isinstance(t, str) or len(t) > TOPIC_MAX_CHARS:
                raise ProjectRecordError(
                    "standard.topics: each item must be a string <= %d chars — got %r"
                    % (TOPIC_MAX_CHARS, t)
                )


def validate_project_record(value: Any) -> None:
    """Validate a ``project:<name>`` record's structured channels (§2.3).

    Only the ``location`` / ``standard`` objects are checked.  Each is
    validated **independently** — the presence/absence of one does not gate
    the other (the independence invariant).  A record that carries neither
    key is valid (there is nothing to violate).

    Raises:
        ProjectRecordError: on any §2.3 contract violation (fail-loud on save).
    """
    if not isinstance(value, dict):
        return  # non-dict payload — nothing structured to validate here
    if "location" in value:
        _validate_location(value["location"])
    if "standard" in value:
        _validate_standard(value["standard"])


# ---------------------------------------------------------------------------
# §2.4 — defaults / fallback pointers (versioned, from SSoT — never free text)
# ---------------------------------------------------------------------------

def default_location(project_name: str) -> Dict[str, Any]:
    """§2.4 — ``location`` absent → derive by the SSoT rule, NOT a free note.

    ``canonical_root = <workspace>/Code/<project-name>/`` (the on-disk name of
    the last path segment **retains its exact case**), ``source =
    derived:ORGANIZATION.md#<project>``, ``verified_at = null`` (null ⇒
    *unverified, derived-by-rule* — the agent must confirm before trusting).

    Args:
        project_name: the project name **as on disk** (case preserved).

    Returns:
        dict: the derived ``location`` object (a *candidate* root, §2.4).
    """
    name = str(project_name).strip().rstrip("/")
    # The derived rule assumes code lives under ``<workspace>/Code/<name>/``.
    # We keep the exact on-disk case of the segment.
    return {
        "canonical_root": "<workspace>/Code/%s/" % (name or "project"),
        "source": "derived:ORGANIZATION.md#%s" % (name or "project"),
        "verified_at": None,
    }


def default_standard(structure_only: bool = False) -> Dict[str, Any]:
    """§2.4 — ``standard`` absent → versioned default pointer (never ad-hoc).

    Args:
        structure_only: if True, point at the structure/organization doc
            (``project-organization``) rather than the development-process
            skill.

    Returns:
        dict: the default ``standard`` object (``skill`` + ``doc_path`` present;
        ``gist``/``topics`` optional/absent).
    """
    if structure_only:
        return {
            "skill": "project-organization",
            "doc_path": "Projects/ORGANIZATION.md",
        }
    return {
        "skill": "development-process",
        "doc_path": "stable/development-process/SKILL.md",
    }


# ---------------------------------------------------------------------------
# §3.2 — extract + attach the structured channels at save time
# ---------------------------------------------------------------------------

def extract_project_channels(value: Any) -> Optional[Dict[str, Any]]:
    """Pluck the ``location``/``standard`` channel(s) out of a record payload.

    Returns a shallow-copied dict containing **only** the keys present among
    ``location``/``standard`` (or ``None`` when the payload is not a dict or
    carries neither) so the caller can persist them on a *separate* top-level
    channel — never re-serialized into the prose body.
    """
    if not isinstance(value, dict):
        return None
    out: Dict[str, Any] = {}
    for field in ("location", "standard"):
        if field in value:
            out[field] = value[field]
    return out or None


def attach_project_channels(value: Any, key: str) -> Any:
    """Ensure a ``project:<name>`` record's structured channels survive the write.

    For ``project:``-namespaced keys this is a *no-op pass-through*: the
    ``location``/``standard`` objects already ride on ``value`` at the top
    level and are kept **out of** the free-text/body path — exactly the §3.2
    placement (same way the Issue-#140 locator attaches, but as a separate
    top-level channel, never serialized into the body).

    Returns *value* unchanged so ordinary (non-``project:``) saves and plain
    string saves are byte-identical to before — mirroring the locator attach
    which must never fail a save and never bloat the body.

    Contract: this function **never raises**.  Validation is enforced
    separately (fail-loud) by :func:`validate_project_record` at the
    orchestrator save boundary, so a malformed channel cannot be persisted
    silently.
    """
    if not isinstance(key, str) or not key.startswith(PROJECT_KEY_PREFIX):
        return value
    return value


def strip_project_channels(value: Any) -> Any:
    """Return *value* with the ``location``/``standard`` top-level keys removed.

    Utility for the "kept out of the free-text body" path: when a payload must
    be rendered/prosed without the structured channels (e.g. a body-only
    injection), pass through this to drop them while the channels remain
    retrievable via the keyed record.  Non-dict input is returned unchanged.
    """
    if not isinstance(value, dict):
        return value
    out = {k: v for k, v in value.items() if k not in ("location", "standard")}
    return out


# ---------------------------------------------------------------------------
# Convenience — the full record shape
# ---------------------------------------------------------------------------

def build_record(
    location: Optional[Dict[str, Any]] = None,
    standard: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compose a ``project:<name>`` record payload from the two channels.

    Both arguments are optional and independent (the invariant).  When both
    are ``None`` the record is an empty payload.  The returned dict is the
    *value* to pass to ``orchestrator.save(key="project:<name>", value=…)``.
    """
    rec: Dict[str, Any] = {}
    if location is not None:
        rec["location"] = location
    if standard is not None:
        rec["standard"] = standard
    return rec


__all__ = [
    "PROJECT_KEY_PREFIX",
    "GIST_MAX_CHARS",
    "TOPICS_MAX",
    "TOPIC_MAX_CHARS",
    "LOCATION_SOURCE_RE",
    "STANDARD_SKILL_RE",
    "ProjectRecordError",
    "normalize_project_key",
    "is_project_key",
    "validate_project_record",
    "default_location",
    "default_standard",
    "extract_project_channels",
    "attach_project_channels",
    "strip_project_channels",
    "build_record",
]
