"""
recall_config — Named config keys for the MemChorus recall-loop north-star spec.

THIS IS THE SINGLE NAMED SOURCE for every cross-pillar recall-loop parameter
used by the implementation of spec sections §4–§6 (cards #223, #224, #225).

All values are frozen defaults.  Downstream code MAY override them at
request time for test / channel-variant purposes, but the canonical spec
defaults live **only** here so that drift is caught at one site by
``test_recall_config_root.py``.

Spec file
---------
``Projects/MemChorus/MemChorus-Recall-Loop-NorthStar-Spec.md``
(authoritative copy maintained outside this repo; the section numbers below
are the contract source for every value in this module)

  §4.2  Five base-dimension weights (Σ must equal 1.0 — asserted 1-AC2)
  §4.3  Item budget K, token budget B_tokens
  §5.2  Supersession attenuation (SUPERSESSION_ATTENUATION = 0.50)
  §6.2  Reserved augmentation slots (RESERVED_SLOTS: 1 implied / 2 explicit)
  §4.2  Recency λ horizon (τ ≈ 30 days)

Import pattern
--------------

    from memchorus.recall_config import RECALL_CONFIG

    k     = RECALL_CONFIG.k_implied                # 3
    b_tok = RECALL_CONFIG.b_tokens_implied        # 600
    w     = RECALL_CONFIG.base_weights            # .query_match, .recency, ...

    # Token estimator (deterministic, §4.3)
    from memchorus.recall_config import estimate_tokens
    est = estimate_tokens(content)                # len(content) // 4
"""

from __future__ import annotations

__all__ = [
    "BaseDimensionWeights",
    "RecallLoopConfig",
    "RECALL_CONFIG",
    "estimate_tokens",
]

# ---------------------------------------------------------------------------
# Five base-dimension weights  (spec §4.2 — asserted Σ = 1.0 in 1-AC2)
# ---------------------------------------------------------------------------

# The new task-aware selection function (pillar 1, #223) uses these five
# dimensions.  They are intentionally *different* from the legacy three-
# weight model in ``relevance_engine.py`` (quality=0.45 / recency=0.30 /
# source=0.25) which remains the runtime scorer for backwards compatibility.
#
# Spec §4.2 table:
#   Dimension            Weight
#   query_match          0.34
#   recency              0.20
#   strength             0.16
#   domain_relevance     0.14
#   source_channel       0.16
#   ─────────────────────────────
#   Σ                   1.00   ← asserted in test_recall_config_root.py

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BaseDimensionWeights:
    """Five base scoring dimensions defined by spec §4.2.

    These are the *base* score weights (before any additive boosts).
    ``Σ = 1.00`` is an invariant asserted in the test suite (1-AC2).
    Override per-request is allowed; the canonical values are here.
    """

    query_match: float = 0.34
    recency: float = 0.20
    strength: float = 0.16
    domain_relevance: float = 0.14
    source_channel: float = 0.16

    @property
    def total(self) -> float:
        """Σ of all five base weights — must equal 1.0 within 1e-9 (1-AC2)."""
        return (
            self.query_match
            + self.recency
            + self.strength
            + self.domain_relevance
            + self.source_channel
        )


# ---------------------------------------------------------------------------
# Aggregate config root  (spec §4.2, §4.3, §5.2, §6.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecallLoopConfig:
    """
    THE config root for the recall-loop north-star spec.

    Every pillar IMPL reads its named constants from this object:

      Pillar 1 (#223) — ``base_weights``, ``k_*``, ``b_tokens_*``,
                        ``recency_tau_days``
      Pillar 2 (#224) — ``supersession_attenuation``
      Pillar 3 (#225) — ``reserved_slots_*``

    All fields are the spec defaults.  No code should contain a second
    copy of these numbers; use ``RECALL_CONFIG`` (the module-level
    singleton) or pass an alternative ``RecallLoopConfig`` instance
    where the API permits override.
    """

    # -- Five base dimension weights (§4.2 — Σ = 1.0, asserted 1-AC2) ----
    base_weights: BaseDimensionWeights = BaseDimensionWeights()

    # -- Item budget K (§4.3) --------------------------------------------
    # K = 3  for implied (decision-point / auto) recall
    # K = 10 for explicit (user / agent-requested) recall
    k_implied: int = 3
    k_explicit: int = 10

    # -- Token budget B_tokens (§4.3) -------------------------------------
    # 600  tokens for implied recall
    # 2500 tokens for explicit recall
    b_tokens_implied: int = 600
    b_tokens_explicit: int = 2500

    # -- Reserved augmentation slots (§6.2, pillar 3) --------------------
    # Max reserved slots (across all three sources combined):
    #   1 for implied recall
    #   2 for explicit recall
    reserved_slots_implied: int = 1
    reserved_slots_explicit: int = 2

    # -- Supersession attenuation (§5.2, pillar 2) ------------------------
    # Superseded-but-valid facts get score *= 0.50 at stage 4 (post-score).
    supersession_attenuation: float = 0.50

    # -- Recency λ horizon (§4.2 — τ ≈ 30 days, injectable) ---------------
    # Used by the exponential-decay recency dimension:
    #   recency_score = exp(-age_days / recency_tau_days)
    # where age_days = (as_of - captured_at).days
    recency_tau_days: float = 30.0


# ---------------------------------------------------------------------------
# Module-level singleton — the canonical instance every pillar imports
# ---------------------------------------------------------------------------

RECALL_CONFIG: RecallLoopConfig = RecallLoopConfig()


# ---------------------------------------------------------------------------
# Token estimator  (§4.3 — deterministic, injectable)
# ---------------------------------------------------------------------------


def estimate_tokens(content: Optional[str]) -> int:
    """
    Deterministic token estimate for injection budget enforcement.

    Spec §4.3: "estimated by a deterministic ``len(content) // 4`` estimate,
    injectable/replacable by a real counter."

    This is the DEFAULT estimator.  Callers that need real token counts
    (e.g. tiktoken / cl100k) may supply their own ``Callable[[str], int]``
    at the API boundary; this function exists so that the budget logic in
    the pipeline has a single canonical default that is trivially
    testable and reproducible.

    Returns 0 for ``None`` or empty input.
    """
    if content is None:
        return 0
    if not isinstance(content, str):
        content = str(content)
    return len(content) // 4
