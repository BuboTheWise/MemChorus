"""
test_recall_config_root — RED→GREEN suite for the cross-pillar config-keys
root (IMPL #223/#224/#225, card t_75149f72).

Spec:          Projects/MemChorus/MemChorus-Recall-Loop-NorthStar-Spec.md
       §4.2  base-dimension weights (Σ = 1.0, asserted 1-AC2)
       §4.3  item budget K / token budget B_tokens
       §5.2  supersession attenuation
       §6.2  reserved augmentation slots
       §4.2  recency τ (lambda horizon)

Acceptance criteria from the Kanban card body:
  AC-A: a single named-source config object exists in src/memchorus/
        exposing EXACTLY: k_implied, k_explicit, b_tokens_implied,
        b_tokens_explicit, reserved_slots_implied, reserved_slots_explicit,
        supersession_attenuation, recency_tau_days, and the five base weights.
  AC-B: every key resolves (no None, no 0-where-nonzero-expected).
  AC-C: Σ of the five base-dimension weights = 1.00 within 1e-9.
  AC-D: auto_recall_engine no longer contains a bare literal ``3`` in its
        limit logic — it must import and use a named constant from
        ``memchorus.recall_config``.

These tests are deterministic, need no network, no live backends, no GPU.
Run serially (no xdist):
    PYTHONPATH=src pytest tests/test_recall_config_root.py -v
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures / imports
# ---------------------------------------------------------------------------

# Force the src tree onto PATH so the module resolves from the worktree.
SRC = Path(__file__).resolve().parent.parent / "src"
os.environ.setdefault("PYTHONPATH", str(SRC))

try:
    from memchorus.recall_config import (
        BaseDimensionWeights,
        RecallLoopConfig,
        RECALL_CONFIG,
        estimate_tokens,
    )
except ImportError as exc:
    pytest.fail(
        f"memchorus.recall_config is not importable. "
        f"Is it on the Python path? Error: {exc}"
    )


# ---------------------------------------------------------------------------
# AC-A: the named-source config object exists and exposes all required keys
# ---------------------------------------------------------------------------


class TestConfigObjectExists:
    """AC-A — a single named config object exists in src/memchorus/."""

    def test_reconciling_config_is_frozen(self) -> None:
        """The config must be immutable (frozen dataclass)."""
        with pytest.raises(Exception):
            RECALL_CONFIG.k_implied = 99  # type: ignore[misc]

    def test_all_spec_keys_present(self) -> None:
        """Every key named in the spec must be a field on RecallLoopConfig."""
        required_keys = {
            "k_implied",
            "k_explicit",
            "b_tokens_implied",
            "b_tokens_explicit",
            "reserved_slots_implied",
            "reserved_slots_explicit",
            "supersession_attenuation",
            "recency_tau_days",
            "base_weights",
        }
        actual = {f.name for f in RecallLoopConfig.__dataclass_fields__.values()}
        missing = required_keys - actual
        assert missing == set(), f"Missing keys on RecallLoopConfig: {missing}"

    def test_base_weights_has_all_five_dimensions(self) -> None:
        """§4.2 — all five base-dimension weights must be present."""
        required = {
            "query_match",
            "recency",
            "strength",
            "domain_relevance",
            "source_channel",
        }
        actual = {f.name for f in BaseDimensionWeights.__dataclass_fields__.values()}
        missing = required - actual
        assert missing == set(), f"Missing base-dimension weights: {missing}"


# ---------------------------------------------------------------------------
# AC-B: every key resolves to the spec-defined default
# ---------------------------------------------------------------------------


class TestSpecDefaults:
    """AC-B — every key resolves to the value in the spec."""

    def test_k_implied_defaults_to_3(self) -> None:
        assert RECALL_CONFIG.k_implied == 3

    def test_k_explicit_defaults_to_10(self) -> None:
        assert RECALL_CONFIG.k_explicit == 10

    def test_b_tokens_implied_defaults_to_600(self) -> None:
        assert RECALL_CONFIG.b_tokens_implied == 600

    def test_b_tokens_explicit_defaults_to_2500(self) -> None:
        assert RECALL_CONFIG.b_tokens_explicit == 2500

    def test_reserved_slots_implied_defaults_to_1(self) -> None:
        assert RECALL_CONFIG.reserved_slots_implied == 1

    def test_reserved_slots_explicit_defaults_to_2(self) -> None:
        assert RECALL_CONFIG.reserved_slots_explicit == 2

    def test_supersession_attenuation_defaults_to_05(self) -> None:
        assert RECALL_CONFIG.supersession_attenuation == pytest.approx(0.50)

    def test_recency_tau_days_defaults_to_30(self) -> None:
        assert RECALL_CONFIG.recency_tau_days == pytest.approx(30.0)

    def test_base_weights_spec_values(self) -> None:
        """§4.2 — the five weights must match the spec table exactly."""
        bw = RECALL_CONFIG.base_weights
        assert bw.query_match == pytest.approx(0.34)
        assert bw.recency == pytest.approx(0.20)
        assert bw.strength == pytest.approx(0.16)
        assert bw.domain_relevance == pytest.approx(0.14)
        assert bw.source_channel == pytest.approx(0.16)


# ---------------------------------------------------------------------------
# AC-C (1-AC2): Σ of the five base-dimension weights = 1.00 within 1e-9
# ---------------------------------------------------------------------------


class TestBaseWeightSum:
    """1-AC2 — base-dimension weights sum to 1.00 within 1e-9 (guard against drift)."""

    def test_sum_equals_one_within_1e9(self) -> None:
        total = (
            RECALL_CONFIG.base_weights.query_match
            + RECALL_CONFIG.base_weights.recency
            + RECALL_CONFIG.base_weights.strength
            + RECALL_CONFIG.base_weights.domain_relevance
            + RECALL_CONFIG.base_weights.source_channel
        )
        assert abs(total - 1.0) < 1e-9, (
            f"Σ base weights = {total!r}, expected 1.0 ± 1e-9 (1-AC2)"
        )

    def test_total_property_matches_manual_sum(self) -> None:
        """The .total property must agree with a manual sum."""
        bw = RECALL_CONFIG.base_weights
        manual = (
            bw.query_match
            + bw.recency
            + bw.strength
            + bw.domain_relevance
            + bw.source_channel
        )
        assert bw.total == manual


# ---------------------------------------------------------------------------
# Token estimator (§4.3 — deterministic, injectable)
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    """§4.3 — deterministic len(content)//4 estimator."""

    def test_empty_string_returns_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_none_returns_zero(self) -> None:
        assert estimate_tokens(None) == 0

    def test_four_chars_returns_one(self) -> None:
        assert estimate_tokens("abcd") == 1

    def test_eight_chars_returns_two(self) -> None:
        assert estimate_tokens("abcdefgh") == 2

    def test_4000_chars_returns_1000(self) -> None:
        assert estimate_tokens("x" * 4000) == 1000

    def test_known_token_count(self) -> None:
        """Sanity: a 400-char string yields exactly 100 estimated tokens."""
        assert estimate_tokens("y" * 400) == 100


# ---------------------------------------------------------------------------
# AC-D: auto_recall_engine uses the config root (no scattered literal '3')
# ---------------------------------------------------------------------------


class TestAutoRecallUsesConfigRoot:
    """AC-D — auto_recall_engine must derive its limit from RECALL_CONFIG,
    not from a hard-coded literal."""

    def test_auto_recall_engine_imports_recall_config(self) -> None:
        """The module must import recall_config (proof of the wiring)."""
        auto_recall = importlib.import_module("memchorus.auto_recall_engine")
        module_file = auto_recall.__file__
        assert module_file is not None, "auto_recall_engine module has no __file__"
        src = Path(module_file).read_text()
        assert "recall_config" in src, (
            "auto_recall_engine.py does not import memchorus.recall_config — "
            "the item-budget limit is likely still a hard-coded literal."
        )

    def test_no_bare_limit_three_literal(self) -> None:
        """The bare strings ``limit=3`` and ``[:3]`` must not appear in
        the runtime code path of auto_recall_engine (they must be replaced
        by the named constant from recall_config)."""
        auto_recall = importlib.import_module("memchorus.auto_recall_engine")
        module_file = auto_recall.__file__
        assert module_file is not None, "auto_recall_engine module has no __file__"
        auto_recall_src = Path(module_file).read_text()

        # The old patterns that should have been replaced by the named
        # constant from recall_config.  Both anchor on the *value* 3 via a
        # word boundary so e.g. ``limit=30`` is not falsely flagged.
        bad_patterns = [
            r"results\s*\[\s*:\s*3\s*\]",  # results[:3]
            r"limit\s*=\s*3\b",  # limit=3  (but not limit=30)
        ]
        found = []
        for pat in bad_patterns:
            for m in re.finditer(pat, auto_recall_src):
                ctx = auto_recall_src[max(0, m.start() - 40) : m.end() + 10]
                found.append((pat, ctx.strip()))

        assert found == [], (
            f"auto_recall_engine.py still contains bare '3' limit literals: {found}\n"
            f"Replace them with RECALL_CONFIG.k_implied (or a module-level "
            f"alias like _IMPLIED_ITEM_BUDGET = RECALL_CONFIG.k_implied)."
        )

    def test_implied_budget_matches_config(self) -> None:
        """If an _IMPLIED_ITEM_BUDGET alias exists, it must equal k_implied."""
        auto_recall = importlib.import_module("memchorus.auto_recall_engine")
        alias = getattr(auto_recall, "_IMPLIED_ITEM_BUDGET", None)
        if alias is not None:
            assert alias == RECALL_CONFIG.k_implied, (
                f"_IMPLIED_ITEM_BUDGET={alias} but RECALL_CONFIG.k_implied="
                f"{RECALL_CONFIG.k_implied} — mismatch"
            )


# ---------------------------------------------------------------------------
# Config root override API (spec §4.3 — "overridable")
# ---------------------------------------------------------------------------


class TestOverrideAPI:
    """Spec says configs are 'overridable' — verify a custom instance works."""

    def test_custom_k_implied(self) -> None:
        """A custom RecallLoopConfig instance can set k_implied to a test value."""
        custom = RecallLoopConfig(k_implied=5, k_explicit=20)
        assert custom.k_implied == 5
        assert custom.k_explicit == 20
        # The module-level singleton must be unchanged
        assert RECALL_CONFIG.k_implied == 3

    def test_custom_supersession_attenuation(self) -> None:
        custom = RecallLoopConfig(supersession_attenuation=0.25)
        assert custom.supersession_attenuation == pytest.approx(0.25)
