#!/usr/bin/env python3
"""Tests for robustness fixes across the MemPalace memory source and hooks formatter.

Covers three GitHub issues:

  #136 — ``_McpClient.add_drawer()`` must interpret the MemPalace MCP server's
         structured ``{"success": True/False}`` response rather than relying on
         keyword matching alone.  A ``success: False`` with no error keyword
         (e.g. ``{"success": False, "drawer_id": "d1"}``) must be treated as a
         failure; a ``success: True`` with an incidental word must succeed.

  #139 — ``MemPalaceMemorySource`` must enforce a ``MIN_RECALL_SCORE`` floor
         (default 0.5, overridable via ``config['min_recall_score']``) so weak /
         off-topic MCP hits are dropped, mirroring the sibling sources.

  #143 — ``hooks._format_context_block()`` must unwrap structured content
         payloads (``{"text": ...}``, nested ``{"content": ...}``) into clean
         strings instead of leaking ``{'key': ...}`` dict reprs.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.mempalace_memory_source import MemPalaceMemorySource, _McpClient
from memchorus.hooks import _format_context_block, _unwrap_content_field


# =========================================================================== #
#  SECTION 1 — #136 add_drawer structured success/error detection            #
# =========================================================================== #

class TestAddDrawerStructuredDetection:
    """add_drawer() must honour the server's structured success flag."""

    def _client_with_response(self, response):
        client = _McpClient(timeout=1)
        client.call_tool = lambda name, args: response  # type: ignore[assignment]
        return client

    def test_structured_success_true(self):
        c = self._client_with_response({"success": True, "drawer_id": "d1"})
        assert c.add_drawer("wing", "room", "content") is True

    def test_structured_success_false_despite_no_error_text(self):
        # The old keyword matcher would return True here because "d1" has no
        # error keyword.  The structured flag must win.
        c = self._client_with_response({"success": False, "drawer_id": "d1"})
        assert c.add_drawer("wing", "room", "content") is False

    def test_structured_failure_with_error_message(self):
        c = self._client_with_response(
            {"success": False, "error": "title must be non-empty"}
        )
        assert c.add_drawer("wing", "room", "content") is False

    def test_none_response_is_failure(self):
        c = self._client_with_response(None)
        assert c.add_drawer("wing", "room", "content") is False

    def test_keyword_fallback_error_still_detected(self):
        # No "success" field at all — fall through to keyword scan.
        c = self._client_with_response({"result": "wing 'foo' not found"})
        assert c.add_drawer("wing", "room", "content") is False

    def test_keyword_fallback_success(self):
        c = self._client_with_response({"result": "drawer d1234 created"})
        assert c.add_drawer("wing", "room", "content") is True

    def test_bare_string_response_with_error(self):
        c = self._client_with_response("Operation failed: invalid room")
        assert c.add_drawer("wing", "room", "content") is False

    def test_bare_string_response_success(self):
        c = self._client_with_response("OK saved 1 drawer")
        assert c.add_drawer("wing", "room", "content") is True


# =========================================================================== #
#  SECTION 2 — #139 recall-score floor on MemPalaceMemorySource              #
# =========================================================================== #

def _source_with_fake_client(mcp_results, config=None):
    """Build a source whose client is a stub returning canned MCP results."""
    src = MemPalaceMemorySource(config=config or {"skip_mcp": True})
    # Force the connected+alive path without spawning a subprocess.  is_alive is
    # a read-only property returning _connected (no persistent session), so set
    # _connected directly rather than reassigning the property.
    src._ensure_connected = lambda: True  # type: ignore[assignment]
    fake_client = _McpClient(timeout=1)
    fake_client._connected = True  # type: ignore[assignment]
    fake_client._persistent_session = None  # type: ignore[assignment]
    fake_client.search = lambda **kw: list(mcp_results)  # type: ignore[assignment]
    src._client = fake_client
    return src


class TestRecallScoreFloor:
    """MEM results below the floor are dropped; at/above are kept."""

    def test_default_floor_is_half(self):
        assert MemPalaceMemorySource.MIN_RECALL_SCORE == 0.5

    def test_resolve_min_recall_score_default(self, tmp_path):
        src = MemPalaceMemorySource(config={"cache_dir": str(tmp_path)})
        assert src._resolve_min_recall_score() == 0.5

    def test_resolve_min_recall_score_override(self, tmp_path):
        src = MemPalaceMemorySource(
            config={"cache_dir": str(tmp_path), "min_recall_score": 0.9}
        )
        assert src._resolve_min_recall_score() == 0.9

    def test_weak_result_dropped_by_default_floor(self, tmp_path):
        src = _source_with_fake_client(
            [
                {"wing": "w", "room": "r", "text": "strong", "similarity": 0.95},
                {"wing": "w", "room": "r", "text": "weak", "similarity": 0.30},
            ],
            config={"cache_dir": str(tmp_path)},
        )
        results = src.search("query")
        scores = [r.get("score") for r in results]
        assert 0.95 in scores
        assert 0.30 not in scores

    def test_result_at_floor_boundary_kept(self, tmp_path):
        # A result exactly AT the floor is kept (>= semantics, ">= 0.5 keep").
        src = _source_with_fake_client(
            [{"wing": "w", "room": "r", "text": "edge", "similarity": 0.5}],
            config={"cache_dir": str(tmp_path)},
        )
        results = src.search("query")
        assert len(results) == 1
        assert results[0]["content"].startswith("edge")

    def test_stricter_override_drops_mid_score(self, tmp_path):
        src = _source_with_fake_client(
            [
                {"wing": "w", "room": "r", "text": "high", "similarity": 0.95},
                {"wing": "w", "room": "r", "text": "mid", "similarity": 0.70},
            ],
            config={"cache_dir": str(tmp_path), "min_recall_score": 0.9},
        )
        results = src.search("query")
        scores = [r.get("score") for r in results]
        assert 0.95 in scores
        assert 0.70 not in scores

    def test_nonnumeric_similarity_treated_as_zero(self, tmp_path):
        # "similarity": "NaN-ish garbage" -> coerced to 0.0 -> below floor.
        src = _source_with_fake_client(
            [{"wing": "w", "room": "r", "text": "bad", "similarity": "not-a-number"}],
            config={"cache_dir": str(tmp_path)},
        )
        results = src.search("query")
        # No similarity key path with bad value -> score 0.0 -> dropped.
        assert not any(r.get("score") and r["score"] >= 0.5 for r in results)

    def test_no_similarity_kept(self, tmp_path):
        # Entries without a reported similarity are kept (floor only applies to
        # scored hits).
        src = _source_with_fake_client(
            [{"wing": "w", "room": "r", "text": "unscored"}],
            config={"cache_dir": str(tmp_path)},
        )
        results = src.search("query")
        assert len(results) == 1


# =========================================================================== #
#  SECTION 3 — #143 hooks content unwrapping                                 #
# =========================================================================== #

class TestUnwrapContentField:
    """Direct behaviour of the _unwrap_content_field helper."""

    def test_plain_string_passthrough(self):
        assert _unwrap_content_field("hello") == "hello"

    def test_dict_text_field(self):
        assert _unwrap_content_field({"text": "the answer", "extra": 1}) == "the answer"

    def test_dict_nested_content_text(self):
        v = {"key": "k1", "content": {"text": "nested"}}
        assert _unwrap_content_field(v) == "nested"

    def test_dict_without_text_or_content_json(self):
        v = {"a": 1, "b": [2, 3]}
        out = _unwrap_content_field(v)
        # Should be compact JSON, not Python repr.
        import json as _json
        assert out == _json.dumps(v, ensure_ascii=False)
        assert "'" not in out or out.startswith("{")

    def test_list_json(self):
        out = _unwrap_content_field([1, "two", {"three": 3}])
        assert "one" not in out and "two" in out
        # No Python single-quote repr
        assert out.startswith("[")

    def test_unserializable_falls_back_to_str(self):
        class Weird:
            def __repr__(self):
                return "<weird>"
            def __str__(self):
                return "<weird>"
        assert _unwrap_content_field(Weird()) == "<weird>"

    def test_empty_string_stays_empty(self):
        assert _unwrap_content_field("") == ""


class TestFormatContextBlockUnwrap:
    """#143 end-to-end: _format_context_block must not leak dict reprs."""

    def test_item_with_dict_content_shows_text_not_repr(self):
        items = [{"key": "k1", "content": {"text": "remember this", "meta": {}}}]
        out = _format_context_block(items)
        assert "remember this" in out
        # The raw dict repr must not appear.
        assert "{'text': " not in out
        assert "{'key':" not in out

    def test_item_with_plain_string_content(self):
        items = [{"key": "k1", "content": "plain string body"}]
        out = _format_context_block(items)
        assert "plain string body" in out

    def test_item_with_top_level_text_field(self):
        # content key present but empty; text fallback should be used.
        items = [{"key": "k1", "content": "", "text": "text field body"}]
        out = _format_context_block(items)
        assert "text field body" in out

    def test_item_dict_content_json_readable(self):
        items = [{"key": "k1", "content": {"payload": [1, 2, 3]}}]
        out = _format_context_block(items)
        # JSON serialised, not Python repr with single quotes.
        assert '{"payload": [1, 2, 3]}' in out

    def test_empty_items_returns_empty(self):
        assert _format_context_block([]) == ""


# =========================================================================== #
#  SECTION 4 — IMPL #166 source_file provenance forwarding in save()          #
# =========================================================================== #


def _save_client_capture():
    """Return (source, capture) where ``capture`` records MCP call args."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="mcp166_")
    src = MemPalaceMemorySource(config={"cache_dir": tmp})
    captured: list = []  # list of (tool_name, args_dict)
    fake = _McpClient(timeout=1)

    def _stub_call_tool(name, args):
        captured.append((name, dict(args)))
        return {"success": True, "drawer_id": "d1"}

    fake.call_tool = _stub_call_tool            # type: ignore[assignment]
    fake._connected = True                      # type: ignore[assignment]
    fake._persistent_session = None             # type: ignore[assignment]
    src._client = fake
    src._ensure_connected = lambda: True        # type: ignore[assignment]
    return src, captured


class TestSaveForwardsSourceFile:
    """save() must forward a non-empty source_file to add_drawer (MCP)."""

    def test_payload_source_file_is_forwarded(self, tmp_path):
        src, captured = _save_client_capture()
        payload = {
            "text": "the memory body",
            "category": "LEARNING",
            "source_file": "/tmp/memchorus-test/notes/session-042.md",
        }
        ok = src.save("key166", payload)
        # One add_drawer MCP call.
        add_calls = [a for (n, a) in captured if n == "mempalace_add_drawer"]
        assert len(add_calls) == 1, "expected exactly one mempalace_add_drawer call"
        assert add_calls[0].get("source_file") == "/tmp/memchorus-test/notes/session-042.md"

    def test_missing_source_file_falls_back_to_key(self, tmp_path):
        src, captured = _save_client_capture()
        # No source_file in payload — provenance must NOT be empty: fall back to key.
        payload = {"text": "the memory body", "category": "RESULT"}
        src.save("key166", payload)
        add_calls = [a for (n, a) in captured if n == "mempalace_add_drawer"]
        assert len(add_calls) == 1
        assert add_calls[0].get("source_file") == "key166"

    def test_blank_source_file_falls_back_to_key(self, tmp_path):
        src, captured = _save_client_capture()
        payload = {"text": "body", "category": "RESULT", "source_file": "   "}
        src.save("key166", payload)
        add_calls = [a for (n, a) in captured if n == "mempalace_add_drawer"]
        assert add_calls[0].get("source_file") == "key166"

    def test_non_dict_value_uses_key(self, tmp_path):
        src, captured = _save_client_capture()
        # A plain string value has no payload dict to pull source_file from,
        # so the key is the provenance locator.
        src.save("key166", "just a string memory")
        add_calls = [a for (n, a) in captured if n == "mempalace_add_drawer"]
        assert add_calls[0].get("source_file") == "key166"


# =========================================================================== #
#  SECTION 5 — IMPL #166 doctor --provenance-report                           #
# =========================================================================== #

from memchorus.install_doctor import (
    _cache_dir_paths,
    _provenance_report,
    _provenance_exit_code,
    _render_provenance_human,
    _render_provenance_json,
    _scan_cache_provenance,
)


class TestProvenanceScan:
    """Cache-scanner behaviour of the provenance report."""

    def test_scan_counts_present_and_missing(self, tmp_path):
        # One file WITH source_file, two WITHOUT.
        import json as _json
        (tmp_path / "a.json").write_text(_json.dumps({"text": "x", "source_file": "a"}))
        (tmp_path / "b.json").write_text(_json.dumps({"text": "y"}))
        (tmp_path / "c.json").write_text(_json.dumps({"text": "z", "source_file": ""}))
        rep = _scan_cache_provenance(tmp_path)
        assert rep["status"] == "ok"
        assert rep["total"] == 3
        assert rep["with_source_file"] == 1
        assert rep["missing_source_file"] == 2
        assert rep["coverage_pct"] == round(1 / 3 * 100, 1)

    def test_scan_not_found(self, tmp_path):
        rep = _scan_cache_provenance(tmp_path / "nope")
        assert rep["status"] == "not_found"
        assert rep["total"] == 0

    def test_exit_codes(self, tmp_path):
        import json as _json
        (tmp_path / "good.json").write_text(_json.dumps({"source_file": "s"}))
        assert _provenance_exit_code(_scan_cache_provenance(tmp_path)) == 0
        (tmp_path / "bad.json").write_text(_json.dumps({"source_file": ""}))
        assert _provenance_exit_code(_scan_cache_provenance(tmp_path)) == 1
        assert _provenance_exit_code(_scan_cache_provenance(tmp_path / "none")) == 2

    def test_report_finds_hermes_cache(self, monkeypatch, tmp_path):
        # Point _cache_dir_paths at a dir that has one good + one bad entry.
        import json as _json
        (tmp_path / "g.json").write_text(_json.dumps({"source_file": "ok"}))
        (tmp_path / "b.json").write_text(_json.dumps({"text": "no sf"}))
        monkeypatch.setattr(
            "memchorus.install_doctor._cache_dir_paths", lambda: [tmp_path]
        )
        rep = _provenance_report()
        assert rep["status"] == "ok"
        assert rep["with_source_file"] == 1
        assert rep["missing_source_file"] == 1


class TestProvenanceHumanRender:
    """Human --provenance-report output must propose a backfill policy when missing > 0."""

    def test_backfill_policy_proposal_present_when_missing(self, capsys):
        report = {
            "cache_dir": "/tmp/memchorus-test/cache",
            "status": "ok",
            "total": 10,
            "with_source_file": 8,
            "missing_source_file": 2,
            "coverage_pct": 80.0,
            "sample_missing": ["old-1", "old-2"],
        }
        _render_provenance_human(report)
        out = capsys.readouterr().out
        # The backfill policy proposal must offer both options.
        assert "Proposed backfill policy" in out
        assert "one-time migration" in out
        assert "orphan" in out
        # Explicitly a proposal, never auto-applied (doctor stays diagnostic).
        assert "does NOT apply any backfill" in out

    def test_no_policy_block_when_fully_covered(self, capsys):
        report = {
            "cache_dir": "/tmp/memchorus-test/cache",
            "status": "ok",
            "total": 5,
            "with_source_file": 5,
            "missing_source_file": 0,
            "coverage_pct": 100.0,
            "sample_missing": [],
        }
        _render_provenance_human(report)
        out = capsys.readouterr().out
        assert "Proposed backfill policy" not in out
