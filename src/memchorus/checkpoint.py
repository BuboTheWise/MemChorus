"""Session-end auto-checkpoint for the active project (IMPL #208).

The #163 chain already gave MemChorus the *read* side of a ``project:<name>``
record (``MemoryOrchestrator.resolve_project_record`` → keyed, deterministic,
independent of ranked recall).  What was missing was the *write* side: nothing
ever refreshed that record from a live working session, so the next
``on_session_start`` resolved a stale root (or only a rule-derived default).

This module fills that gap.  At ``on_session_end`` we capture, for the active
project:

* a **verified** canonical root (the working directory the session actually
  operated in — ``source=ssot:session-checkpoint#<slug>``,
  ``verified_at=<now UTC>``), and
* a terse **working-state summary** (the session's most recent user intent as
  the ``standard.gist`` plus a small set of salient ``standard.topics``),

and persist it under the same ``project:<slug>`` key the reader resolves.  The
payload is validated against the §2.3 contract *here* (fail-loud is safe in a
teardown path because we catch and degrade), never relying on the store being
forgiving.

Design constraints (kept deliberately small):
* No orchestrator import at module top-level — the orchestrator is injected at
  call time so the module stays unit-testable and never drags the runtime graph.
* Every public entry point degrades to ``None``/``False`` on any error; a
  checkpoint write must never break ``on_session_end`` teardown.
* Reuses :mod:`memchorus.project_record` for key normalization and §2.4 defaults,
  and :func:`memchorus.orientation._resolve_project` for project detection, so
  there is exactly one canonical "active project" seam in the codebase.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Source marker stamped on a checkpoint-derived root.  Matches the §2.1
#: ``location.source`` rule ``^(ssot|derived):.+$`` — this is *verified* by
#: session (we were actually working here), not rule-derived.
CHECKPOINT_SOURCE_PREFIX = "ssot:session-checkpoint#"

# --------------------------------------------------------------------------- #
# Session summary heuristics (no LLM — deterministic, bounded, silent)         #
# --------------------------------------------------------------------------- #

#: Stop-words / function words that carry no topic signal.
_STOP = frozenset({
    "a", "an", "and", "as", "at", "be", "by", "do", "for", "from", "have", "i",
    "in", "into", "it", "its", "is", "me", "my", "no", "not", "of", "off", "on",
    "or", "please", "so", "that", "the", "this", "to", "was", "were", "what",
    "when", "where", "which", "while", "who", "why", "will", "with", "won't",
    "you", "your", "can", "could", "get", "just", "let", "make", "need", "now",
    "then", "there", "these", "they", "them", "each", "again", "some",
    "than", "too", "very", "about", "after", "also", "been", "before", "being",
    "below", "between", "both", "but", "did", "either", "else", "even", "every",
    "few", "had", "has", "here", "how", "if", "many", "must", "more", "most",
    "other", "our", "out", "over", "own", "same", "she", "should", "such",
    "take", "under", "up", "us", "we", "well", "would", "all", "am", "one",
    "thanks", "thank", "help", "hmm", "ok", "okay",
})


def _ts_utc_iso8601() -> str:
    """Current UTC instant in the §2.3 accepted form (``...T..Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_topics(text: str, limit: int = 6) -> List[str]:
    """Derive up to *limit* salient topic terms from *text* (frequency-ranked).

    Deterministic and dependency-free: tokenizer → stop-word filter →
    frequency sort (ties broken alphabetically).  Each term is kept only if it
    is a clean lowercase slug of <= 24 chars (the §2.2 ``standard.topics`` cap).
    """
    words = re.findall(r"[a-z][a-z0-9_./-]{2,23}", (text or "").lower())
    freq: Dict[str, int] = {}
    for w in words:
        # A bare stop-word (or a path/ident that is purely functional) is skipped.
        if w in _STOP:
            continue
        freq[w] = freq.get(w, 0) + 1
    if not freq:
        return []
    ranked = sorted(freq.keys(), key=lambda w: (-freq[w], w))
    out: List[str] = []
    for w in ranked:
        if len(w) > 24:  # defensive re-check against the field cap
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _gist_from_session(user_texts: List[str], max_chars: int = 120) -> str:
    """Condense the session's most recent user intent to a §2.2 ``gist`` line.

    Prefers the *last* substantive user message (the freshest intent), collapses
    whitespace, and hard-caps at ``max_chars`` (the ``GIST_MAX_CHARS`` field cap)
    with a ``…`` ellipsis so a long turn can never trip the validator.
    """
    candidates = [t.strip() for t in (user_texts or []) if t and t.strip()]
    if not candidates:
        return ""
    text = " ".join(candidates[-1].split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "\u2026"


# --------------------------------------------------------------------------- #
# Record construction                                                         #
# --------------------------------------------------------------------------- #

def build_checkpoint_record(
    project_name: str,
    cwd: str,
    user_texts: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build the ``project:<slug>`` key + value for the session checkpoint.

    Returns ``(key, value)`` where *value* is a ``build_record``-shaped payload
    (``location`` + ``standard``) that satisfies the §2.3 contract:

    * ``location.canonical_root`` — the working directory (a *verified* root),
      ``source=CHECKPOINT_SOURCE_PREFIX <slug>``, ``verified_at=<now UTC>``.
    * ``standard`` — the repo §2.4 default pointer (valid ``skill``/``doc_path``)
      carrying the session ``gist`` + ``topics``; the default pointer is kept so
      the record remains valid even when the session produced no usable text.

    Raises:
        ValueError: if *project_name* is empty (caller should skip instead).
    """
    if not project_name or not str(project_name).strip():
        raise ValueError("build_checkpoint_record: project_name is required")

    from memchorus.project_record import (
        build_record,
        default_location,
        default_standard,
        normalize_project_key,
    )

    key = normalize_project_key(name=project_name.strip())
    slug = key.split(":", 1)[1]

    # Location — the verified working root.  Reuse the §2.4 defaults to get a
    # well-formed object, then override with the *verified* cwd (we were
    # actually operating in this directory during the session) and stamp it.
    loc = dict(default_location(project_name))
    loc["canonical_root"] = (cwd or loc["canonical_root"]).rstrip()
    if not str(loc["canonical_root"]).strip():
        loc = dict(default_location(project_name))
    loc["source"] = "%s%s" % (CHECKPOINT_SOURCE_PREFIX, slug)
    loc["verified_at"] = _ts_utc_iso8601()

    # Standard — default valid pointer, with the session summary attached.
    std = dict(default_standard())
    user_texts = user_texts or []
    gist = _gist_from_session(user_texts)
    if gist:
        std["gist"] = gist
    topics = _extract_topics(" ".join(user_texts))
    if topics:
        std["topics"] = topics

    return key, build_record(location=loc, standard=std)


def write_checkpoint(
    orchestrator: Any,
    project_name: Optional[str],
    cwd: Optional[str] = None,
    user_texts: Optional[List[str]] = None,
) -> Optional[str]:
    """Persist the session checkpoint via *orchestrator*.save.

    Degrades silently: returns the saved key on success, ``None`` on any
    missing-input / orchestrator-absent / validation / write failure.  A
    checkpoint is best-effort telemetry for the *next* session — it must never
    raise into the ``on_session_end`` teardown path.
    """
    try:
        import os

        if project_name is None:
            return None
        project_name = str(project_name).strip()
        if not project_name:
            return None
        if cwd is None:
            cwd = os.getcwd()

        key, value = build_checkpoint_record(
            project_name=project_name,
            cwd=cwd or "",
            user_texts=user_texts,
        )

        save = getattr(orchestrator, "save", None)
        if not callable(save):
            logger.debug("checkpoint: orchestrator has no save() — skipping")
            return None

        ok = save(
            key=key,
            value=value,
            source_name=None,
            category="SESSION",
            metadata={"provenance": "memchorus.session-checkpoint", "kind": "checkpoint"},
        )
        if ok:
            logger.info("checkpoint: wrote session checkpoint '%s'", key)
            return key
        logger.debug("checkpoint: save() at source returned falsy for '%s'", key)
        return None
    except Exception as exc:  # pragma: no cover - deliberate teardown guard
        logger.debug("checkpoint: write failed — degrading. %s", exc)
        return None


__all__ = [
    "CHECKPOINT_SOURCE_PREFIX",
    "build_checkpoint_record",
    "write_checkpoint",
]
