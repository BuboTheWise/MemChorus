"""Test placeholder artifact detection rejects synthetic filler while preserving real content."""

import pytest
from memchorus.auto_storage_engine import _is_placeholder_artifact, AutoStorageEngine

class TestPlaceholderArtifactDetection:
    """MC-004: Placeholder artifacts should be caught by pattern regexes and heuristics."""

    @pytest.mark.parametrize("text", [
        "session context t_52da572a current task",
        "session context t_f637eab9 current task",
        "Session Context T_17cfe174 Current Task",
        "tool output for t_aadba0dc",
        "execution context t_bf08f040",
        "tool result t_4aa408d3",
    ])
    def test_placeholder_patterns_rejected(self, text):
        """Synthetic placeholder patterns should be detected and rejected."""
        assert _is_placeholder_artifact(text) is True, f"Placeholder was not rejected: {text!r}"

    @pytest.mark.parametrize("text", [
        "The fix involved modifying hooks.py to catch ExceptionGroup errors in three places.",
        "I decided to use pgvector instead of ChromaDB because it aligns better with our existing PostgreSQL infrastructure.",
        "Post-action analysis: found 3 meaningful targets for follow-up research on autonomous agent workflows.",
    ])
    def test_real_content_preserved(self, text):
        """Genuine content should pass through without false positives."""
        assert _is_placeholder_artifact(text) is False, f"Real content was incorrectly blocked: {text!r}"

    def test_heuristic_short_unstructured_no_keywords_rejected(self):
        """Short unstructured text with no significance keywords should be caught by heuristic."""
        filler = "this is just some random text output from a tool call today"
        assert _is_placeholder_artifact(filler) is True

class TestAutoStorageIntegration:
    """Placeholder guard integrates correctly into capture_outcome pipeline."""

    def setup_method(self):
        """Create a mock orchestrator for each test."""
        self.orchestrator = MockOrchestrator()
        self.engine = AutoStorageEngine(orchestrator=self.orchestrator)

    def test_placeholder_rejected_by_engine(self):
        """Placeholder text should not pass through capture_outcome pipeline."""
        result = self.engine.capture_outcome(
            "session context t_12345678 current task", outcome_type="automatic"
        )
        assert result["saved"] is False
        assert result["reason"] == "placeholder_artifact"

    def test_real_learning_saved(self):
        """Real learning content should be saved through the pipeline."""
        real = "I learned that BehavioralTrigger needs letter-only lookaround regex boundaries to avoid false positives on JSON keys."
        result = self.engine.capture_outcome(real, outcome_type="automatic")
        assert result["saved"] is True
        assert "LEARNING" in result.get("significance", "") or len(result.get("significance", "")) > 0

    def test_query_echo_still_blocked(self):
        """Existing query echo prevention should not be broken by new changes."""
        result = self.engine.capture_outcome(
            "past planning patterns architecture decisions strategy notes",
            outcome_type="automatic"
        )
        assert result["saved"] is False


class MockOrchestrator:
    """Minimal orchestrator mock that supports save() with provenance keys."""

    def __init__(self):
        self.saves = []

    def recommended_sources(self, write_type="general"):
        return ["hermes_default"]

    def save(self, key, payload, source_name=None):
        self.saves.append((key, payload, source_name))
        return True
