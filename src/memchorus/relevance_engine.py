"""
Relevance Scoring Engine

Provides a scoring algorithm that evaluates memory sources and individual results
based on recency, source type, match quality, and context weight.  The orchestrator
uses these scores to rank multi-source search results rather than relying on a
hard-coded priority chain.

Design decisions (from Gap Analyses G1 + G2):
- Scores are normalised to [0.0, 1.0] so disparate dimensions are comparable.
- Context weighting is injected at the orchestrator layer, not in the engine itself,
  keeping the engine pure and testable.  The ``ContextWeight`` dataclass carries
  domain-level preferences (e.g. "memory" -> hermes_default).
- Scoring is additive per-dimension; weights sum to 1.0 by default but are overridable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

# ---------------------------------------------------------------------------
# Public API types
# ---------------------------------------------------------------------------


@dataclass
class ContextWeight:
    """Weights that influence source relevance for a given retrieval context.

    Attributes:
        domain_weights:  Maps domain names (e.g. 'memory') to source-name -> weight.
                        Sources not mentioned get a neutral weight of 0.25.
        recency_weight:  Normalised importance of the recency dimension (0..1).
        quality_weight:  Normalised importance of the text-match quality dimension (0..1).
        source_type_weight: Importance of the source-type bias (0..1).
    """

    domain_weights: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        "memory": {"hermes_default": 1.5, "mempalace": 0.5},
        "graph": {"mempalace": 1.5, "hermes_default": 0.5},
    })
    recency_weight: float = 0.30
    quality_weight: float = 0.45
    source_type_weight: float = 0.25


@dataclass
class RankedResult:
    """A single search result carrying its relevance score and provenance."""

    key: str
    content: Any
    source: str
    score: float
    # Extra metadata passed through from the MemorySource (e.g. timestamp)
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol for concrete sources to opt into self-scoring
# ---------------------------------------------------------------------------


class Scorched(Protocol):
    """A memory source that can attach a relevance score to its results."""

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]: ...
    def retrieve_with_score(
        self, key: str, context: Optional[ContextWeight] = None
    ) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Core Scorer class
# ---------------------------------------------------------------------------


class RelevanceScorer:
    """Evaluate and rank memory results using a multi-dimensional scoring model.

    Dimensions (weights configurable via ``ContextWeight``):
        1. **Text match quality** -- lexical overlap between query and result content
           (BM25-inspired unigram recall).  Default weight = 0.45.
        2. **Recency decay** -- exponential decay from the result timestamp using a
           half-life of 30 days (configurable via ``half_life_days``).  Default
           weight = 0.30.
        3. **Source-type bias** -- boosts the base probability assigned to each source.
           For example, 'hermes_default' gets priors={'hermes_default': 0.7} by default.
           Default weight = 0.25.
    """

    def __init__(
        self,
        half_life_days: float = 30.0,
        priors: Optional[Dict[str, float]] = None,
    ):
        self.half_life_days = half_life_days
        # Normalise priors to a probability distribution if provided
        if priors:
            total = sum(priors.values())
            self.priors = {k: v / total for k, v in priors.items()}
        else:
            self.priors = {"hermes_default": 0.7, "mempalace": 0.3}

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _score_recency(self, timestamp_str: Optional[str]) -> float:
        """Return a value in [0, 1] for ``timestamp_str`` (ISO-8601)."""
        if not timestamp_str:
            return 0.5  # neutral
        try:
            ts = datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            return 0.5
        delta = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
        if delta < 0:
            delta = 0  # future dates get neutral too
        decay = 0.5 ** (delta / max(self.half_life_days, 1))
        return float(decay)

    @staticmethod
    def _score_quality(query: str, content: Any) -> float:
        """Unigram overlap between ``query`` and *content* as a float in [0, 1]."""
        if not query or not content:
            return 0.3  # neutral when either side is empty
        q_terms = set(re.findall(r"\w+", query.lower()))
        c_text = str(content).lower()
        c_terms = set(re.findall(r"\w+", c_text))
        if not q_terms or not c_terms:
            return 0.3
        recall = len(q_terms & c_terms) / max(len(q_terms), 1)
        precision = len(q_terms & c_terms) / max(len(c_terms), 1)
        # F1-like metric (precision doesn't help much here; bias toward recall)
        return float(max(recall, precision))

    def _score_source_type(self, source: str) -> float:
        """Normalised prior for *source* in [0, 1]."""
        raw = self.priors.get(source, 0.5)
        # Scale to [0, 1] assuming priors are in [0, max_prior].
        max_prior = max(self.priors.values()) if self.priors else 1.0
        return float(raw / max(max_prior, 1e-9))

    def _score_domain_bias(
        self, source: str, domain: Optional[str], context: ContextWeight
    ) -> float:
        """Boost for the given ``domain`` (or neutral)."""
        if not domain or domain not in context.domain_weights:
            return 0.25
        weights = context.domain_weights[domain]
        raw = weights.get(source, 0.25)
        # Normalise: max weight in domain is the target ~1.0
        max_w = max(weights.values()) if weights else 1.0
        return float(raw / max(max_w, 1e-9)) * context.source_type_weight

    # ------------------------------------------------------------------
    # Public scoring
    # ------------------------------------------------------------------

    def score(
        self,
        result: Dict[str, Any],
        query: str,
        context: Optional[ContextWeight] = None,
    ) -> float:
        """Compute a single relevance score in [0, 1] for ``result``.

        Args:
            result: A dict produced by a MemorySource.search() call.
                    Expected keys: ``key``, ``content``, ``source``, plus optionally
                    ``timestamp`` and ``_domain`` (injected by the orchestrator).
            query: The original search query (used for quality).
            context: Optional weighting preferences from the caller.

        Returns:
            Float score in [0, 1].  Higher is more relevant.
        """
        if context is None:
            context = ContextWeight()

        content = result.get("content", "")
        source = result.get("source", "unknown")
        ts = result.get("timestamp")
        domain = result.get("_domain")

        quality = self._score_quality(query, content)
        recency = self._score_recency(ts)

        # Use domain-aware bias when the caller injected a _domain hint; fall back
        # to base source-type priors otherwise.  domain_bias already includes the
        # context.source_type_weight multiplier so we do not add it again.
        if domain:
            src_dim = self._score_domain_bias(source, domain, context)
        else:
            src_dim = self._score_source_type(source) * context.source_type_weight

        raw = context.quality_weight * quality + context.recency_weight * recency + src_dim

        return float(raw)

    def score_and_rank(
        self,
        results: List[Dict[str, Any]],
        query: str,
        context: Optional[ContextWeight] = None,
    ) -> List[RankedResult]:
        """Score each item in *results* and sort descending.

        Returns ``list[RankedResult]`` guaranteed sorted by score (highest first).
        Duplicate keys are removed -- the highest-scoring instance wins.
        """
        if context is None:
            context = ContextWeight()

        scored: Dict[str, RankedResult] = {}
        for r in results:
            s = self.score(r, query, context)
            key = r.get("key", str(r))
            if key not in scored or s > scored[key].score:
                scored[key] = RankedResult(
                    key=key,
                    content=r.get("content"),
                    source=r.get("source", "unknown"),
                    score=round(s, 4),
                    meta={k: v for k, v in r.items() if k not in ("key", "content", "source")},
                )

        ranked = sorted(scored.values(), key=lambda x: x.score, reverse=True)
        return ranked

    def select_best_source(
        self,
        results: List[Dict[str, Any]],
        query: str,
        context: Optional[ContextWeight] = None,
    ) -> Optional[RankedResult]:
        """Return the single best-ranked result, or ``None`` on empty input."""
        ranked = self.score_and_rank(results, query, context)
        return ranked[0] if ranked else None
