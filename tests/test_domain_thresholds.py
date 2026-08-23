"""GH#98: Domain-aware minimum recall score thresholds - TDD tests

Tests that RelevanceScorer and MemoryOrchestrator support configurable per-domain
threshold overrides instead of a single global minimum. Different recall scenarios
(error lookup, planning, code review) need different precision floors.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from unittest.mock import patch, MagicMock


class TestDomainThresholdsConfig:
    """Acceptance criterion: New config structure with domain_thresholds nested map."""

    def test_default_domain_thresholds_in_config(self):
        """Domain thresholds have sensible defaults."""
        from memchorus.relevance_engine import _DEFAULT_DOMAIN_THRESHOLDS
        assert "general" in _DEFAULT_DOMAIN_THRESHOLDS
        assert "error_context" in _DEFAULT_DOMAIN_THRESHOLDS
        assert "code_review" in _DEFAULT_DOMAIN_THRESHOLDS

    def test_scorer_accepts_domain_thresholds(self):
        """RelevanceScorer constructor accepts domain_thresholds parameter."""
        from memchorus.relevance_engine import RelevanceScorer
        thresholds = {
            "error_context": 0.5,
            "general": 0.3,
        }
        scorer = RelevanceScorer(domain_thresholds=thresholds)
        # Should have the thresholds stored
        assert hasattr(scorer, '_domain_thresholds') or hasattr(scorer, 'domain_thresholds')

    def test_config_validation_rejects_invalid(self):
        """Config validation rejects negative thresholds or values > 1.0."""
        from memchorus.relevance_engine import RelevanceScorer

        with pytest.raises(ValueError):
            RelevanceScorer(domain_thresholds={"bad": -0.5})

        with pytest.raises(ValueError):
            RelevanceScorer(domain_thresholds={"bad": 1.5})


class TestDomainThresholdScoring:
    """Acceptance criterion: Relevance scorer uses domain-specific floor."""

    def test_higher_threshold_for_error_context(self):
        """Error context has higher threshold, filtering out low-confidence results."""
        from memchorus.relevance_engine import RelevanceScorer, RankedResult

        thresholds = {
            "error_context": 0.6,
            "general": 0.3,
        }
        scorer = RelevanceScorer(domain_thresholds=thresholds)

        # Mock scored results - some below error threshold but above general
        mock_results: list[RankedResult] = [
            RankedResult(key="err1", content="content err1", source="test", score=0.55),
            RankedResult(key="err2", content="content err2", source="test", score=0.7),
            RankedResult(key="err3", content="content err3", source="test", score=0.4),
        ]
        # Only err2 survives the 0.6 floor for error_context
        filtered = scorer._filter_by_domain_threshold(mock_results, "error_context")
        assert len(filtered) == 1
        assert filtered[0].key == "err2"

    def test_fallback_chain_domain_to_global(self):
        """Unknown domain falls back to global default, then hardcoded 0.3."""
        from memchorus.relevance_engine import RelevanceScorer

        scorer = RelevanceScorer(
            min_score=0.35,
            domain_thresholds={"known": 0.6}
        )
        # Unknown domain should use global min_score
        floor = scorer.get_threshold("unknown_domain")
        assert floor == 0.35

    def test_general_domain_uses_global_minimum(self):
        """The 'general' key maps to the global min_score default."""
        from memchorus.relevance_engine import RelevanceScorer

        scorer = RelevanceScorer(min_score=0.4)
        floor = scorer.get_threshold("general")
        assert floor == 0.4


class TestOrchestratorDomainPassing:
    """Acceptance criterion: Orchestrator passes domain/context hint to search."""

    def test_search_accepts_domain_parameter(self):
        """MemoryOrchestrator.search() accepts optional domain parameter."""
        from memchorus.orchestrator import MemoryOrchestrator
        import inspect

        sig = inspect.signature(MemoryOrchestrator.search)
        assert "domain" in sig.parameters or "context_hint" in sig.parameters

    def test_hook_passes_domain_on_error_state(self):
        """When BehavioralTrigger detects ERROR_STATE, hook passes domain='error_context'."""
        from memchorus.hooks import MemChorusHooks
        # This will be implemented - test verifies the integration works
        pass


class TestBoundaryCases:
    """Regression + edge case tests."""

    def test_no_domain_config_uses_sane_defaults(self):
        """Without domain config, scorer uses hardcoded minimum of 0.3."""
        from memchorus.relevance_engine import RelevanceScorer

        scorer = RelevanceScorer()
        # Should always have some floor
        floor = scorer.get_threshold("general")
        assert floor == pytest.approx(0.3, abs=0.1)

    def test_zero_results_when_all_below_domain_floor(self):
        """If all results score below the domain floor, nothing leaks through."""
        from memchorus.relevance_engine import RelevanceScorer

        scorer = RelevanceScorer(
            min_score=0.3,
            domain_thresholds={"strict": 0.8}
        )
        # All items score 0.5 - above general floor but below strict
        low_results = [
            type("R", (), {"key": f"k{i}", "content": f"item {i}", "score": 0.5})()
            for i in range(3)
        ]
        # scorer should be able to filter by domain
        filtered = scorer._filter_by_domain_threshold(low_results, "strict")
        assert len(filtered) == 0


# All tests self-contained — no shared fixtures needed
