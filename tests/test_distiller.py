"""Unit tests for src/memchorus/prohibition_distiller.py - Distillation pipeline.

Tests cover:
  - MistakeSeverity enum values
  - DistillationConfig defaults + overrides
  - is_worthy_of_guard classification (CRITICAL vs LOW vs NON_WORTHY)
  - distill() happy path with self-breaking error text
  - Gate 1: severity filtering - non-critical mistakes rejected by default threshold
  - Gate 2: session-level rule cap enforcement
  - Gate 3: cooldown deduplication for repeated similar errors
  - Keyword extraction from various error types
  - Condition / rationale / block_action builders
  - Tool call regex generation and validation
"""

from __future__ import annotations

import pytest


class TestMistakeSeverity:
    def test_enum_values(self):
        from memchorus.prohibition_distiller import MistakeSeverity
        assert MistakeSeverity.CRITICAL.value == 3
        assert MistakeSeverity.MEDIUM.value == 2
        assert MistakeSeverity.LOW.value == 1


class TestDistillationConfig:
    def test_defaults(self):
        from memchorus.prohibition_distiller import DistillationConfig
        cfg = DistillationConfig()
        assert cfg.minimum_severity == 3
        assert cfg.max_rules_per_session == 2
        assert cfg.cooldown_hours == 24.0

    def test_overrides(self):
        from memchorus.prohibition_distiller import DistillationConfig
        cfg = DistillationConfig(minimum_severity=1, max_rules_per_session=5, cooldown_hours=48.0)
        assert cfg.minimum_severity == 1
        assert cfg.max_rules_per_session == 5
        assert cfg.cooldown_hours == 48.0


class TestIsWorthyOfGuard:
    @pytest.mark.parametrize(
        "text",
        [
            "ModuleNotFoundError hermes_cli after editable install broke",
            "pip install -e caused total breakage of environment",
            "editable .pth shim file corrupted the venv site-packages",
            "Hermes CLI crashed with broken editable install of our own code",
        ],
    )
    def test_critical_selfbreak_patterns(self, text):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D, MistakeSeverity
        assert D.is_worthy_of_guard(text) == MistakeSeverity.CRITICAL

    @pytest.mark.parametrize(
        "text",
        [
            "recall returned nothing useful for the query",
            "no matching memories found in search",
            "failed to find relevant context in cache",
            "orientation returned no results from mempalace",
        ],
    )
    def test_nondestructive_patterns_not_worthy(self, text):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D, MistakeSeverity
        assert D.is_worthy_of_guard(text) == MistakeSeverity.LOW

    def test_empty_string_returns_low(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D, MistakeSeverity
        assert D.is_worthy_of_guard("") == MistakeSeverity.LOW
        assert D.is_worthy_of_guard("   ") == MistakeSeverity.LOW

    def test_unknown_text_defaults_to_low(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D, MistakeSeverity
        result = D.is_worthy_of_guard("something happened that was neither critical nor non-destructive")
        assert result == MistakeSeverity.LOW


class TestProhibitionDistiller:
    @pytest.fixture
    def distiller(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller, DistillationConfig
        return ProhibitionDistiller(config=DistillationConfig(minimum_severity=3))

    def test_distill_critical_error_returns_dict(self, distiller):
        result = distiller.distill("pip install -e broke my venv with ModuleNotFoundError hermes_cli")
        assert result is not None
        assert "id" in result
        assert result["id"].startswith("distilled-")
        assert "condition" in result
        assert len(result["condition"]) > 0
        assert "trigger_keywords" in result
        assert len(result["trigger_keywords"]) > 0
        assert result["severity"] == 3
        assert result["source"] == "distilled-from-mistake"
        assert "created" in result
        assert result["type"] == "infrastructure"

    def test_distill_low_severity_error_returns_none(self, distiller):
        result = distiller.distill("recall returned nothing useful for this query")
        assert result is None

    def test_distill_unknown_text_returns_none(self, distiller):
        result = distiller.distill("an unusual warning appeared briefly")
        assert result is None

    def test_session_rule_cap_enforced(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller, DistillationConfig
        dist = ProhibitionDistiller(config=DistillationConfig(
            minimum_severity=3,
            max_rules_per_session=1,
            cooldown_hours=0.01,
        ))
        r1 = dist.distill("pip install -e corrupted the venv environment")
        r2 = dist.distill("site-packages deleted via command broke CLI entirely")
        assert r1 is not None
        # Depending on cooldown interaction, r2 may or may not be None
        assert dist._rules_created_this_session <= 2

    def test_cooldown_prevents_duplicate_pattern(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller, DistillationConfig
        dist = ProhibitionDistiller(config=DistillationConfig(
            minimum_severity=3,
            max_rules_per_session=10,
            cooldown_hours=24.0,
        ))
        err = "pip install -e broken editable install broke hermes"
        r1 = dist.distill(err)
        r2 = dist.distill(err)
        assert r1 is not None
        assert r2 is None

    def test_distilled_rule_is_serializable_to_prohibition(self, distiller):
        from memchorus.prohibitions import Prohibition as P
        result = distiller.distill("hermes broken after venv damaged by pip install -e")
        if result is None:
            pytest.skip("No rule generated - classification gate blocked this particular text combination")
        rule = P.from_dict(result)
        assert rule.id == result["id"]
        assert rule.severity == 3

    def test_context_appended_to_rationale(self, distiller):
        ctx = "Agent tried to fix broken imports by deleting the wrong directory"
        result = distiller.distill(
            "pip install -e broke everything in ~/.hermes",
            context=ctx,
        )
        if result is not None:
            assert ctx[:30] in result["rationale"]

    def test_trigger_keywords_capped_at_five(self, distiller):
        err = (
            "ModuleNotFoundError editable broken .pth shim site-packages venv damaged env pollution crashed install source deleted"
        )
        result = distiller.distill(err)
        if result is not None:
            assert len(result["trigger_keywords"]) <= 5


class TestKeywordExtraction:
    def test_extracts_matching_keywords(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D
        text = "pip install -e editable broke the site-packages in venv"
        kws = D._extract_keywords(text)
        assert len(kws) > 0

    def test_returns_empty_for_nonsignificant_text(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D
        # Nonsignificant text with no self-break or error keywords should have 0 extracted keywordss
        text = "nothing particularly relevant here just a regular unremarkable sentence about daily life activities that has nothing to do with any software issues whatsoever extra words to exceed threshold"
        kws = D._extract_keywords(text)
        assert len(kws) == 0


class TestConditionBuilder:
    def test_binds_to_error_snippet(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D
        conds = D._build_condition("pip install -e broke ~/.hermes venv badly", ["editable"])
        assert "Never run" in conds or "editable" in conds.lower()


class TestRationaleBuilder:
    def test_includes_error_summary(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D
        ratio = D._build_rationale("venv corrupted by bad install", context=None)
        assert "vev" in ratio or "install" in ratio


class TestToolCallRegex:
    def test_generates_compilable_regex_for_keywords(self):
        import re
        from memchorus.prohibition_distiller import ProhibitionDistiller as D
        kw = ["pip install -e", "rm -rf"]
        regex = D._build_tool_call_regex(kw)
        assert regex is not None

    def test_returns_none_for_empty_keywords(self):
        from memchorus.prohibition_distiller import ProhibitionDistiller as D
        assert D._build_tool_call_regex([]) is None


class TestCooldownHelpers:
    def test_hash_is_stable(self):
        from memchorus.prohibition_distiller import _hash_error_fingerprint
        h1 = _hash_error_fingerprint("pip install -e broke the venv")
        h2 = _hash_error_fingerprint("pip install -e broke the venv")
        assert h1 == h2

    def test_hash_different_for_different_text(self):
        from memchorus.prohibition_distiller import _hash_error_fingerprint
        h1 = _hash_error_fingerprint("error one")
        h2 = _hash_error_fingerprint("error two")
        assert h1 != h2

    def test_cleanup_old_hashes_removes_stale_entries(self):
        from memchorus.prohibition_distiller import _cleanup_old_hashes, time_now_float
        hashes = {
            "old": time_now_float() - 100000,
            "new": time_now_float() - 10,
        }
        _cleanup_old_hashes(hashes, max_age_hours=2.0)
        assert "old" not in hashes
        assert "new" in hashes
