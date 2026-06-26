"""Tests for RelevanceScorer.rank_sources() helper.

Covers:
- Default priors rank hermes_default before mempalace
- Custom priors can flip the order
- Domain hints influence ranking (memory -> hermes_default, graph -> mempalace)
- Empty source list returns empty
- Unknown source names fall back to neutral prior 0.5
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.relevance_engine import RelevanceScorer, ContextWeight


class TestRankSourcesBasic(unittest.TestCase):
    """Default prior behavior."""

    def setUp(self):
        self.scorer = RelevanceScorer()

    def test_default_order_hermes_first(self):
        """With default priors (hermes_default=0.7, mempalace=0.3), hermes_default should rank first."""
        sources = ["mempalace", "hermes_default"]
        ranked = self.scorer.rank_sources(sources)
        self.assertEqual(ranked[0], "hermes_default")
        self.assertEqual(ranked[1], "mempalace")

    def test_empty_list_returns_empty(self):
        """rank_sources([]) should return []."""
        self.assertEqual(self.scorer.rank_sources([]), [])


class TestRankSourcesCustomPriors(unittest.TestCase):
    """Custom prior values can change ranking."""

    def test_priors_flip_order(self):
        """Give mempalace a higher prior and it should come first."""
        scorer = RelevanceScorer(priors={"mempalace": 0.9, "hermes_default": 0.5})
        ranked = scorer.rank_sources(["hermes_default", "mempalace"])
        self.assertEqual(ranked[0], "mempalace")
        self.assertEqual(ranked[1], "hermes_default")

    def test_custom_single_source(self):
        """Single source with custom prior returns in a one-item list."""
        scorer = RelevanceScorer(priors={"custom": 1.0})
        ranked = scorer.rank_sources(["custom"])
        self.assertEqual(ranked, ["custom"])


class TestRankSourcesDomainHints(unittest.TestCase):
    """Domain hints from ContextWeight influence ranking."""

    def test_memory_domain_boosts_hermes_default(self):
        """In 'memory' domain, hermes_default should get a boost over mempalace."""
        scorer = RelevanceScorer()
        ctx = ContextWeight()  # default has memory -> hermes_default=1.5, mempalace=0.5
        ranked = scorer.rank_sources(
            ["mempalace", "hermes_default"],
            context=ctx,
        )
        self.assertEqual(ranked[0], "hermes_default")

    def test_graph_domain_boosts_mempalace(self):
        """When source_type_weight is high and domain_weights favor mempalace,
        mempalace flips ahead of hermes_default even though the default prior (0.7)
        favors hermes_default."""
        from dataclasses import replace

        scorer = RelevanceScorer()
        ctx = ContextWeight()
        # Make a graph-only context with a high source_type_weight so the domain
        # signal actually competes against the 0.7 prior gap
        asymmetric_ctx = replace(
            ctx,
            domain_weights={"graph": {"mempalace": 2.0, "hermes_default": 0.5}},
            source_type_weight=0.85,
        )
        ranked = scorer.rank_sources(
            ["hermes_default", "mempalace"],
            context=asymmetric_ctx,
        )
        self.assertEqual(ranked[0], "mempalace")

    def test_unknown_domain_no_boost(self):
        """A domain not in domain_weights should add no boost."""
        scorer = RelevanceScorer()
        ctx = ContextWeight(domain_weights={"nonexistent": {"hermes_default": 1.5}})
        ranked = scorer.rank_sources(
            ["hermes_default", "mempalace"],
            context=ctx,
        )
        # hermes_default still wins because its prior (0.7 > 0.3) and domain boost
        self.assertEqual(ranked[0], "hermes_default")


class TestRankSourcesUnknownNames(unittest.TestCase):
    """Source names not in priors fall back to neutral 0.5."""

    def test_unknown_source_neutral_prior(self):
        """An unknown source gets prior 0.5, which is between hermes_default (0.7) and mempalace (0.3)."""
        scorer = RelevanceScorer()
        ranked = scorer.rank_sources(
            ["mempalace", "unknown_source", "hermes_default"],
        )
        self.assertEqual(ranked[0], "hermes_default")       # 0.7 (highest)
        self.assertEqual(ranked[1], "unknown_source")       # 0.5 (neutral)
        self.assertEqual(ranked[2], "mempalace")            # 0.3 (lowest)


class TestRankSourcesIntegration(unittest.TestCase):
    """Ensure retrieve() tries sources in ranked order correctly."""

    def test_retrieve_uses_ranked_sources(self):
        """retrieve() should try higher-ranked source first and use it if available."""
        import tempfile, os, shutil
        from memchorus.orchestrator import MemoryOrchestrator
        from memchorus.hermes_memory_source import HermesDefaultMemorySource

        tmpdir = tempfile.mkdtemp(prefix="memchorus_rank_")
        hermes_dir = os.path.join(tmpdir, "hd")

        try:
            os.makedirs(hermes_dir)

            orch_config = {
                "default_source": "hermes_default",
                "hermes_default_config": {"memory_dir": hermes_dir},
                "mempalace_config": {},
            }
            orch = MemoryOrchestrator(config=orch_config)

            # Verify default order: hermes_default (prior 0.7) > mempalace (prior 0.3)
            scored = list(orch._scorer.rank_sources(list(orch.memory_sources.keys())))
            self.assertEqual(scored[0], "hermes_default", f"Default should favor hermes_default; got {scored}")

            # Save a key only to mempalace, then verify retrieve() still finds it (fallback)
            mp_src = orch.memory_sources["mempalace"]
            # Use TempMemPalaceSource to write directly to our temp dir
            self.patch_mempalace_dir(mp_src, os.path.join(tmpdir, "mp"))

            key = "test_key"
            value = {"data": "in mempalace"}
            mp_src.save(key, value)

            # Now also save to hermes_default (default-ranked first) - it lacks the key
            orch.memory_sources["hermes_default"].save(key + "_different", value)

            # retrieve() tries hermes_default first, finds nothing, falls through to mempalace
            result = orch.retrieve(key)
            self.assertIsNotNone(result)
            self.assertEqual(result["data"], "in mempalace")

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def patch_mempalace_dir(self, source, cache_dir):
        """Ensure mempalace source can write to *cache_dir*."""
        os.makedirs(cache_dir, exist_ok=True)
        class _Mocked(type(source)):
            def _get_cache_dir(slf): return cache_dir
        # Replace the type (safe for this test-only class)
        source.__class__ = _Mocked


if __name__ == "__main__":
    unittest.main(verbosity=2)
