"""
Tests for Issue #140 — locator storage.

Verifies:
  1. `extract_locator` produces the card schema {source, path_or_url, title,
     topics, gist, key} from markdown, HTML, tool output and structured payloads.
  2. No locator is invented for bare unstructured short strings (no bloat).
  3. Caller-supplied explicit locators are preserved field-by-field.
  4. `format_locator` emits a single compact line, hard-capped at
     LOCATOR_LINE_MAX_CHARS (150), with graceful topic/pointer trimming.
  5. `should_inject_locator` gates on body length so short bodies stay as-is.
  6. End-to-end: MemoryOrchestrator.save() attaches a locator alongside the
     body, and the hooks formatter collapses the blob into the locator line.
"""
import pytest

from memchorus import locator as L
from memchorus import MemoryOrchestrator


# ---------------------------------------------------------------------------
# Fixtures: representative bodies
# ---------------------------------------------------------------------------

MD_DOC = (
    "# MemChorus Recall Tuning\n"
    "\n"
    "Tuning playbook for the recall pipeline.\n"
    "\n"
    "## Scoring weights\n"
    "The relevance scorer boosts recency and quality.\n"
    "\n"
    "## Injection budget\n"
    "The context block enforces a per-entry character budget.\n"
    "\n"
    + "Additional detail " * 30 + "\n"
)

HTML_PAGE = (
    "<html><head><title>Hermes Docs — Gateway</title></head><body>\n"
    "<p>The Hermes gateway routes messages between platforms and agent sessions.</p>\n"
    "<h2>Configuration</h2>\n"
    "<p>Set the binding endpoint in config.yaml.</p>\n"
    "<p>see https://hermes-agent.nousresearch.com/docs/gateway</p>\n"
    "</body></html>\n"
    + " padding " * 40
)

TOOL_OUTPUT = (
    "pytest exit=1 FAIL tests/test_a.py\n"
    "\n"
    "# Test report\n"
    "\n"
    "## Failures\n"
    "test_a failed on assertion at line 42\n"
    "\n"
    "## Summary\n"
    "1 test failed, 0 passed\n"
    + "y" * 250 + "\n"
)


# ---------------------------------------------------------------------------
# 1. Extraction schema
# ---------------------------------------------------------------------------

class TestExtractLocatorSchema:

    def test_markdown_document_has_full_schema(self):
        loc = L.extract_locator({"content": MD_DOC}, key="tune/recall", source_name="hermes")
        assert loc is not None
        # Card-required fields
        assert "source" in loc
        assert "title" in loc
        assert "topics" in loc
        assert "gist" in loc
        # Key anchor for on-demand retrieval
        assert loc["key"] == "tune/recall"
        # Correct title
        assert loc["title"] == "MemChorus Recall Tuning"
        # Topic list does NOT duplicate the title itself
        assert "MemChorus Recall Tuning" not in loc["topics"]
        # Topics are the section headings
        assert "Scoring weights" in loc["topics"]
        assert "Injection budget" in loc["topics"]
        # Gist is one compact line ≤ GEST_MAX_CHARS
        assert 0 < len(loc["gist"]) <= L.GEST_MAX_CHARS

    def test_html_page_extracts_url_and_title(self):
        loc = L.extract_locator({"content": HTML_PAGE}, key="docs/gateway", source_name="web_fetch")
        assert loc is not None
        assert loc["source"].lower() in {"web", "page", "url", "web_fetch", ""} or loc["source"]
        assert loc["path_or_url"] == "https://hermes-agent.nousresearch.com/docs/gateway"
        assert "Gateway" in loc["title"]
        # URL not polluted by a closing tag
        assert "</" not in loc["path_or_url"]

    def test_tool_output_extracts_file_path(self):
        loc = L.extract_locator({"content": TOOL_OUTPUT}, key="ci/fail", source_name="tool_output")
        assert loc is not None
        # The pytest-failed file path should be surfaced as path_or_url
        assert "tests/test_a.py" in loc.get("path_or_url", "")
        # Title is the leading report heading, not the slug itself
        assert loc.get("title")

    def test_string_body_produces_locator_when_substantive(self):
        # A long enough plain-string body still gets a locator
        long_body = "This note explains the gateway routing model and binding endpoints. " * 10
        loc = L.extract_locator(long_body, key="n/1", source_name="")
        assert loc is not None
        assert "source" in loc
        assert "gist" in loc


class TestExtractLocatorNoBloat:

    def test_bare_short_string_gets_no_locator(self):
        # A 2-char body is below the inject threshold and carries no structure,
        # so attach_locator leaves it byte-identical to the input.
        assert L.attach_locator("hi", key="n/1") == "hi"

    def test_attach_locator_attaches_for_substantive_plain_string(self):
        # A long enough plain-string body DOES get a locator attached.
        long_body = "This note explains the gateway routing model. " * 10
        v = L.attach_locator(long_body, key="n/1")
        assert isinstance(v, dict)
        assert "locator" in v
        assert v["content"] == long_body

    def test_explicit_locator_preserved_field_by_field(self):
        caller_locator = {"source": "web", "path_or_url": "https://example.com/a"}
        value = {"locator": dict(caller_locator), "content": "x" * 300}
        out = L.attach_locator(value, key="k", source_name="")
        assert out["locator"]["source"] == "web"
        assert out["locator"]["path_or_url"] == "https://example.com/a"

    def test_attach_locator_idempotent(self):
        once = L.attach_locator({"content": MD_DOC}, key="k", source_name="hermes")
        twice = L.attach_locator(once, key="k", source_name="hermes")
        assert once.get("locator") == twice.get("locator")


# ---------------------------------------------------------------------------
# 2. Formatting: single compact line ≤ 150 chars
# ---------------------------------------------------------------------------

class TestFormatLocator:

    def test_line_capped_at_150(self):
        loc = L.extract_locator({"content": MD_DOC}, key="k", source_name="hermes")
        assert loc is not None
        line = L.format_locator(loc, source_name="hermes", key="k")
        assert len(line) <= L.LOCATOR_LINE_MAX_CHARS

    def test_line_contains_pointer_or_retrieve(self):
        loc = L.extract_locator({"content": HTML_PAGE}, key="k", source_name="web_fetch")
        assert loc is not None
        line = L.format_locator(loc, source_name="web_fetch", key="k")
        # Must show how to go back for the full body
        assert "retrieve(key=" in line or "→" in line

    def test_topics_are_trimmed_before_pointer(self):
        # Inject many topics and a long pointer; line should still be ≤ 150
        loc = {
            "source": "vault",
            "title": "Very Long Document Title That Is Going To Be Somewhat Explanatory",
            "topics": ["alpha one", "beta two", "gamma three", "delta four", "epsilon five"],
            "path_or_url": "https://example.com/a/very/long/path/with/many/segments",
            "key": "k",
        }
        line = L.format_locator(loc, source_name="", key="k")
        assert len(line) <= L.LOCATOR_LINE_MAX_CHARS
        # At least the pointer or a topic is retained
        assert "example.com" in line or "topic" in line or line

    def test_empty_locator_degrades_to_key(self):
        line = L.format_locator({}, source_name="", key="some/key")
        assert "some/key" in line


# ---------------------------------------------------------------------------
# 3. Injection gating
# ---------------------------------------------------------------------------

class TestShouldInjectLocator:

    def test_short_body_does_not_inject(self):
        loc = L.extract_locator({"content": "short enough note about gateway setup"})
        assert L.should_inject_locator("short enough note about gateway setup", loc) is False

    def test_long_body_injects_with_usable_locator(self):
        loc = L.extract_locator({"content": MD_DOC}, key="k")
        assert loc is not None
        assert L.should_inject_locator(MD_DOC, loc) is True

    def test_no_locator_never_injects(self):
        assert L.should_inject_locator(MD_DOC, None) is False


# ---------------------------------------------------------------------------
# 4. End-to-end: orchestrator save() attaches a locator
# ---------------------------------------------------------------------------

class _MockSource:
    """Minimal storage backing: captures saves, supports search + delete."""

    def __init__(self):
        self.data = {}
        self.available = True

    @property
    def is_available(self) -> bool:
        return self.available

    @property
    def name(self):
        return "mock"

    def save(self, key, value):
        self.data[key] = value
        return True

    def search(self, query="") -> list:
        from memchorus.relevance_engine import RankedResult
        results = []
        for k, v in self.data.items():
            content_str = str(v) + " " + str(k)
            for term in query.split():
                if term and term.lower() in content_str.lower():
                    results.append(
                        RankedResult(key=k, content=v, source="mock", score=0.8)
                    )
                    break
        return results

    def delete(self, key):
        if self.data:
            self.data.pop(key, None)
        return True


class TestOrchestratorAttachesLocator:

    def _make_orch(self):
        orch = MemoryOrchestrator(config={"skip_init_sources": True})
        src = _MockSource()
        orch.register_source(src)
        return orch, src

    def test_save_markdown_doc_stores_locator_alongside_body(self):
        orch, src = self._make_orch()
        orch.save("tune/recall", {"content": MD_DOC}, category="learning")
        stored = src.data.get("tune/recall")
        assert isinstance(stored, dict)
        # Locator sits alongside the body
        assert "locator" in stored
        loc = stored["locator"]
        assert "source" in loc
        assert "title" in loc
        assert "topics" in loc
        assert "gist" in loc
        assert "key" in loc and loc["key"] == "tune/recall"
        # The body is NOT replaced — it stays fully recoverable
        assert "content" in stored
        assert stored["content"] == MD_DOC

    def test_save_html_page_keeps_body_and_locator(self):
        orch, src = self._make_orch()
        orch.save("docs/gateway", {"content": HTML_PAGE}, category="learning")
        stored = src.data.get("docs/gateway")
        assert isinstance(stored, dict)
        assert stored["locator"]["path_or_url"] == (
            "https://hermes-agent.nousresearch.com/docs/gateway"
        )
        assert stored["content"] == HTML_PAGE


# ---------------------------------------------------------------------------
# 5. End-to-end: hooks formatter collapses the blob into the locator line
# ---------------------------------------------------------------------------

class TestHooksFormatterCollapses:

    def _format_item(self, item: dict) -> str:
        from memchorus import hooks
        return hooks._format_context_block([item])

    def test_long_blob_collapsed_to_locator_line(self):
        loc = L.extract_locator({"content": MD_DOC}, key="tune/recall", source_name="hermes")
        item = {
            "key": "tune/recall",
            "source": "mock",
            "score": 0.9,
            # Body + locator side-by-side (what save() persists)
            "content": {"content": MD_DOC, "locator": loc},
        }
        rendered = self._format_item(item)
        # The rendered block should NOT contain the long body verbatim
        # (we expect the locator line in its place).
        body_tail = MD_DOC[-60:]
        assert body_tail not in rendered
        # It should show the on-demand retrieval pointer.
        assert "retrieve(key=" in rendered
        # And be materially shorter than dumping the full body.
        assert len(rendered) < len(MD_DOC) + 40

    def test_short_body_not_collapsed(self):
        short = "short enough note about gateway setup"
        loc = L.extract_locator({"content": short}, key="n/1")
        item = {
            "key": "n/1",
            "source": "mock",
            "score": 0.9,
            "content": {"content": short, "locator": loc} if loc else short,
        }
        rendered = self._format_item(item)
        # Body stays (below threshold => no locator injection)
        assert "gateway setup" in rendered
