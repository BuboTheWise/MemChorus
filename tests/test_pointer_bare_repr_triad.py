# -*- coding: utf-8 -*-
"""Pointer / bare-repr integrity triad (#219 + #220 + #221).

These tests pin the three linked bugs that share one mechanism: the low-signal
gate hands the agent a ``read it: retrieve(key=…)`` pointer, but (1) the gate
misses the bare single-quote Python-repr dump shape, (2) the write path serialises
structured-but-not-plain-dict objects via ``str()`` (bare repr) rather than JSON,
and (3) ``retrieve(key)`` returns a hard indistinguishable ``None`` when the
pointer is cold, so the agent cannot tell "cold/pruned" from "never existed".

At current HEAD (before the fixes) the positive cases FAIL RED and the guards
hold; after the fixes the positive cases GREEN and the guards still hold.  Each
test is self-contained (no MCP server required).
"""

import json
import tempfile

import pytest


# =========================================================================== #
#  #219 — read-side gate: bare single-quote Python-repr dump shape            #
# =========================================================================== #

def _gate():
    from memchorus.hooks import _should_collapse_low_signal
    return _should_collapse_low_signal


class TestGateBarePythonRepr:
    """#219: the gate must collapse the bare single-quote Python-repr nested
    dict shape — the case ``json.loads`` cannot parse and the gate today lets
    slide (verified in the issue: ``collapse=False`` on HEAD)."""

    def test_bare_python_repr_nested_collapsed(self):
        # 2+ effective top-level fields, single-quote (Python-repr, not JSON).
        body = str({"key": "x", "content": {"text": "line1\nline2"}})
        assert body.startswith("{'key': 'x'")  # prove it is the REPR shape
        with pytest.raises(ValueError):
            json.loads(body)  # ... and JSON can't parse it
        assert _gate()(body) is True, (
            "bare Python-repr nested dict must collapse to a pointer, not inline"
        )

    def test_bare_python_repr_single_payload_key_collapsed(self):
        body = str({"output": "dump text"})
        assert body.startswith("{'output': 'dump text'")
        assert _gate()(body) is True, (
            "single recognised-payload-key repr dump must collapse"
        )


class TestGateBareReprGuarded:
    """#219 guards: meaningful single records, plain words, and prose must NOT
    collapse (deliberate exclusion + AC6 over-suppression pin)."""

    def test_meaningful_single_record_still_inlines(self):
        # A *single* arbitrary key whose value is a meaningful record inlines —
        # the gate's deliberate exclusion (guards the ``len(parsed) >= 2`` rule).
        body = str({"payload": [1, 2, 3]})
        assert body.startswith("{'payload': [1, 2, 3]}")
        assert _gate()(body) is False, (
            "a meaningful single-record repr must inline, not collapse"
        )

    def test_plain_prose_still_inlines(self):
        prose = (
            "trust execution: drop tasks into triage and expect "
            "autonomous completion without babysitting"
        )
        assert _gate()(prose) is False, (
            "real multi-word prose must NOT collapse (AC6 over-suppression)"
        )

    def test_non_json_and_non_literal_repr_degrades_false(self):
        # A body that is neither JSON nor a Python literal (contains a call)
        # must degrade to False — inlined, never swallowed (never-raises).
        body = "{'x': sum([1, 2, 3])}"
        assert body.startswith("{'x': sum(")
        assert _gate()(body) is False

    def test_none_and_non_str_never_raise(self):
        assert _gate()(None) is False
        assert _gate()(123) is False


# =========================================================================== #
#  #220 — write-side: structured object must serialise to JSON, not repr      #
# =========================================================================== #

def _emit():
    from memchorus.hooks import _emit_body
    return _emit_body


class TestEmitBodyStructured:
    """#220: the write path must produce valid JSON (or a tagged body) for
    structured inputs — never a bare single-quote repr that downstream recall
    cannot recognise."""

    def test_plain_dict_is_json(self):
        body, kind = _emit()({"a": 1, "b": [1, 2]})
        assert kind == "json"
        json.loads(body)  # parseable JSON, not a repr

    def test_dataclass_like_is_json_not_repr(self):
        # The regression case today: a structured object that is NOT a plain
        # dict falls into ``str()`` and becomes a single-quote repr.
        from types import SimpleNamespace

        obj = SimpleNamespace(name="x", items=["a", "b"])
        body, kind = _emit()(obj)
        assert kind == "json"
        parsed = json.loads(body)  # must be parseable JSON
        assert parsed["name"] == "x"

    def test_plain_string_unchanged_and_tagged(self):
        body, kind = _emit()("a perfectly ordinary prose line")
        assert kind == "str"
        assert body == "a perfectly ordinary prose line"


class TestEmitBodyStrReprLike:
    """#220 guard: a non-dict, non-__dict__ object whose ``str()`` LOOKS like a
    structured dump (starts with ``{`` and has a ``': '`` pattern) must be tagged
    ``str-repr-like`` so the reader knows it is a structural dump — never left as
    a bare ``str`` the reader must guess about."""

    def test_object_str_repr_like_tagged(self):
        class ReprLike:
            def __str__(self) -> str:
                return "{'k': 'v', 'other': [1]}"

        body, kind = _emit()(ReprLike())
        assert body == "{'k': 'v', 'other': [1]}"
        assert kind == "str-repr-like"

    def test_plain_ordinary_object_is_str(self):
        # A non-structural object (e.g. an int) stays ``str``.
        body, kind = _emit()(42)
        assert kind == "str"
        assert body == "42"


# =========================================================================== #
#  #221 — read-side contract: retrieve() must distinguish a cold/missing key  #
# =========================================================================== #


def _make_source(tmp):
    from memchorus.mempalace_memory_source import MemPalaceMemorySource
    return MemPalaceMemorySource(config={"cache_dir": tmp})


class TestRetrievePointerContract:
    """#221: a ``retrieve(key)`` for a cold/pruned key must return a
    distinguishable not-found result — the caller must be able to tell
    "cold/miss" from "present but empty". A valid pointer is also re-fetchable
    via ``fallback="live"``."""

    def test_present_nonempty_returns_body(self, tmp_path):
        src = _make_source(str(tmp_path))
        value = {"text": "the actual body content"}
        src.save("K_PRESENT", value)
        out = src.retrieve("K_PRESENT")
        assert out is not None
        assert isinstance(out, dict) and out.get("text") == "the actual body content"

    def test_missing_distinguishable_from_empty(self, tmp_path):
        src = _make_source(str(tmp_path))
        # Present but empty: the file exists; its value is an empty string.
        src.save("K_EMPTY", "")
        empty_out = src.retrieve("K_EMPTY")
        # Missing: the file does not exist at all.
        miss_out = src.retrieve("K_MISSING")
        # The miss must be distinguishable from the empty-but-valid result.
        assert miss_out != empty_out, (
            "a cold/missing retrieve must be distinguishable from "
            "'present but empty'; got miss=%r empty=%r" % (miss_out, empty_out)
        )
        assert miss_out is not None, (
            "a cold/missing retrieve must not return a bare indistinguishable None"
        )

    def test_fallback_live_refetches_on_miss(self, tmp_path):
        src = _make_source(str(tmp_path))
        # A key that is NOT in the local cache but IS answerable by a live
        # (mock) search — the fallback must attempt the re-fetch and use it.
        calls = []

        def _fake_search(query, limit=10, *, wing=None, room=None):
            calls.append(query)
            # Simulate a live hit that can land the body by the query.
            return [
                {
                    "key": "LIVE/1",
                    "content": "body-from-live",
                    "source": "mcp_live",
                }
            ]

        src.search = _fake_search
        out = src.retrieve("K_COLD", fallback="live")
        assert calls, "fallback='live' must attempt a live re-fetch on a miss"
        # A valid pointer that CAN be re-landed must resolve to a body, not a
        # bare cold sentinel.
        assert out is not None
        assert "body-from-live" in _as_string(out)

    def test_fallback_live_exhausted_distinguishable(self, tmp_path):
        src = _make_source(str(tmp_path))

        def _empty_live(query, limit=10, *, wing=None, room=None):
            return []  # live surface has nothing either

        src.search = _empty_live
        out = src.retrieve("K_COLD", fallback="live")
        # Even with the live source exhausted, the result must be distinguishable
        # (not a bare indistinguishable None) — the caller can say "tried live,
        # still cold" rather than silently getting None.
        assert out is not None

    def test_default_warm_key_byte_identical(self, tmp_path):
        # Regression guard: a warm key returns its body exactly — the default
        # path (no fallback) is unchanged.
        src = _make_source(str(tmp_path))
        value = {"text": "stable body"}
        src.save("K_STABLE", value)
        out = src.retrieve("K_STABLE")
        assert out == {"text": "stable body"}


def _as_string(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(value)
