"""GH-284 Issue B, Scope 3 — version / schema gate (re-export shim).

This module used to *be* the gate.  It is now a thin compatibility shim that
re-exports the canonical implementation from the SDK:

    from memchorus.version_gate import ...

Why a shim and not a delete
---------------------------
Three reasons, all load-bearing:

1. **Import safety (B2).**  ``integrations.provider`` must still import — and
   still be able to report a precise ``unavailable_reason()`` — even when the
   ``memchorus`` SDK is absent or too old to carry ``memchorus.version_gate``.
   The ``except`` branch below provides import-safe stand-ins with the same
   public API, so the provider never fails to load for want of the gate.

2. **Consumer stability.**  The provider ``__init__`` and the scope-3 tests
   import the gate symbols from this path.  Re-exporting (rather than
   changing the import site) keeps every consumer — including
   ``mock.patch("integrations.provider._version_gate_status")`` — working
   unchanged.

3. **Identity.**  The scope-3 contract test asserts
   ``EXPECTED_SDK_VERSION is <the gate's EXPECTED_SDK_VERSION>``.  Because both
   the provider and the test resolve the name to *the same object* in
   ``memchorus.version_gate``, that identity holds; and
   ``EXPECTED_SDK_VERSION == memchorus.__version__`` holds because the SDK
   derives the expected floor from ``memchorus.__version__`` —
   equal-by-construction, no second literal to drift.

The sanctioned dependency direction is provider → SDK (``integrations/`` may
import ``memchorus``, never the reverse), so importing ``memchorus.version_gate``
here is the correct direction for the shipped wheel as well.
"""

from __future__ import annotations

__all__ = [
    "EXPECTED_SDK_VERSION",
    "parse_version",
    "compare_versions",
    "VersionGateStatus",
    "evaluate",
    "palace_schema_version",
    "status_for_current_environment",
]

try:  # Happy path: the canonical SDK gate is present.
    from memchorus.version_gate import (
        EXPECTED_SDK_VERSION,
        parse_version,
        compare_versions,
        VersionGateStatus,
        evaluate,
        palace_schema_version,
        status_for_current_environment,
    )
except Exception:  # noqa: BLE001 - B2: SDK absent / too old to carry the gate.
    # Self-contained import-safe stand-ins that preserve the public API and
    # the provider's fallback constructor contract (see
    # ``MemChorusMemoryProvider._version_gate``), so the provider still loads
    # and can report a precise unavailable_reason() when the SDK is missing.
    import re as _re
    from dataclasses import dataclass as _dataclass
    from pathlib import Path as _Path
    from typing import Optional as _Optional, Tuple as _Tuple

    EXPECTED_SDK_VERSION = "2.0.80"

    _VERSION_RE = _re.compile(
        r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]([0-9A-Za-z]+))?\s*$"
    )
    _PRE_RELEASE_RANK = {
        "alpha": -3,
        "beta": -2,
        "rc": -1,
        "pre": -1,
        "dev": -2,
    }

    @_dataclass(frozen=True)
    class _ParsedVersion:
        major: int = 0
        minor: int = 0
        patch: int = 0
        pre: _Optional[str] = None

        def _sort_key(self) -> _Tuple[int, int, int, int, int]:
            tag = (self.pre or "").lower()
            m = _re.match(r"([a-z]+)(\d*)", tag)
            prefix = m.group(1) if m else ""
            rank = _PRE_RELEASE_RANK.get(prefix, 0)
            num = int(m.group(2)) if m and m.group(2) else 0
            return (self.major, self.minor, self.patch, rank, num)

    def parse_version(value):
        try:
            s = str(value).strip()
            m = _VERSION_RE.match(s)
            if not m:
                return None
            return _ParsedVersion(
                major=int(m.group(1)),
                minor=int(m.group(2) or 0),
                patch=int(m.group(3) or 0),
                pre=m.group(4),
            )
        except Exception:  # noqa: BLE001
            return None

    def compare_versions(a, b) -> int:
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

    @_dataclass(frozen=True)
    class VersionGateStatus:
        installed: _Optional[str] = None
        expected: str = EXPECTED_SDK_VERSION
        safe_for_reads: bool = True
        safe_for_writes: bool = False
        reason: str = ""
        schema_version: _Optional[str] = None
        blocked_by: str = "no-sdk"

        @property
        def blocked(self) -> bool:
            return not self.safe_for_writes

    def palace_schema_version(palace_dir) -> _Optional[str]:
        try:
            p = _Path(palace_dir) if palace_dir else None
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
        except Exception:  # noqa: BLE001
            pass
        return None

    def evaluate(installed_version, expected_version: str = EXPECTED_SDK_VERSION, palace_dir=None) -> VersionGateStatus:
        if installed_version is None or str(installed_version).strip() == "":
            return VersionGateStatus(
                installed=None,
                expected=expected_version,
                safe_for_reads=True,
                safe_for_writes=False,
                reason="SDK not importable — no memchorus package in the active interpreter.",
                blocked_by="no-sdk",
            )
        return VersionGateStatus(
            installed=str(installed_version),
            expected=expected_version,
            safe_for_reads=True,
            safe_for_writes=False,
            reason="Gate shim active: the canonical SDK gate is unavailable — writes refused by default.",
            blocked_by="no-sdk",
        )

    def status_for_current_environment() -> VersionGateStatus:
        try:
            import memchorus  # noqa: F401
            installed = getattr(memchorus, "__version__", None)
        except Exception:  # noqa: BLE001
            installed = None
        return evaluate(installed_version=installed)
