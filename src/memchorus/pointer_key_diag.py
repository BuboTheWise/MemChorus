"""Pointer-key vs drawer_id mismatch diagnostic (issue #331).

The recall pipeline uses two structurally disjoint ID spaces (see issue #329):

* **pointer keys** — ``<tag>-<sha16>`` / ``<tag>_<sha16>`` (e.g.
  ``decision-88e86e5790c4c3ec`` / ``DECISION_88e86e5790c4c3ec``), a
  MemChorus flat-file ID space; and
* **drawer ids** — MemPalace embedding ids (``drawer_<wing>_<room>_<sha256>``).

A pointer key is *resolved* when a retrieve for it yields a non-null body from
a local store — either the profile-scoped / base ``mempalace_cache``
(under either the flat or the drawer cross-form) or the profile ``memories``
flat store.  A pointer key that references a body that no local store yields is
a **mismatch** (a dangling pointer / 404): the injected directive points at a
``learning-*`` / ``decision-*`` key whose body is missing or null-payload.

GH-330 made the retrieve path probe both cross-forms across every candidate
profile leaf, so a well-formed key stored anywhere now resolves.  This
diagnostic validates *that* invariant: it scans the key store (and any
operator-supplied keys from a recall block), resolves each pointer key through
the same flat/drawer cross-form logic, and reports the ones that still do not
resolve — count plus a few examples.  It is read-only (non-destructive) and
fast (local file lookups only, no MCP / no live refetch).

The flat/drawer cross-form and pointer-shape helpers are imported from
:mod:`memchorus.mempalace_memory_source` so the diagnostic exercises the exact
invariant the GH-330 fix enforces, rather than re-deriving it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# Single source of truth for pointer-key shape + the flat<->drawer cross-form
# resolution the GH-330 fix relies on.  Reuse, don't re-derive.
from memchorus.mempalace_memory_source import (
    _POINTER_KEY_RE,
    _cache_key_variants,
)


def _is_pointer_key(name: str) -> bool:
    """True if ``name`` is a pointer key (``<tag>[-_]<8+hex>``)."""
    return bool(_POINTER_KEY_RE.match(name))


def _canonical_flat(key: str) -> str:
    """Canonical flat form of a pointer key (``decision-<hex>``).

    No-op for non-pointer keys.  Used to de-duplicate a key that appears under
    both its flat (``decision-<hex>``) and drawer (``DECISION_<hex>``) forms.
    """
    m = _POINTER_KEY_RE.match(key)
    if not m:
        return key
    return f"{m.group('cat').lower()}-{m.group('hex')}"


def _has_payload(path: Path) -> bool:
    """True if the cache file holds a non-null top-level body.

    Mirrors the null-check in ``MemPalaceMemorySource.retrieve`` — a file whose
    top-level is ``null`` (a failed capture) or that fails to parse counts as
    *unresolved*, not as a hit.  Best-effort: any read/parse error is a miss.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unreadable/corrupt → treat as a miss
        return False
    if data is None:
        return False
    if isinstance(data, dict):
        return any(v is not None for v in data.values())
    return True


@dataclass
class PointerKeyReport:
    """Result of a pointer-key resolution census."""

    total_keys: int = 0
    resolved: int = 0
    unresolved: int = 0
    # Cross-form hits: the backing file's form differs from the canonical flat
    # key (e.g. key ``decision-<hex>`` stored as ``DECISION_<hex>.json``).
    # These are exactly the pointer-key / drawer_id form divergences the GH-330
    # fix absorbs; reported so the operator can see how much divergence exists.
    cross_form_hits: int = 0
    unresolved_keys: List[str] = field(default_factory=list)
    scanned_roots: int = 0
    no_data: bool = True  # True when no pointer keys were found at all

    @property
    def ok(self) -> bool:
        """Clean when nothing is unresolved (including the no-data case)."""
        return self.unresolved == 0

    def to_dict(self) -> Dict:
        return {
            "total_keys": self.total_keys,
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "cross_form_hits": self.cross_form_hits,
            "unresolved_keys": list(self.unresolved_keys),
            "scanned_roots": self.scanned_roots,
            "no_data": self.no_data,
            "ok": self.ok,
        }

    def summary(self) -> str:
        if self.no_data:
            return "no pointer keys found in the key store"
        if self.ok:
            return (
                f"{self.total_keys} pointer keys, all resolve locally "
                f"({self.cross_form_hits} resolved across flat/drawer forms)"
            )
        return (
            f"{self.unresolved} of {self.total_keys} pointer keys do NOT resolve "
            f"to a local body (dangling pointers / 404)"
        )


def discover_pointer_keys(roots: Sequence[Path]) -> Dict[str, List[Path]]:
    """Map canonical flat pointer key -> list of backing file paths.

    Scans each ``root`` (a ``mempalace_cache`` or ``memories`` directory) for
    ``*.json`` files whose stem is a pointer key, de-duplicating by canonical
    flat key.  Non-directories are skipped silently.
    """
    found: Dict[str, List[Path]] = {}
    for root in roots:
        try:
            if not root.is_dir():
                continue
            entries = sorted(root.glob("*.json"))
        except OSError:  # noqa: BLE001 — unreadable dir → skip
            continue
        for f in entries:
            if not f.is_file():
                continue
            if _is_pointer_key(f.stem):
                key = _canonical_flat(f.stem)
                found.setdefault(key, []).append(f)
    return found


def resolve_pointer_key(
    key: str,
    cache_dirs: Sequence[Path],
    memory_dirs: Sequence[Path],
) -> Optional[Path]:
    """Resolve ``key`` through the fixed local path (GH-330).

    For each store directory, probe every cross-form variant of ``key``
    (``_cache_key_variants``) and return the first file holding a non-null
    body.  Returns ``None`` when no store yields a body — the 404 / mismatch.
    Pure file lookups: no MCP, no live refetch, non-destructive.
    """
    variants = _cache_key_variants(key)
    for store in list(cache_dirs) + list(memory_dirs):
        try:
            if not store.is_dir():
                continue
        except OSError:  # noqa: BLE001
            continue
        for v in variants:
            p = store / f"{v}.json"
            try:
                if p.is_file() and _has_payload(p):
                    return p
            except OSError:  # noqa: BLE001
                continue
    return None


def pointer_key_report(
    cache_dirs: Sequence[Path],
    memory_dirs: Sequence[Path],
    extra_keys: Optional[Sequence[str]] = None,
) -> PointerKeyReport:
    """Census: resolve every pointer key and report the ones that miss.

    ``cache_dirs`` / ``memory_dirs`` are the store roots to probe (typically
    the base + per-profile ``mempalace_cache`` and ``memories`` dirs — the
    same leaves the fixed retrieve path consults).  ``extra_keys`` is an
    optional list of pointer keys referenced elsewhere (e.g. copied from a
    recall block) that may have *no* backing file — these are the dangling
    pointers a key-store-only scan cannot see.

    A key is *resolved* when any store yields a non-null body for one of its
    cross-form variants; *unresolved* otherwise.  A resolved key whose backing
    file form differs from the canonical flat key is a cross-form hit (the
    pointer-key / drawer_id divergence GH-330 absorbs).
    """
    all_dirs = list(cache_dirs) + list(memory_dirs)
    scanned = 0
    for d in all_dirs:
        try:
            if d.is_dir():
                scanned += 1
        except OSError:  # noqa: BLE001
            continue

    found = discover_pointer_keys(all_dirs)
    keys = set(found)
    if extra_keys:
        for k in extra_keys:
            k = (k or "").strip()
            if _is_pointer_key(k):
                keys.add(_canonical_flat(k))

    report = PointerKeyReport(
        total_keys=len(keys),
        no_data=(len(keys) == 0),
        scanned_roots=scanned,
    )

    resolved = 0
    cross_form = 0
    unresolved: List[str] = []
    for key in sorted(keys):
        hit = resolve_pointer_key(key, cache_dirs, memory_dirs)
        if hit is None:
            unresolved.append(key)
            continue
        resolved += 1
        # Cross-form divergence: the backing file is NOT the canonical flat
        # form (i.e. stored as the drawer form, or an other-case variant).
        if hit.stem != key:
            cross_form += 1

    report.resolved = resolved
    report.unresolved = len(unresolved)
    report.cross_form_hits = cross_form
    report.unresolved_keys = unresolved
    return report
