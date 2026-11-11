"""GH-284 Issue B, Scope 3 — version / schema gate (canonical SDK copy).

This module is the single source of truth for the "refuse unsafe writes"
gate.  It moved here (from ``integrations/provider/_version_gate.py``)
deliberately: only the ``memchorus`` package is shipped in the wheel
(``integrations/`` is repo-local, not in ``package-dir``), so on a clean
install the doctor — which is itself part of the shipped SDK — cannot import
``integrations.provider``.  The original module's docstring promised that
*the provider and the doctor both call ``evaluate``*; that promise is only
true when the gate lives inside the SDK.  ``integrations/provider/
_version_gate.py`` is now a thin re-export shim so every existing consumer
(the provider ``__init__`` and the scope-3 tests) keeps working unchanged.

What the gate does
------------------
When the installed SDK is older than the version the store was written by, or
when the on-disk MemPalace ``schema_version`` marker is newer than the
installed SDK, the gate refuses *writes* while keeping *reads* available.
Reads degrade gracefully (the provider simply returns no hits), so an
operator is never fully stranded; writes are the dangerous direction
(clobbering a newer schema), so they are the ones held back.

The gate is import-safe: it never raises, and on any internal error it
degrades to *block writes* (the conservative default) — an unsafe write is
never allowed through a gate that cannot be sure it is safe.

``EXPECTED_SDK_VERSION`` is now derived from ``memchorus.__version__`` —
equal-by-construction, so the last literal "2.0.80" drift surface is gone.
Cross-version protection (a store written by a newer SDK than the one running)
is carried by the ``schema_version`` probe, not by the expected floor.

Symbols
-------
- :data:`EXPECTED_SDK_VERSION` — the declared minimum (= current SDK version).
- :func:`parse_version` — tolerant version string → numeric tuple.
- :func:`compare_versions` — total-order comparison (pre-release < release).
- :class:`VersionGateStatus` — the result of a gate evaluation.
- :func:`evaluate` — pure: evaluate against a given installed version.
- :func:`palace_schema_version` — read the on-disk schema marker.
- :func:`status_for_current_environment` — evaluate against the live env.

The provider and the doctor both call :func:`evaluate` /
:func:`status_for_current_environment`; neither needs to know about the
schema probe or the version comparison directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

# Resolve the canonical runtime version at import time.  This makes the
# expected floor *equal-by-construction* to the SDK actually in use, so the
# two can never drift.  The fallback (an empty/low floor) is reached only in a
# broken interpreter where ``memchorus`` itself cannot be imported — a case
# that is independently a hard "no-sdk" block in :func:`evaluate`, so the
# exact floor value there is immaterial.
try:  # pragma: no cover - import guard, exercised only in a broken env
    from memchorus import __version__ as _SDK_VERSION
except Exception:  # noqa: BLE001
    _SDK_VERSION = None

__all__ = [
    "EXPECTED_SDK_VERSION",
    "parse_version",
    "compare_versions",
    "VersionGateStatus",
    "evaluate",
    "palace_schema_version",
    "status_for_current_environment",
]

#: Declared minimum SDK version.  Derived from ``memchorus.__version__``
#: (2.0.80 at the time of issue #284) so it is equal-by-construction to the
#: running SDK — no separate literal to keep in sync.  The gate treats *this
#: value or newer* as safe for writes and *older* as unsafe.  Reads remain
#: safe in both cases — the provider already degrades gracefully when no hits
#: come back.  Falls back to ``"0"`` (which blocks all writes) if the version
#: cannot be read, which is the conservative default.
EXPECTED_SDK_VERSION = _SDK_VERSION if _SDK_VERSION else "0"

_VERSION_RE = re.compile(
    r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]([0-9A-Za-z]+))?\s*$"
)
_PRE_RELEASE_RANK = {"alpha": -3, "beta": -2, "rc": -1, "pre": -1, "dev": -2}


@dataclass(frozen=True)
class _ParsedVersion:
    """Internal parsed form: numeric tuple + optional pre-release tag."""

    major: int = 0
    minor: int = 0
    patch: int = 0
    pre: Optional[str] = None

    def _sort_key(self) -> Tuple[int, int, int, int, int]:
        """Total-order key: pre-release sorts *before* the release.

        The pre-release rank is keyed off the leading alphabetic prefix of
        the tag (``rc1`` → ``rc``, ``beta2`` → ``beta``); the trailing
        numeric suffix is compared as an integer so ``beta10`` sorts after
        ``beta2``.  A purely numeric or unknown tag ranks as a
        release-equivalent (0) so it still sorts deterministically.
        """
        tag = (self.pre or "").lower()
        m = re.match(r"([a-z]+)(\d*)", tag)
        prefix = m.group(1) if m else ""
        rank = _PRE_RELEASE_RANK.get(prefix, 0)
        num = int(m.group(2)) if m and m.group(2) else 0
        return (self.major, self.minor, self.patch, rank, num)


@dataclass(frozen=True)
class VersionGateStatus:
    """Result of a single :func:`evaluate` call."""

    installed: Optional[str] = None
    expected: str = EXPECTED_SDK_VERSION
    safe_for_reads: bool = True
    safe_for_writes: bool = False
    reason: str = ""
    schema_version: Optional[str] = None
    blocked_by: str = "no-sdk"  # "no-sdk" | "version" | "schema" | "ok"

    @property
    def blocked(self) -> bool:
        return not self.safe_for_writes


def parse_version(value) -> Optional[_ParsedVersion]:
    """Parse a version string into a :class:`_ParsedVersion`.

    Tolerant of ``v`` prefix, missing minor/patch (``"2.0"`` →
    ``2.0.0``), and trailing pre-release tags (``"2.0.80-rc1"``).
    Returns ``None`` for anything that does not start with a digit.
    Never raises.
    """
    try:
        s = str(value).strip()
        m = _VERSION_RE.match(s)
        if not m:
            return None
        major = int(m.group(1))
        minor = int(m.group(2) or 0)
        patch = int(m.group(3) or 0)
        pre = m.group(4)
        return _ParsedVersion(major=major, minor=minor, patch=patch, pre=pre)
    except Exception:
        return None


def compare_versions(a, b) -> int:
    """Sign of ``a - b``: ``-1`` if a < b, ``0`` if equal, ``+1`` if a > b.

    Both arguments may be strings or pre-parsed :class:`_ParsedVersion`.
    If either cannot be parsed, the comparison is ``0`` (indeterminate) —
    callers should treat that as "no signal" rather than "a < b".
    """
    pa = a if isinstance(a, _ParsedVersion) else parse_version(a)
    pb = b if isinstance(b, _ParsedVersion) else parse_version(b)
    if pa is None or pb is None:
        return 0
    ka, kb = pa._sort_key(), pb._sort_key()
    if ka < kb:
        return -1
    if ka > kb:
        return 1
    return 0


def palace_schema_version(palace_dir) -> Optional[str]:
    """Read the ``schema_version`` marker from a MemPalace data directory.

    Returns the marker's string value, or ``None`` if the directory does
    not exist, is not readable, or carries no marker.  Never raises.
    """
    try:
        p = Path(palace_dir) if palace_dir else None
        if p is None:
            return None
        for candidate in (
            p / "schema_version",
            p / "VERSION",
            p / "manifest" / "schema_version",
        ):
            if candidate.is_file():
                text = candidate.read_text().strip()
                if text:
                    return text.splitlines()[0].strip()
    except Exception:
        pass
    return None


def evaluate(
    installed_version,
    expected_version: str = EXPECTED_SDK_VERSION,
    palace_dir=None,
) -> VersionGateStatus:
    """Evaluate the version / schema gate for a given installed SDK.

    Parameters
    ----------
    installed_version
        The version string from the installed ``memchorus.__version__``.
        Pass ``None`` (or the empty string) when the SDK is not importable
        at all — that is the *strongest* block (``no-sdk``).
    expected_version
        The declared minimum.  Defaults to :data:`EXPECTED_SDK_VERSION`.
    palace_dir
        Optional MemPalace data directory.  When provided, the
        ``schema_version`` marker is consulted; if it is newer than the
        installed SDK's major.minor, the gate also refuses writes (catches
        the case where the installed ``__version__`` is stale but the store
        was migrated by a newer SDK).

    Returns
    -------
    VersionGateStatus
        ``safe_for_writes`` is ``True`` only when the installed version is
        *at or above* the expected minimum AND (when a palace dir was given)
        the schema marker is not newer than the installed major.minor.
    """
    # Case 1: no SDK at all.  Reads are "safe" in the sense that the provider
    # will just have no hits (orientation degrades gracefully); writes are
    # hard-blocked — there is nothing to write to.
    if installed_version is None or str(installed_version).strip() == "":
        return VersionGateStatus(
            installed=None,
            expected=expected_version,
            safe_for_reads=True,
            safe_for_writes=False,
            reason="SDK not importable — no memchorus package in the active interpreter.",
            blocked_by="no-sdk",
        )

    # Case 2: version comparison.
    installed_str = str(installed_version).strip()
    if parse_version(installed_str) is None:
        # The SDK imported and reported a version string, but it could not be
        # parsed (stale / hand-edited / truncated).  Per B6 the mismatch must
        # be *detected and reported*, so we block writes rather than silently
        # allowing them.  Reads stay available (the provider degrades
        # gracefully when no hits come back).
        return VersionGateStatus(
            installed=installed_str,
            expected=expected_version,
            safe_for_reads=True,
            safe_for_writes=False,
            reason=(
                f"Installed SDK version {installed_str!r} could not be "
                f"parsed against the declared minimum {expected_version}.  "
                f"Writes are refused until the version can be compared; "
                f"reads remain available."
            ),
            blocked_by="version",
        )

    cmp = compare_versions(installed_version, expected_version)
    if cmp < 0:
        return VersionGateStatus(
            installed=str(installed_version),
            expected=expected_version,
            safe_for_reads=True,
            safe_for_writes=False,
            reason=(
                f"Installed SDK {installed_version} is older than the "
                f"declared minimum {expected_version}.  Writes are refused to "
                f"avoid clobbering a newer schema; reads remain available."
            ),
            blocked_by="version",
        )

    # Case 3: schema probe (only when a palace dir is supplied).
    if palace_dir is not None:
        schema = palace_schema_version(palace_dir)
        if schema:
            if compare_versions(schema, installed_version) > 0:
                return VersionGateStatus(
                    installed=str(installed_version),
                    expected=expected_version,
                    safe_for_reads=True,
                    safe_for_writes=False,
                    schema_version=schema,
                    reason=(
                        f"Palace schema_version {schema} is newer than the "
                        f"installed SDK {installed_version}.  Writes are "
                        f"refused to avoid downgrading the store; reads "
                        f"remain available."
                    ),
                    blocked_by="schema",
                )

    # All clear.
    return VersionGateStatus(
        installed=str(installed_version),
        expected=expected_version,
        safe_for_reads=True,
        safe_for_writes=True,
        reason="",
        blocked_by="ok",
    )


def status_for_current_environment() -> VersionGateStatus:
    """Convenience: evaluate the gate against the *live* environment.

    Reads ``memchorus.__version__`` if importable, and consults the
    active profile's MemPalace data directory for a schema marker.
    Never raises — on any internal error, returns the ``no-sdk`` block.
    """
    try:
        installed = None
        try:
            import memchorus  # noqa: F401
            installed = getattr(memchorus, "__version__", None)
        except Exception:
            installed = None

        palace_dir = None
        try:
            from memchorus.state_inventory import (
                inventory_for_profile,
                StateCategory,
            )
            for entry in inventory_for_profile():
                if entry.category == StateCategory.PALACE:
                    palace_dir = entry.path
                    break
        except Exception:
            palace_dir = None

        return evaluate(installed_version=installed, palace_dir=palace_dir)
    except Exception:
        return VersionGateStatus(
            installed=None,
            expected=EXPECTED_SDK_VERSION,
            safe_for_reads=True,
            safe_for_writes=False,
            reason="Version-gate evaluation failed (internal error) — writes refused by default.",
            blocked_by="no-sdk",
        )
