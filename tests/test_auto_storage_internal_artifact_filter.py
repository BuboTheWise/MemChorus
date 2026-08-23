"""
test_auto_storage_internal_artifact_filter -- GAP097 tests for self-referential
internal artifact rejection in auto_storage_engine.

Verifies that _is_internal_artifact() rejects raw tool output, session metadata dumps,
recursive introspection artifacts, and truncated capture blobs while still allowing
genuine content through the filter.
"""

import pytest
from memchorus.auto_storage_engine import _is_internal_artifact


class TestInternalArtifactRawJson:
    """Reject JSON with known internal API signature keys."""

    def test_rejects_session_search_json_with_mode_and_results(self):
        raw = '{"success": true, "mode": "discover", "query": "test", "results": []}'
        assert _is_internal_artifact(raw) is True

    def test_rejects_session_search_with_match_message_id(self):
        raw = '{"match_message_id": 12345, "messages_before": []}'
        assert _is_internal_artifact(raw) is True

    def test_rejected_tool_output_with_tool_key(self):
        raw = '{"tool_output": {"key": "value"}, "status": "ok"}'
        assert _is_internal_artifact(raw) is True

    def test_rejects_session_meta_block(self):
        raw = '{"session_meta": {"source": "cli", "model": "qwen"}}'
        assert _is_internal_artifact(raw) is True

    def test_rejects_tool_result_structure(self):
        raw = '{"result": [{"output": "..."}], "tool_call_id": "call_abc"}'
        assert _is_internal_artifact(raw) is True

    def test_rejects_memchorus_decision_payload(self):
        raw = '{"text": "{\"success\": true}", "content": "data"}'
        assert _is_internal_artifact(raw) is True


class TestInternalArtifactRecursiveIntrospection:
    """Reject content that appears to be a dump of MemChorus's own search results."""

    def test_rejects_session_search_dump(self):
        raw = 'session_search returned 3 sessions matching query'
        assert _is_internal_artifact(raw) is False  # this should pass through — it's natural language
        # Only raw dumps with structural markers get rejected

    def test_rejects_skills_list_tool_output_shape(self):
        raw = '{"total_count": 50, "paths": ["test.py"]}'
        assert _is_internal_artifact(raw) is True

    def test_rejects_knowledge_graph_dump(self):
        raw = '{"entities": {"Max": ["loves", "chess"]}, "triples": 12}'
        assert _is_internal_artifact(raw) is True


class TestInternalArtifactTruncated:
    """Reject partial captures truncated by budget limits."""

    def test_rejects_budget_exceeded_truncation(self):
        raw = 'some content here... (truncated, budget exceeded)'
        assert _is_internal_artifact(raw) is True

    def test_rejects_context_limit_truncation(self):
        raw = 'a long message that got cut off... (truncated, budget exceeded)\nmore stuff'
        assert _is_internal_artifact(raw) is True


class TestInternalArtifactNegativeCases:
    """Content that SHOULD NOT be rejected — legitimate findings contain some overlap."""

    def test_allows_terminal_output_with_real_findings(self):
        raw = (
            "Ran git log and found three commits from August related to "
            "the refactor. The latest one squashed changes from branch beta-2."
        )
        assert _is_internal_artifact(raw) is False

    def test_allows_json_in_natural_language_wrapper(self):
        raw = (
            "The analysis showed the configuration has these keys: "
            '{"mode": "strict", "level": 3}. I changed them accordingly.'
        )
        assert _is_internal_artifact(raw) is False
    def test_allows_tool_output_with_explanation(self):
        raw = (
            "I checked the results and confirmed: pytest returned 10 passed tests. "
            "The coverage gap was in auth modules, which I patched."
        )
        assert _is_internal_artifact(raw) is False

    def test_allows_learning_containing_keywords(self):
        raw = (
            "I learned that the success callback requires a mode parameter and "
            "that session_meta must include timestamps for proper ordering."
        )
        assert _is_internal_artifact(raw) is False

    def test_allows_code_review_findings(self):
        raw = (
            "Review found three issues: line 42 has an off-by-one, the tool call "
            "on line 78 needs error handling, and results were not deduplicated."
        )
        assert _is_internal_artifact(raw) is False

    def test_allows_normal_json_config(self):
        raw = '{"timeout": 30, "retries": 3, "model": "default"}'
        assert _is_internal_artifact(raw) is False
