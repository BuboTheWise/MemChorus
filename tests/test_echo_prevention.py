"""
Tests for query-echo artifact prevention in AutoStorageEngine and hooks.

Acceptance criteria covered:
  AC-1: auto_storage_engine._is_query_echo() rejects known query templates
  AC-2: capture_outcome() skips storage when text matches a query template
  AC-3: legitimate tool output still gets saved (no over-filtering)
  AC-4: hooks.on_post_tool_call also blocks query echoes

Regression test for t_8d008135 — ensures recall _QUERY_MAP strings do not
get stored as memory content via the post-recall storage cycle.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.auto_storage_engine import (
    AutoStorageEngine,
    _is_query_echo,
    _KNOWN_QUERY_TEMPLATES,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _MockOrchestrator:
    """Records every save() call for assertion."""

    def __init__(self):
        self.saved_calls = []  # [(key, value)]

    def recommended_sources(self, write_type="general", max_results=3):
        return ["mock"]

    def save(self, key, value, **kwargs):
        self.saved_calls.append((key, value))
        return True

    def retrieve(self, key):
        return None


def _make_engine(orch=None):
    if orch is None:
        orch = _MockOrchestrator()
    return AutoStorageEngine(orchestrator=orch)


# ---------------------------------------------------------------------------
# Test _is_query_echo helper directly
# ---------------------------------------------------------------------------


class TestIsQueryEcho(unittest.TestCase):
    """_is_query_echo detects known query templates accurately."""

    def test_exact_match_error_state(self) -> None:
        self.assertTrue(_is_query_echo("errors recovery patterns failure modes known issues"))

    def test_exact_match_planning_start(self) -> None:
        self.assertTrue(_is_query_echo("past planning patterns architecture decisions strategy notes"))

    def test_exact_match_tool_call_intent(self) -> None:
        self.assertTrue(
            _is_query_echo("tool usage history command conventions domain-specific guidance")
        )

    def test_exact_match_post_action_complete(self) -> None:
        self.assertTrue(_is_query_echo("post-action learnings outcomes results"))

    def test_all_templates_in_set(self) -> None:
        """Verify every _QUERY_MAP value is present in the guard set.

        This catches drift if a query template is updated in
        auto_recall_engine but forgotten here — the previous version of this
        test only compared counts and silently passed when contents diverged.

        The guard set may also contain legacy templates (superset), so we check
        containment rather than exact equality.
        """
        from memchorus.auto_recall_engine import _QUERY_MAP  # type: ignore[import-not-found]
        query_values = set(_QUERY_MAP.values())
        missing = query_values - _KNOWN_QUERY_TEMPLATES
        self.assertEqual(
            missing, set(),
            f"_KNOWN_QUERY_TEMPLATES is missing {len(missing)} _QUERY_MAP value(s). "
            "If a template was updated, update the guard set too.",
        )

    def test_near_exact_match_with_leading_whitespace(self) -> None:
        self.assertTrue(
            _is_query_echo("  errors recovery patterns failure modes known issues  ")
        )

    def test_case_variation_still_detected(self) -> None:
        self.assertTrue(
            _is_query_echo("Errors Recovery Patterns Failure Modes Known Issues")
        )

    def test_legitimate_text_not_flagged(self) -> None:
        """Real content containing some of the same words is NOT an echo."""
        self.assertFalse(
            _is_query_echo(
                "I learned that error recovery patterns are critical for failure modes"
                " and fixing known issues in the system architecture."
            )
        )

    def test_short_unrelated_text_not_flagged(self) -> None:
        self.assertFalse(_is_query_echo("The benchmark achieved 99.2% accuracy"))

    def test_tool_command_output_not_flagged(self) -> None:
        self.assertFalse(
            _is_query_echo("pip install requests==2.31.0 returned exit code 0")
        )


# ---------------------------------------------------------------------------
# Test capture_outcome skips query echoes
# ---------------------------------------------------------------------------


class TestCaptureOutcomeEchoPrevention(unittest.TestCase):
    """capture_outcome() rejects query templates — they never reach orchestrator.save()."""

    def test_error_query_echo_not_saved(self) -> None:
        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orchestrator=orch)

        result = engine.capture_outcome("errors recovery patterns failure modes known issues")

        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "query_echo_artifact")
        self.assertEqual(len(orch.saved_calls), 0)

    def test_planning_query_echo_not_saved(self) -> None:
        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orchestrator=orch)

        result = engine.capture_outcome("past planning patterns architecture decisions strategy notes")

        self.assertFalse(result["saved"])
        self.assertEqual(result["reason"], "query_echo_artifact")
        self.assertEqual(len(orch.saved_calls), 0)

    def test_all_query_templates_blocked(self) -> None:
        """All query templates from _QUERY_MAP are rejected (currently 5)."""
        from memchorus.auto_recall_engine import _QUERY_MAP  # type: ignore[import-not-found]

        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orchestrator=orch)

        for _dp, query_template in _QUERY_MAP.items():
            result = engine.capture_outcome(query_template)
            self.assertFalse(
                result["saved"],
                f"Query template '{query_template}' should have been blocked",
            )
            self.assertEqual(result["reason"], "query_echo_artifact")

        self.assertEqual(len(orch.saved_calls), 0)

    def test_legitimate_content_still_saved(self) -> None:
        """Real tool output is NOT over-filtered."""
        orch = _MockOrchestrator()
        engine = AutoStorageEngine(orchestrator=orch)

        legit_texts = [
            "I learned that the endpoint returns JSON now",
            "The benchmark result was 99.2% accuracy",
            "We decided to use PostgreSQL instead of SQLite",
            "Something went wrong with the deployment pipeline",
            "pip install completed successfully with exit code 0",
        ]

        for text in legit_texts:
            orch.saved_calls.clear()
            result = engine.capture_outcome(text)
            self.assertTrue(result["saved"], f"Legitimate content was blocked: '{text}'")
            # Dual-write for LEARNING/MISTAKE/DECISION can produce 2 save calls;
            # what matters is that at least one save occurred.
            self.assertGreater(len(orch.saved_calls), 0)


# ---------------------------------------------------------------------------
# Test hooks.on_post_tool_call also blocks echoes
# ---------------------------------------------------------------------------


class TestHooksBlockEchoes(unittest.TestCase):
    """hooks.py on_post_tool_call rejects query template strings."""

    @unittest.skipIf(
        not os.environ.get("MEMCHORUS_TEST_HOOKS"),
        "Set MEMCHORUS_TEST_HOOKS=1 to run hooks tests (requires bootstrap).",
    )
    def test_post_tool_call_blocks_echo(self) -> None:
        """When the global orchestrator is bootstrapped, query echoes are blocked."""
        from memchorus.hooks import MemChorusHooks

        hooks = MemChorusHooks()
        result = hooks.on_post_tool_call(
            tool_output="errors recovery patterns failure modes known issues"
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()
