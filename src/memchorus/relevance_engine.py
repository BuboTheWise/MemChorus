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

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

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
            logger.warning(
                "Recency scorer: timestamp %s is in the future (delta=%.2f days). "
                "Check clock skew or manual edit on the data source.",
                timestamp_str, abs(delta),
            )
            delta = 0
        decay = 0.5 ** (delta / max(self.half_life_days, 1))
        return float(decay)

    @staticmethod
    def _extract_content_text(content: Any) -> str:
        """Extract readable text from any content type for quality scoring.

        Handles plain strings, dicts (keys + leaf values joined), lists (elements joined).
        This fixes the bug where dict/list content from MemPalace _from_str()
        was losing all semantic overlap with query terms when converted via str().
        """
        if not content:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                try:
                    parts.append(RelevanceScorer._extract_content_text(item))
                except (TypeError, ValueError):
                    parts.append(str(item))
            return " ".join(parts)
        if isinstance(content, dict):
            parts = []
            for key, val in content.items():
                parts.append(str(key))
                try:
                    parts.append(RelevanceScorer._extract_content_text(val))
                except (TypeError, ValueError):
                    parts.append(str(val))
            return " ".join(parts)
        # Fallback to string representation for anything else
        try:
            return str(content)
        except Exception:
            return ""

    @staticmethod
    def _score_quality(query: str, content: Any) -> float:
        """Normalised quality score in [0, 1] from unigram F1 between query & content.

        Formula::

            Terms are extracted via ``\\w+`` word boundaries on both sides.

                recall    = |Q ∩ C| / max(|Q|, 1)
                precision = |Q ∩ C| / max(|C|, 1)
                F1      = 2 * (prec * rec) / (prec + rec)

        Accepts any content type (str, dict, list).  Non-string types are first
        flattened via :meth:`_extract_content_text`.

        Edge cases:
            - Empty query or empty content -> 0.3 (neutral floor)

            - Zero precision AND zero recall -> 0.0
        """
        if not query or not content:
            return 0.3  # neutral when either side is empty
        c_text = RelevanceScorer._extract_content_text(content).lower()
        q_terms = set(re.findall(r"\w+", query.lower()))
        c_terms = set(re.findall(r"\w+", c_text))
        if not q_terms or not c_terms:
            return 0.3
        recall = len(q_terms & c_terms) / max(len(q_terms), 1)
        precision = len(q_terms & c_terms) / max(len(c_terms), 1)
        # F-max metric (bias toward whichever dimension is larger)
        # F1 harmonic mean of recall and precision
        # Penalizes imbalance: if one dimension is zero, quality should be zero
        if recall + precision == 0:
            return 0.0
        f1 = 2 * (precision * recall) / (precision + recall)
        return float(f1)

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

    @staticmethod
    def _guess_domain(query: str, context: ContextWeight) -> Optional[str]:
        """Heuristic to pick the most relevant domain for a query based on keyword overlap."""
        if not query or not context.domain_weights:
            return None
        q_terms = set(re.findall(r"\w+", query.lower()))
        best_domain = None
        best_count = 0
        for domain in context.domain_weights:
            d_terms = set(re.findall(r"\w+", domain.lower()))
            hits = len(q_terms & d_terms)
            if hits > best_count:
                best_count = hits
                best_domain = domain
        return best_domain if best_count > 0 else None

    # ------------------------------------------------------------------
    # Public scoring
    # ------------------------------------------------------------------

    def score(
        self,
        result: Dict[str, Any],
        query: str,
        context: Optional[ContextWeight] = None,
        score_max: float = 1.0,
        auto_provenance_penalty: float = 0.3,
    ) -> float:
        """Compute a single relevance score in [0, 1] for ``result``.

        Scoring formula
        ~~~~~~~~~~~~~~~
        Each dimension produces a value in [0, 1]:
            quality   -- F1 of unigram recall/precision between query & content
            recency   -- exponential decay (half-life) from result timestamp -> 1.0 when brand new
            src_dim   -- normalised source prior (domain-aware if _domain hint present)

        The three raw dimension weights are first L1-normalised so they sum to 1.0,
        ensuring the weighted combination of three [0, 1] components also lands in
        [0, 1].  A final min/max clamp serves as a safety net against floating-point
        drift or caller-supplied weight anomalies.

        Normalisation:        w_q' = w_q / (w_q + w_r + w_s)

        Bug 3 addition (AC4): auto_provenance_penalty parameter applies a multiplicative
            factor to results that contain ``_auto_provenance: True`` in their metadata,
            down-weighting automatically-captured content so it ranks below deliberately
            stored memories.  Default factor = 0.3 (i.e., the raw score is multiplied by
            0.3 for auto-stored items).

        Args:
            result: A dict produced by a MemorySource.search() call.
                    Expected keys: ``key``, ``content``, ``source``, plus optionally
                    ``timestamp`` and ``_domain`` (injected by the orchestrator).
                    Auto-provenance marker: ``_auto_provenance`` set to True for auto-captured.
            query: The original search query (used for quality).
            context: Optional weighting preferences from the caller.  Weights are
                     normalised before use so they always sum to 1.0 regardless of
                     the absolute values provided.
            score_max: Hard ceiling for the returned value (default ``1.0``).
                       Raise if you want a wider range, but [0, 1] is the
                       documented contract and safest for downstream consumers.
            auto_provenance_penalty: Multiplicative penalty applied to auto-captured
                content (default 0.3 so that auto-stored memories get 30% of their
                raw score).

        Returns:
            Float score in ``[0, score_max]``.  Higher is more relevant.
        """
        if context is None:
            context = ContextWeight()

        content = result.get("content", "")
        source = result.get("source", "unknown")
        ts = result.get("timestamp")
        domain = result.get("_domain")

        quality = self._score_quality(query, content)
        recency = self._score_recency(ts)

        # Use domain-aware bias when the caller injected a _domain hint;
        # if not explicitly set, try to infer it from query terms.
        domain_raw = result.get("_domain")
        if domain_raw is None:
            domain_raw = self._guess_domain(query, context)

        # -- Source component ------------------------------------------------------------------
        # The source_type_weight factor must be pulled *into* the L1 normalisation step
        # below, so we compute the unweighted [0, 1] prior first and apply weights later.
        if domain_raw:
            src_prior = (
                float(
                    context.domain_weights.get(domain_raw, {}).get(source, 0.25)
                    / max(
                        max(context.domain_weights[domain_raw].values(), default=1.0),
                        1e-9,
                    )
                )
            )
        else:
            src_prior = self._score_source_type(source)

        # -- L1-normalise the three dimension weights ------------------------------------------
        qw = context.quality_weight
        rw = context.recency_weight
        sw = context.source_type_weight
        w_sum = qw + rw + sw

        if w_sum > 0:
            qw_n, rw_n, sw_n = qw / w_sum, rw / w_sum, sw / w_sum
        else:
            # All weights are zero -- fall back to equal contribution
            qw_n = rw_n = sw_n = 1.0 / 3.0

        raw = qw_n * quality + rw_n * recency + sw_n * src_prior

        # Bug 3 AC4: provenance penalty -- auto-captured content gets a multiplicative
        # factor (default 0.3) so it ranks below deliberately stored memories.
        if result.get("_auto_provenance") is True:
            raw *= auto_provenance_penalty

        # Safety clamp (floating-point drift / user error guard)
        return float(min(max(raw, 0.0), score_max))

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
                    # Exclude 'score' so the RelevanceScorer's normalized value is not
                    # overwritten by the raw source-level word-count score (G3 fix).
                    meta={k: v for k, v in r.items() if k not in ("key", "content", "source", "score")},
                )

        ranked = sorted(scored.values(), key=lambda x: x.score, reverse=True)
        return ranked

    def rank_sources(
        self,
        source_names: List[str],
        *,
        context: Optional[ContextWeight] = None,
    ) -> List[str]:
        """Rank *source_names* by source-type bias alone (no quality/recency).

        This is a lightweight alternative to ``score_and_rank`` for the case where
        there are no content results yet — only source priors and optional domain
        hint from *context.domain_weights* matter.

        Returns a list of source names sorted by descending bias score.  Ties (within
        1e-9) preserve input order (stable sort).

        The ``source_type_weight`` controls how much domain-appropriateness overrides
        default priors: at 0 the ranking is pure prior; at 1 it ignores the prior
        entirely and ranks only by domain fit.
        """
        if not source_names:
            return []

        if context is None:
            context = ContextWeight()

        source_type_w = context.source_type_weight
        max_prior = max(self.priors.values()) if self.priors else 1.0

        pairs: list[tuple[str, float]] = []
        for name in source_names:
            # Normalized default prior (how reliable is this source by default?)
            prior_component = (self.priors.get(name, 0.5) / max(max_prior, 1e-9))

            # Domain-aware component (average normalized fit across all domains)
            domain_component = None
            if context.domain_weights:
                scores_for_domain = []
                for weights in context.domain_weights.values():
                    w_val = weights.get(name, 0.25)
                    max_w = max(weights.values()) if weights else 1.0
                    norm_w = w_val / max(max_w, 1e-9)
                    scores_for_domain.append(norm_w)
                if scores_for_domain:
                    domain_component = sum(scores_for_domain) / len(scores_for_domain)

            # Blend: higher source_type_weight → domain fit matters more vs default prior
            if domain_component is not None and source_type_w > 0:
                final = (1 - source_type_w) * prior_component + context.source_type_weight * domain_component
            else:
                final = prior_component

            pairs.append((name, final))

        ranked, _ = zip(*sorted(pairs, key=lambda t: -t[1]), strict=True)
        return list(ranked)

    def select_best_source(
        self,
        results: List[Dict[str, Any]],
        query: str,
        context: Optional[ContextWeight] = None,
    ) -> Optional[RankedResult]:
        """Return the single best-ranked result, or ``None`` on empty input."""
        ranked = self.score_and_rank(results, query, context)
        return ranked[0] if ranked else None
