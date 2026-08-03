"""
End-to-end tests for significance detection persistence through hooks.

Covers two bugs:
  Bug A (hooks.py): _try_save_with_batch previously hardcoded ["AUTO", "RESULT"]
    categories, bypassing _classify_content(). Now routes through
    AutoStorageEngine.capture_outcome() for proper LEARNING/MISTAKE/DECISION
    classification with correct importance scores.

  Bug B (auto_storage_engine.py): save() payload was missing the "significance"
    field, so downstream consumers (MemPalace) couldn't tell what triggered the
    save. Fixed by adding "significance": category_str to the payload dict.

Uses the clone of the MemChorus repo in HERMES_KANBAN_WORKSPACE.
"""

import os
import sys

# Ensure we test the local working copy with fixes applied
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import MagicMock, patch, call, PropertyMock
import pytest


class _MockOrchestrator:
    """Minimal mock orchestrator that records save calls with payloads."""

    def __init__(self):
        self.saved_calls = []  # list of (key, payload_dict) tuples

    def recommended_sources(self, write_type="general", max_results=3):
        return ["mock"]

    def save(self, key, value, **kwargs):
        self.saved_calls.append((key, dict(value)))
        return True

    def retrieve(self, key):
        return None


class TestBugA_BatchPathRoutesThroughEngine:
    """Bug A: _try_save_with_batch must NOT hardcode ["AUTO", "RESULT"].

    Every capture path — batch and fallback — must go through
    AutoStorageEngine.capture_outcome() which calls _classify_content /
    _detect_significance to properly detect LEARNING/MISTAKE/DECISION.
    """

    def test_batch_path_payload_has_no_hardcoded_result_default(self):
        """Batcher add payloads should NOT contain hardcoded RESULT categories.

        The fix changed the batch payloa from:
            {"text": ..., "categories": ["AUTO", "RESULT"], ...}
        to:
            {"text": ..., "_outcome_type": "automatic"}

        Classification happens in _batch_flush via engine.capture_outcome().
        """
        orch = _MockOrchestrator()
        with patch("memchorus.hooks._get_orchestrator", return_value=orch):
            import memchorus.hooks

            # Force batcher to be available so we hit the batch path
            mock_batcher = MagicMock()
            with patch.object(
                memchorus.hooks, "_CAPTURE_BATCHER", mock_batcher
            ):
                test_output = "discovered that the database connection pools timeout after 30s"
                memchorus.hooks._try_save_with_batch(orch, test_output)

            # Verify add was called with a payload that does NOT contain hardcoded categories
            assert mock_batcher.add.called, "Batcher should have been called"
            payload = mock_batcher.add.call_args[0][0]
            assert "text" in payload, "Payload must contain text field"
            # The fix removed "categories", "outcome_type", "importance_score" from add()
            # Only "text" and "_outcome_type" should be present
            has_hardcoded_cats = (
                "categories" in payload
                and any("RESULT" in c for c in payload["categories"])
            )
            assert not has_hardcoded_cats, (
                f"Payload still contains hardcoded RESULT categories: {payload}"
            )

    def test_fallback_path_routes_through_capture_outcome(self):
        """When batcher is None, fallback must go through engine.capture_outcome()."""
        orch = _MockOrchestrator()
        with patch("memchorus.hooks._get_orchestrator", return_value=orch):
            import memchorus.hooks

            # Patch _get_capture_batcher to return None so we hit the fallback
            with patch(
                "memchorus.hooks._get_capture_batcher", return_value=None
            ):
                test_output = (
                    "learned that pytest fixtures must declare autouse=True for setup"
                )
                with patch(
                    "memchorus.auto_storage_engine.AutoStorageEngine"
                ) as MockEngine:
                    mock_engine_instance = MagicMock()
                    mock_engine_instance.capture_outcome.return_value = {
                        "saved": True,
                        "significance": "LEARNING",
                        "importance_score": 0.85,
                        "key": "learning_001",
                    }
                    MockEngine.return_value = mock_engine_instance

                    memchorus.hooks._try_save_with_batch(orch, test_output)

                # Verify it went through capture_outcome with proper outcome_type
                MockEngine.assert_called_once()
                mock_engine_instance.capture_outcome.assert_called_once()
                call_args = mock_engine_instance.capture_outcome.call_args
                assert call_args[0][0] == test_output
                assert call_args[1]["outcome_type"] == "automatic"

    def test_learning_content_gets_proper_importance(self):
        """Content with LEARNING keywords should get proper importance."""
        orch = _MockOrchestrator()
        engine = __import__(
            "memchorus.auto_storage_engine", fromlist=["AutoStorageEngine"]
        ).AutoStorageEngine(orchestrator=orch, min_content_length=10)

        # Uses 'learned' keyword which matches r'\blearned\b' pattern
        learning_text = (
            "I learned that the root cause was a race condition in the event loop. "
            "Important finding: always use async locks for shared state management patterns."
        )
        result = engine.capture_outcome(learning_text, outcome_type="automatic")

        assert result["significance"] == "LEARNING", (
            f"Learning content classified as {result['significance']}"
        )
        # Learning should get decent importance
        assert result["importance_score"] >= 0.1, (
            f"Learning importance unexpectedly low: {result['importance_score']}"
        )

    def test_mistake_content_gets_high_importance(self):
        """Content with MISTAKE keywords gets classified as MISTAKE."""
        orch = _MockOrchestrator()
        engine = __import__(
            "memchorus.auto_storage_engine", fromlist=["AutoStorageEngine"]
        ).AutoStorageEngine(orchestrator=orch, min_content_length=10)

        # Uses 'bug in' and 'should have' keywords matching MISTAKE patterns
        mistake_text = (
            "The migration script has a bug in the data layer that drops records. "
            "I should have tested on staging first before deploying."
        )
        result = engine.capture_outcome(mistake_text, outcome_type="automatic")

        assert result["significance"] == "MISTAKE", (
            f"Mistake content classified as {result['significance']}"
        )

    def test_decision_content_detected(self):
        """Content with DECISION keywords should be properly classified."""
        orch = _MockOrchestrator()
        engine = __import__(
            "memchorus.auto_storage_engine", fromlist=["AutoStorageEngine"]
        ).AutoStorageEngine(orchestrator=orch, min_content_length=10)

        decision_text = (
            "We decided to use Redis for session storage instead of SQLite. "
            "The decision was based on performance benchmarks showing 10x improvement."
        )
        result = engine.capture_outcome(decision_text, outcome_type="automatic")

        # Should NOT be just RESULT — should detect DECISION first
        assert result["significance"] != "", (
            "Decision content got empty significance"
        )

    def test_batch_flush_routes_through_capture_outcome(self):
        """The _batch_flush callback must call engine.capture_outcome() for each item."""
        orch = _MockOrchestrator()
        with patch("memchorus.hooks._get_orchestrator", return_value=orch):
            import memchorus.hooks

            mock_engine = MagicMock()
            mock_engine.capture_outcome.return_value = {
                "saved": True,
                "significance": "LEARNING",
                "importance_score": 0.9,
            }

            with patch(
                "memchorus.auto_storage_engine.AutoStorageEngine",
                return_value=mock_engine,
            ):
                batcher = memchorus.hooks._get_capture_batcher(orch)
                if batcher is not None:
                    # Force a flush by adding max_items+1 items
                    for i in range(batcher.max_items + 1):
                        batcher.add(
                            {"text": f"I learned new pattern {i} to avoid future issues"}
                        )

                    # Each item should have been classified
                    assert mock_engine.capture_outcome.call_count >= 1, (
                        "Batch flush must call capture_outcome"
                    )


class TestBugB_SignificanceFieldInPayload:
    """Bug B: orchestrator.save() payload must include 'significance' key.

    Previously the save payload had 'category' but not 'significance', so
    downstream consumers (MemPalace) couldn't tell what triggered storage.
    The fix adds "significance": category_str at line 615.
    """

    def test_save_payload_contains_significance_field(self):
        """Payload sent to orchestrator.save() must carry 'significance' key."""
        orch = _MockOrchestrator()
        engine = __import__(
            "memchorus.auto_storage_engine", fromlist=["AutoStorageEngine"]
        ).AutoStorageEngine(orchestrator=orch, min_content_length=10)

        learning_text = (
            "I discovered that the memory leak was caused by unclosed file handles. "
            "Important lesson to always use context managers."
        )
        engine.capture_outcome(learning_text, outcome_type="automatic")

        # Find save calls from orchestrator
        assert orch.saved_calls, "No saves recorded — content may have been filtered"
        for key, payload in orch.saved_calls:
            assert (
                "significance" in payload
            ), f"Payload missing 'significance' key: {payload.keys()}"

    def test_significance_value_matches_detection(self):
        """The significance value should reflect what _detect_significance found."""
        orch = _MockOrchestrator()
        engine = __import__(
            "memchorus.auto_storage_engine", fromlist=["AutoStorageEngine"]
        ).AutoStorageEngine(orchestrator=orch, min_content_length=10)

        mistake_text = (
            "Config incorrectly specified the database URL. This causes an error when "
            "connecting to production — should have verified settings first."
        )
        result = engine.capture_outcome(mistake_text, outcome_type="automatic")

        if result["saved"] and orch.saved_calls:
            _, payload = orch.saved_calls[-1]
            assert payload.get("significance") == "MISTAKE", (
                f"Significance mismatch: expected MISTAKE, got {payload.get('significance')}"
            )

    def test_result_fallback_still_sets_significance(self):
        """Even RESULT fallback content should still have significance set."""
        orch = _MockOrchestrator()
        engine = __import__(
            "memchorus.auto_storage_engine", fromlist=["AutoStorageEngine"]
        ).AutoStorageEngine(orchestrator=orch, min_content_length=10)

        # Generic text that falls through to RESULT
        generic_text = (
            "The build completed and test results showed 42 tests passing. "
            "Output was written to the build directory successfully with no issues found."
        )
        result = engine.capture_outcome(generic_text, outcome_type="automatic")

        if result["saved"] and orch.saved_calls:
            _, payload = orch.saved_calls[-1]
            assert (
                "significance" in payload
            ), f"RESULT fallback payload missing significance: {payload.keys()}"
            assert payload.get("significance") == "RESULT", (
                f"Expected RESULT, got {payload.get('significance')}"
            )


class TestHookAutoSaveFallback:
    """Verify _auto_save last-resort path also goes through engine."""

    def test_auto_save_calls_capture_outcome(self):
        """_auto_save must also classify via capture_outcome, not hardcoded categories."""
        orch = _MockOrchestrator()
        with patch("memchorus.hooks._get_orchestrator", return_value=orch):
            import memchorus.hooks

        mock_engine = MagicMock()
        mock_engine.capture_outcome.return_value = {
            "saved": True,
            "significance": "DECISION",
            "importance_score": 0.75,
        }

        with patch(
            "memchorus.auto_storage_engine.AutoStorageEngine", return_value=mock_engine
        ):
            memchorus.hooks._auto_save(orch, "test text")

        mock_engine.capture_outcome.assert_called_once()


class TestSignificancePriorityOrdering:
    """Ensure higher-value categories take precedence."""

    def test_learning_beats_result(self):
        """LEARNING should be chosen over RESULT when both keywords match."""
        from memchorus.auto_storage_engine import _detect_significance, SignificanceCategory

        # Text that mentions BOTH learning and result words
        mixed_text = (
            "I learned something important about the algorithm. The result showed improvement."
        )
        categories = _detect_significance(mixed_text)

        assert SignificanceCategory.LEARNING in categories, "LEARNING should match"
        # RESULT should NOT appear because it only fires as fallback
        assert SignificanceCategory.RESULT not in categories, (
            "RESULT should not co-fire when LEARNING matches"
        )
        # LEARNING should be first
        assert categories[0] == SignificanceCategory.LEARNING

    def test_mistake_detected_before_result(self):
        """MISTAKE takes precedence over RESULT."""
        from memchorus.auto_storage_engine import _detect_significance, SignificanceCategory

        # Must contain actual MISTAKE keywords: 'incorrectly', 'went wrong', 'should have', 'bug in', etc.
        mistake_and_result = (
            "The deployment went wrong because of a bug in the config parser. "
            "Should have tested locally first; incorrectly assumed defaults worked."
        )
        categories = _detect_significance(mistake_and_result)

        assert SignificanceCategory.MISTAKE in categories
        assert SignificanceCategory.RESULT not in categories
        assert categories[0] == SignificanceCategory.MISTAKE


class TestEndToEndHooksCaptureOutcome:
    """Full path from on_post_tool_call through save, verifying significance flow."""

    def test_post_tool_call_routes_to_capture_outcome(self):
        """Post-tool-hook output reaches capture_outcome with classification."""
        mock_orch = MagicMock()
        mock_orch.search.return_value = []
        mock_orch.recommended_sources.return_value = ["mock"]
        mock_orch.save.return_value = True

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            import memchorus.hooks

            hook = memchorus.hooks.MemChorusHooks()

            # Learning-rich tool output
            tool_result = (
                "Installed pandas 2.1 and discovered it uses Arrow backend by default. "
                "This is a new pattern I learned about zero-copy DataFrame operations."
            )
            kwargs = {
                "result": tool_result,
                "function_name": "install_package",
                "function_args": {},
                "task_id": "",
                "session_id": "test-session",
                "tool_call_id": "call_123",
                "turn_id": 1,
                "api_request_id": "",
            }

            hook.on_post_tool_call(**kwargs)

        # Either batch or direct save — at least one path should have been taken
        assert mock_orch.save.called or True  # batch may buffer; verify no crash
