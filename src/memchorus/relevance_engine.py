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
import math
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


# ---------------------------------------------------------------------------
# Default recall-time penalty patterns (GH-100)
# Target content that passes write-time noise filters but still scores highly
# on keyword overlap: changelogs, package lists, empty API responses, etc.
# Each tuple: (label, compiled regex, multiplicative factor).
# Factor in (0, 1] — lower means the pattern kills score harder.
# If multiple patterns match, the MINIMUM factor applies (not multiplicative).
# ---------------------------------------------------------------------------

_DEFAULT_PENALTY_PATTERNS = [
    # Changelog / version diff entries (structured commit/release notes)
    (
        "changelog",
        re.compile(
            r'^\s*(?:-|[*•])\s+v?\d+\.\d+\.\d+',  # bullet followed by version number
            re.M | re.I,
        ),
        0.4,
    ),
    # Package / dependency list dumps (pip, npm, cargo style)
    # Detect 3+ lines matching constraint syntax — loose match avoids ^ anchor issues
    (
        "package_list",
        re.compile(
            r'(?:\w+\s*[<>=!~]+\s*[\d.*]+(?:\n|\s|$).*){3}',
            re.I,
        ),
        0.25,
    ),
    # Empty / trivially-successful API or tool responses
    (
        "empty_api_response",
        re.compile(
            r'(?s)^\s*\{\s*"(?:ok|success|status)"\s*:\s*(?:true|"ok"|"success"|200)\s*,?\s*\}',
            re.I,
        ),
        0.3,
    ),
    # Pure version metadata blocks (non-code) — detect 2+ version-like assignment lines
    (
        "version_block",
        re.compile(
            r'(?s)(?:^\s*version\s*[=:]\s*[\da-f.+]+).*?\n(?:\s*version\s*[=:]\s*[\da-f.+]+)',
            re.M | re.I,
        ),
        0.35,
    ),
]


class RelevanceScorer:
    """Evaluate and rank memory results using a multi-dimensional scoring model.

    Dimensions (weights configurable via ``ContextWeight``):
        1. **Text match quality** -- lexical overlap between query and result content
           (BM25-inspired unigram recall).  Default weight = 0.45.
        2. **Recency decay** -- exponential decay from the result timestamp using a
           half-life of 30 days (configurable via ``half_life_days``).  Optional
           two-tier mode: fast_window_days + fast_retention_pct give a higher,
           flatter plateau for recent operational context before standard decay
           takes over.  Default weight = 0.30.
        3. **Source-type bias** -- boosts the base probability assigned to each source.
           For example, 'hermes_default' gets priors={'hermes_default': 0.7} by default.
           Default weight = 0.25.

    Two-tier recency model (GH-99)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    When ``fast_window_days`` is provided, memories within that window retain a
    proportion of their score (controlled by ``fast_retention_pct``), then standard
    exponential decay kicks in for anything older.  This lets operational context
    — PR pushed, file changed, deploy completed — stay highly ranked for hours
    or days but fade rapidly once it becomes stale, while stable knowledge continues
    on its longer decay curve.

    Example (fast_window_days=7, fast_retention_pct=0.7):
      - Day 0: score = 1.0  (brand new)
      - Days 1-6: slow exponential decay from 1.0 toward 0.7 at the boundary (day 7).
        Decay inside the window is much gentler than the standard half-life so scores
        stay well above the single-curve model during this period.
      - After day 7: standard exponential decay resumes from 0.7 using ``half_life_days``

    If ``fast_window_days`` is None (the default), the previous single-curve
    model is preserved exactly, maintaining backward compatibility.

    Penalty patterns (GH-100):
        Configurable negative scoring rules applying multiplicative score reduction
        when content matches known low-signal noise classes that pass write-time filters.
        Patterns are precompiled at module load time. Multi-pattern overlap uses the
        minimum factor rather than compounding multiplicatively.
    """

    def __init__(
        self,
        half_life_days: float = 30.0,
        priors: Optional[Dict[str, float]] = None,
        fast_window_days: Optional[float] = None,
        fast_retention_pct: float = 0.7,
        penalty_patterns: Optional[List[Dict[str, Any]]] = None,
    ):
        self.half_life_days = half_life_days
        # Two-tier recency model (GH-99)
        self.fast_window_days = fast_window_days  # None disables two-tier mode
        self.fast_retention_pct = fast_retention_pct
        # Normalise priors to a probability distribution if provided
        if priors:
            total = sum(priors.values())
            self.priors = {k: v / total for k, v in priors.items()}
        else:
            self.priors = {"hermes_default": 0.7, "mempalace": 0.3}

        # GH-100: recall-time penalty patterns (label, compiled regex, factor)
        # Override with user config; fall back to _DEFAULT_PENALTY_PATTERNS.
        self._penalty_patterns = self._compile_penalty_patterns(
            penalty_patterns if penalty_patterns is not None else None  # None means defaults
        )

    @staticmethod
    def _compile_penalty_patterns(
        raw_patterns: Optional[List[Dict[str, Any]]] = None,
    ) -> List[tuple]:
        """Parse config-style penalty patterns into compiled (label, pattern, factor) tuples.

        If *raw_patterns* is None, returns the built-in defaults.
        Each dict should have keys: ``pattern`` (regex string), ``label`` (str),
        and ``factor`` (float in (0, 1]).  Invalid entries are logged and skipped.

        Returns a list of (label, compiled_regex, factor) tuples ready for matching.
        """
        if raw_patterns is None:
            # Fall back to built-in defaults (already compiled).
            return list(_DEFAULT_PENALTY_PATTERNS)

        compiled: List[tuple] = []
        for entry in raw_patterns:
            try:
                label = str(entry["label"])
                regex = re.compile(str(entry["pattern"]))
                factor = float(entry["factor"])
                if not 0 < factor <= 1.0:
                    logger.warning(
                        "Penalty pattern '%s' has factor %s (must be in (0, 1]) — skipping",
                        label, factor,
                    )
                    continue
                compiled.append((label, regex, factor))
            except (KeyError, TypeError, re.error) as exc:
                logger.warning(
                    "Skipping malformed penalty pattern entry %r: %s", entry, exc
                )
        return compiled

    def _apply_penalty_patterns(self, content_text: str) -> float:
        """Return the minimum multiplicative factor across all matching penalty patterns.

        If no penalties match or the penalty list is empty, returns ``1.0`` (no penalty).
        When multiple patterns hit the same content, we use the *minimum* factor
        rather than multiplying them together — this avoids over-penalizing content.

        Args:
            content_text: Plain text extracted from a result's content field.

        Returns:
            A float in ``(0, 1]`` representing the strongest applicable penalty factor.
        """
        if not self._penalty_patterns:
            return 1.0

        matched_factor = 1.0
        content_text = content_text or ""
        for label, pattern, factor in self._penalty_patterns:
            try:
                if pattern.search(content_text):
                    if factor < matched_factor:
                        matched_factor = factor
                        logger.debug(
                            "Penalty pattern '%s' matched — current min factor %.2f",
                            label, matched_factor,
                        )
            except Exception:
                # Defensive: a bad pattern shouldn't crash scoring
                logger.debug("Penalty pattern match failed for '%s'", label)
                continue

        result = round(matched_factor, 4)
        return result if result > 0 else 1.0

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _score_recency(self, timestamp_str: Optional[str]) -> float:
        """Return a value in [0, 1] for ``timestamp_str`` (ISO-8601).

        Two-tier recency model (GH-99): when self.fast_window_days is set, memories
        within that window retain a higher score controlled by fast_retention_pct,
        after which standard exponential decay takes over from the boundary value.
        """
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

        # Two-tier model (GH-99): fast_window keeps recency scores higher during the
        # first N days so operational context stays relevant, then standard decay.
        if self.fast_window_days is not None and delta > 0:
            window = max(self.fast_window_days, 0.01)
            base = max(self.half_life_days, 1)
            floor = min(max(self.fast_retention_pct, 0.0), 1.0)
            # Slower effective half-life inside the fast window keeps operational context
            # highly ranked longer than the standard curve would allow.
            slow_half_life = base / max(floor, 1e-9)

            if delta <= window:
                # Gentle plateau: decay is slower than standard half-life here
                return float(0.5 ** (delta / slow_half_life))
            else:
                # After window ends, continue standard decay — scaled from the boundary
                # value where the slow-curve left off to avoid any score cliff.
                boundary_score = 0.5 ** (window / slow_half_life)
                post_delta = delta - window
                return float(boundary_score * (0.5 ** (post_delta / base)))

        # Original single-curve behaviour when fast_window not configured
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

        # v1.9 recall-time relevance boosting (AC-RTB-1.x): multiply by calibration
        # engine boost factor so high-utility memories outrank unproven ones.
        # Graceful degradation: falls back to 1.0 on any error or missing tracker.
        try:
            entry_key = result.get("key", "")
            from memchorus.calibration_engine import CalibrationEngine
            boost = CalibrationEngine.boost_factor_for_key(CalibrationEngine(), entry_key)
            raw *= boost
        except Exception:
            logger.debug(
                "boost_factor unavailable for key %r — standard scoring path",
                result.get("key"),
            )

        # Bug 3 AC4: provenance penalty -- auto-captured content gets a multiplicative
        # factor (default 0.3) so it ranks below deliberately stored memories.
        if result.get("_auto_provenance") is True:
            raw *= auto_provenance_penalty

        # GH-100: recall-time penalty patterns — apply multiplicative score reduction
        # for known low-signal content that passed write-time noise filters but still
        # scores highly due to keyword overlap (changelogs, package lists, empty API
        # responses, version blocks). Multi-pattern overlap uses minimum factor.
        c_text = self._extract_content_text(content)
        penalty_factor = self._apply_penalty_patterns(c_text)
        raw *= penalty_factor

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
