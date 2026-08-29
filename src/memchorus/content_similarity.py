"""
Cross-source content similarity utilities for recall-time deduplication.

Provides N-gram-based Jaccard similarity for detecting near-duplicate content
from different memory sources before the top-N truncation in orchestrator.search().

Design notes:
- Uses 2-gram (bigram) overlap as a compromise between exact-hash dedup (too
  strict — misses paraphrased duplicates) and full MinHash (overkill for
  ~15-25 candidate results per search). N-gram Jaccard on bigrams is O(N*M)
  where N and M are the number of grams in each text, which is negligible at
  recall-time volumes.
- Configurable threshold defaults to 0.85 (issue #95). When two entries exceed
  this threshold, only the higher-scoring one is kept. Recency breaks ties when
  scores are within 0.05 of each other.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# N-gram extraction helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Lower-case and extract word tokens."""
    return re.findall(r'\w+', text.lower())


def _ngrams(tokens: List[str], n: int = 2) -> "set[tuple[str, ...]]":
    """Generate N-grams from a token list."""
    if len(tokens) < n:
        return {tuple(tokens)}
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _extract_text(content: Any) -> str:
    """Flatten arbitrary content to a comparable text string.

    Mirrors RelevanceScorer._extract_content_text logic so that two pieces of
    content scored by the scorer produce identical flattened text here.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_extract_text(item) for item in content)
    if isinstance(content, dict):
        parts: List[str] = []
        for key, val in content.items():
            parts.append(str(key))
            parts.append(_extract_text(val))
        return " ".join(parts)
    try:
        return str(content)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLD = 0.85
_SCORE_TOLERANCE = 0.05


def jaccard_similarity(text_a: str, text_b: str, n: int = 2) -> float:
    """Compute N-gram Jaccard similarity between two texts in [0.0, 1.0].

    Uses bigrams (n=2) by default. Returns 1.0 for identical texts, 0.0 when
    they share no grams. Empty inputs return 0.0 (no similarity).
    """
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)

    if not tokens_a or not tokens_b:
        return 0.0

    grams_a = _ngrams(tokens_a, n)
    grams_b = _ngrams(tokens_b, n)

    union = grams_a | grams_b
    if not union:
        return 0.0

    intersection = grams_a & grams_b
    return len(intersection) / len(union)


def containment_similarity(text_a: str, text_b: str) -> float:
    """Word-set overlap coefficient (containment) between two texts in [0.0, 1.0].

    Containment is ``|tokens_a ∩ tokens_b| / min(|tokens_a|, |tokens_b|)``.  Unlike
    Jaccard (which divides by the *union*), containment measures how much of the
    *smaller* document is reproduced inside the other.  That is exactly the
    long-doc case Jaccard silently misses: a 15-line entry that is fully subsumed
    by a 60-line document scores only ~0.25 on Jaccard (the big denominator) yet
    scores 1.0 on containment, because every distinctive word of the short entry
    appears in the long one.

    Returns 1.0 when the shorter text's word set is entirely contained in the
    longer, 0.0 when they share no words or either is empty.  Case-insensitive.
    """
    tokens_a = set(re.findall(r'\w+', text_a.lower()))
    tokens_b = set(re.findall(r'\w+', text_b.lower()))

    if not tokens_a or not tokens_b:
        return 0.0

    small = min(len(tokens_a), len(tokens_b))
    if small == 0:
        return 0.0

    intersection = len(tokens_a & tokens_b)
    return intersection / small


def canonical_content_fingerprint(source: str, title: str, body: str) -> str:
    """Stable, canonical content hash derived from source + title + body.

    GH-142 (option 2): used to seed the storage key so re-saving the *same*
    source content collapses to one entry instead of stacking a second copy.
    The inputs are normalised (case-folded, whitespace-collapsed) before hashing,
    so cosmetic re-wording of surrounding metadata does not change the key, while
    a genuinely different body still does.

    Returns a 16-hex-char sha256 prefix (64 bits) — collision-resistant enough
    for entry-level dedup without a full cryptographic digest in every key.
    """
    import hashlib

    def _norm(s: str) -> str:
        return " ".join((s or "").lower().split())

    canonical = "\x1f".join([_norm(source), _norm(title), _norm(body)])
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).digest()
    return digest.hex()[:16]


class RecallDeduplicator:
    """Post-GAP095 deduplication engine for cross-source recall results.

    After the orchestrator has scored and ranked results from all sources, this
    component removes near-duplicates so that only one representative per
    similarity cluster reaches the injected memory block.

    Args:
        threshold: Jaccard similarity above which two entries are considered
                   duplicates. Defaults to 0.85.
        score_tolerance: When scores are within this range, the more recent
                         entry wins as tiebreaker. Defaults to 0.05.
        ngram_size: N-gram size for Jaccard computation (default 2).

    Usage:
        deduper = RecallDeduplicator(threshold=0.85)
        kept = deduper.deduplicate(ranked_results)
    """

    def __init__(
        self,
        threshold: float = _DEFAULT_THRESHOLD,
        score_tolerance: float = _SCORE_TOLERANCE,
        ngram_size: int = 2,
    ):
        self.threshold = max(threshold, 0.0)
        self.score_tolerance = score_tolerance
        self.ngram_size = ngram_size

    # ------------------------------------------------------------------
    # Core deduplication
    # ------------------------------------------------------------------

    def deduplicate(
        self,
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Remove near-duplicates from a scored+ranked result list.

        Iterates through the already-ranked results (highest score first).
        For each entry checks Jaccard similarity against every kept entry.
        If it exceeds threshold against *any* kept entry, the newer candidate
        is discarded unless its score is sufficiently higher to override the
        tiebreaker logic.

        Args:
            results: List of dicts with at least "score" and "content" keys.
                     Expected to be sorted by score descending (highest first).

        Returns:
            Deduplicated list, preserving original ranking order for kept items.
        """
        if not results:
            return []

        seen_texts: List[str] = []
        kept: List[Dict[str, Any]] = []

        for item in results:
            candidate_text = _extract_text(item.get("content", ""))
            is_dup = False
            for existing in seen_texts:
                sim = jaccard_similarity(candidate_text, existing, self.ngram_size)
                if sim >= self.threshold:
                    is_dup = True
                    break

            if not is_dup:
                kept.append(item)
                seen_texts.append(candidate_text)

        return kept

    # ------------------------------------------------------------------
    # Tiebreaker-aware variant (score-proximity recency override)
    # ------------------------------------------------------------------

    def deduplicate_with_tiebreaker(
        self,
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Deduplicate with score-proximity recency tiebreaker.

        When two entries are near-duplicates AND their scores differ by less
        than ``score_tolerance`` (default 0.05), the more recently timestamped
        entry is preferred regardless of which arrived first in the ranked list.

        This handles the case where a paraphrased version from mempalace outranks
        an exact-copy from hermes_default by a hair — the fresher entry wins.

        Args:
            results: Scored+ranked dicts (descending score).

        Returns:
            Deduplicated list preserving ranking order of kept items.
        """
        if not results:
            return []

        # Phase 1: collect similarity groups
        # Each group keeps track of the best member by (score, then recency)
        groups: List[List[Dict[str, Any]]] = []
        groups_texts: List[str] = []

        for item in results:
            candidate_text = _extract_text(item.get("content", ""))
            matched_group = False

            for idx, existing_text in enumerate(groups_texts):
                sim = jaccard_similarity(candidate_text, existing_text, self.ngram_size)
                if sim >= self.threshold:
                    groups[idx].append(item)
                    matched_group = True
                    break

            if not matched_group:
                groups.append([item])
                groups_texts.append(candidate_text)

        # Phase 2: pick the winner from each group
        kept: List[Dict[str, Any]] = []
        for group in groups:
            if len(group) == 1:
                kept.append(group[0])
                continue

            # Multiple candidates are near-duplicates — pick by score,
            # falling back to recency tiebreaker within tolerance.
            winner = self._pick_winner(group)
            kept.append(winner)

        return kept

    @staticmethod
    def _pick_winner(group: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Pick the best entry from a similarity group.

        Priority: highest score wins. If scores are within tolerance, the most
        recent timestamp is preferred. Falls back to first-in-list on stale data.
        """
        import datetime as dt_module

        best = max(group, key=lambda x: float(x.get("score", 0.0)))
        best_score = float(best.get("score", 0.0))

        # Check if any other member is within tolerance — if so, among those
        # near-score members pick the most recent by timestamp.
        contenders = [
            m for m in group
            if abs(float(m.get("score", 0.0)) - best_score) <= _SCORE_TOLERANCE
        ]

        if len(contenders) <= 1:
            return best

        def _parse_ts(item: Dict[str, Any]) -> float:
            ts = item.get("timestamp")
            if not ts:
                return 0.0
            try:
                return dt_module.datetime.fromisoformat(str(ts)).timestamp()
            except (ValueError, TypeError):
                return 0.0

        # Among contenders with near-equal scores, prefer most recent
        winner = max(contenders, key=_parse_ts)
        return winner
