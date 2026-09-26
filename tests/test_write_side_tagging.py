#!/usr/bin/env python3
"""Tests for Issue #220 — write-side structured-tool-output tagging.

The auto-capture write path (``hooks.py``) currently serialises a structured
but non-dict tool output (a dataclass, a library object) with ``str()``, which
produces a single-quote Python-repr that the read side cannot cleanly classify
as JSON.  #220 makes the write path's emission *deterministic and
self-describing*:

  - ``hooks._emit_body(tool_output) -> (body_str, emission_kind)``
      * plain dict/list/tuple/set  -> canonical ``json.dumps`` body, ``"json"``
      * a plain ``str``           -> emitted verbatim,                    ``"str"``
      * a structured object with a real ``__dict__`` bag
                                   -> ``json.dumps(vars(...))``,           ``"json"``
      * anything else whose ``str()`` looks structural (starts with ``{``/``[``)
                                   -> the repr string,               ``"str-repr-like"``
      * anything else                    -> the ``str`` string,                ``"str"``

  - ``mempalace_memory_source.save`` persists the tagged record so
    ``retrieve(key)`` round-trips ``emission_kind``.

  - A pre-existing record with NO ``emission_kind`` reads back exactly as it
    did before (no migration crash, no changed behaviour for the old corpus).

The ACs are asserted here; the write-path helper and the persistence contract
do not exist at the base commit, so the tagged tests FAIL RED and the round-trip
tests are regression guards.
"""
import dataclasses
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.hooks import _emit_body as _emit_body  # noqa: E402  (RED until #220 lands)
from memchorus.mempalace_memory_source import MemPalaceMemorySource  # noqa: E402


def _src(tmp_path):
    return MemPalaceMemorySource(
        config={"skip_mcp": True, "cache_dir": str(tmp_path)}
    )


# =========================================================================== #
#  SECTION 1 — _emit_body: how a tool-output value is serialised + tagged     #
# =========================================================================== #
class TestEmitBody:
    def test_plain_dict_emits_json(self):
        body, kind = _emit_body({"key": "k", "content": "v"})
        assert kind == "json"
        assert json.loads(body) == {"key": "k", "content": "v"}

    def test_plain_list_emits_json(self):
        body, kind = _emit_body([1, 2, 3])
        assert kind == "json"
        assert json.loads(body) == [1, 2, 3]

    def test_dataclass_like_emits_json_not_repr(self):
        @dataclasses.dataclass
        class _Row:
            name: str
            count: int

        row = _Row(name="x", count=3)
        # Regression case: today this object falls into ``else: str(...)`` and
        # becomes "Row(name='x', count=3)"; it must instead be canonical JSON.
        body, kind = _emit_body(row)
        assert kind == "json"
        assert json.loads(body) == {"name": "x", "count": 3}

    def test_plain_string_emits_str_unchanged(self):
        body, kind = _emit_body("hello world")
        assert kind == "str"
        assert body == "hello world"

    def test_structural_str_repr_marked(self):
        class _ReprObj:  # no instance __dict__ bag
            def __str__(self):
                return "{'a': 1, 'b': 2}"

        body, kind = _emit_body(_ReprObj())
        # Either an explicitly tagged struct-like repr (Option 2) or a JSON-wrap
        # (Option 1) is acceptable — pin that it is NOT silently a "str" prose.
        assert kind in ("str-repr-like", "json")
        assert kind != "str"
        assert body

    def test_scalar_falls_back_to_str(self):
        body, kind = _emit_body(42)
        assert kind == "str"
        assert body == "42"

    def test_non_serialisable_dict_still_json(self):
        obj = object()
        body, kind = _emit_body({"o": obj})
        # default=str keeps it JSON-valid rather than raising.
        assert kind == "json"
        parsed = json.loads(body)
        assert "o" in parsed


# =========================================================================== #
#  SECTION 2 — emission_kind persistence + legacy compatibility               #
# =========================================================================== #
class TestEmissionKindPersistence:
    def test_emission_kind_round_trips_via_retrieve(self, tmp_path):
        src = _src(tmp_path)
        src.save("ek_key", {"text": "hello", "emission_kind": "json"})
        out = src.retrieve("ek_key")
        assert out is not None
        assert out.get("emission_kind") == "json"
        assert out.get("text") == "hello"

    def test_legacy_record_without_emission_kind_still_reads(self, tmp_path):
        # Pre-existing corpus entry: no emission_kind field at all.  Reading it
        # back must behave exactly as today (the tagged payload round-trips, no
        # crash, and the absence is simply absent).
        src = _src(tmp_path)
        src.save("legacy_key", {"text": "old body", "category": "LEARNING"})
        out = src.retrieve("legacy_key")
        assert out is not None
        assert out.get("text") == "old body"
        with pytest.raises(KeyError):  # no field was invented
            out["emission_kind"]

    def test_non_dict_value_round_trips_with_str_kind(self, tmp_path):
        src = _src(tmp_path)
        src.save("str_key", "just prose")
        out = src.retrieve("str_key")
        assert out == "just prose"
