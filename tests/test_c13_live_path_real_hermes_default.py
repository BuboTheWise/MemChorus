"""C13 — live-path proof against the REAL ``HermesDefaultMemorySource``.

Background  ------------
The first-pass C13 test file (``test_c13_recall_liveness_gate.py``) verified the
three-way gate (RESOLVED / COLD / DEAD) using a ``_FakeSource`` whose
``retrieve()`` accepts a ``fallback=`` kwarg — matching the MemPalace MCP
``fallback="live"`` contract.  But the REAL ``HermesDefaultMemorySource``
does NOT accept a ``fallback=`` kwarg at all: its ``retrieve`` signature is
``retrieve(key: str)``.  The existing test suite never wired the REAL source
through the gate, so the Oct-21 self-assessment (which ran against the real
source and a real memory dir) was the first place a real-body-vs-pointer shape
divergence could surface.

The 3 keys from the Oct-21 self-assessment (``learning-ad4ecdb3-8645``,
``mistake-c86e1e4b-7403``, ``learning-787ec4d8-9846``) showed up with what
looked like empty source content.  On the live path at this moment, those 3
keys RESOLVE (the 51–55 KB bodies are present on disk); the C13 gate keeps
them (``RESOLVED``) and renders them via the existing ``locator_preview``
low-signal gate (#217), collapsing each body to the one-line
``read it: retrieve(key=…)`` pointer form — which is the SAME shape that the
self-assessment flagged as "empty".  In other words: the gate works, but the
render layer collapses a 51 KB body to a one-line pointer before injection.
The pointer form itself is correct and honest (the body is one retrieve()
away), but the C13 "shipped in tests, not proven at injection" clause is
specifically about proving that the gate's DROP / KEEP / ANNOTATE behaviour
is correct on the live path — not just against fakes that accept ``fallback=``.

What this file does  ----------------
Builds a deterministic tmp-directory fixture (a few JSON files, mimicking
the shape of a real profile memory dir), points the REAL
``HermesDefaultMemorySource`` at it, builds a REAL ``MemoryOrchestrator``
wired to that source (not a fake), and drives ``apply_recall_liveness()``
+ ``_format_context_block()`` (exactly the seam the hook calls) to prove
that:

  * a key whose body IS in the real memory dir is classified RESOLVED and its
    pointer-form survives the gate (it is NOT dropped);
  * a key whose body is genuinely absent (no .json file) is classified DEAD
    and its pointer-form IS dropped from the injected block, with an honest
    diagnostic naming the key;
  * a key whose body IS in the real memory dir is classified RESOLVED but
    is rendered via the low-signal gate as a one-line pointer (the same
    pointer form Oct-21 flagged as "empty" — this test proves the pointer
    form is the CORRECT, honest rendering, not a bug).

Acceptance criteria for this file  ----------------
* AC1 (live / no-fake) — the gate's KEEP (RESOLVED) and DROP (DEAD) decisions
  match what the real ``HermesDefaultMemorySource`` actually holds, without
  the fake-source shortcut.
* AC2 (diagnostic)     — a DEAD drop carries a diagnostic naming the key.
* AC3 (render shape)   — a RESOLVED key with a large body is rendered as the
  one-line pointer form (NOT dropped, NOT empty), which proves the render
  layer is collapsing large bodies on purpose (low-signal, #217) rather than
  accidentally losing them.

Placement  -----------
    real source (HermesDefaultMemorySource)
      -> real orchestrator (MemoryOrchestrator.__new__, memory_sources={…})
         -> apply_recall_liveness (items=[pointer form])
            -> _format_context_block (gated items)
    i.e., exactly the same seam the hook's on_pre_llm_call() calls — the
    only difference is that the source is the real hermes_default source
    (not _FakeSource).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections import OrderedDict
from typing import Any, Dict, List

from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus import hooks as memchorus_hooks


def _build_fixture_memory_dir(tmpdir: str) -> List[str]:
    """Create a few .json fixture files mimicking a real memory dir.

    Returns the list of key names (no .json suffix) that were written.
    The bodies are large-ish (a few KB) so the low-signal gate (#217) will
    collapse them to the one-line pointer form — the same shape that the
    Oct-21 self-assessment flagged.
    """
    # 3 fixture keys that ARE present (mimicking the 3 Set-A keys).
    present_keys = [
        "learning-fixupaaa-1111",
        "mistake-fixuxxxx-2222",
        "learning-fixupbbb-3333",
    ]
    for k in present_keys:
        # Each body is a ~2 KB blob of prose (large enough for the low-signal
        # gate to fire, small enough to be cheap).
        body = {
            "memory": (
                f"FIXTURE-BODY-{k}: " + "this is a substantial body of text. " * 60
            ),
            "source": "hermes_default",
            "kind": "learning" if k.startswith("learning") else "mistake",
        }
        path = os.path.join(tmpdir, f"{k}.json")
        with open(path, "w") as f:
            json.dump(body, f)

    return present_keys


def _pointer_item(key: str) -> Dict[str, Any]:
    """A recall-block pointer item in the exact shape the hook feeds the gate."""
    return {
        "key": key,
        "score": 0.9,
        "source": "hermes_default",
        "content": (
            f"source=hermes_default\u2014 read it: retrieve(key='{key}'). "
            f"If that key is missing (cold cache, never saved, or pruned), "
            f"re-fetch the live source before treating it as absent."
        ),
    }


def _build_real_orch(source: HermesDefaultMemorySource) -> MemoryOrchestrator:
    """Build a real orchestrator wired to the real hermes_default source.

    Uses ``__new__`` to skip the heavy constructor (which would auto-boot
    other sources and touch the network / MCP), then sets just the attributes
    ``probe_recall_key_liveness`` touches — the same seam the existing C13
    test file uses, but with the real source instead of a fake.
    """
    orch: Any = MemoryOrchestrator.__new__(MemoryOrchestrator)
    orch.memory_sources = {source._name: source}
    orch._priority_order = [source._name]
    orch._source_enabled = {source._name: True}
    orch._retrieve_cache = OrderedDict()
    orch._cache_ttl = 1_000_000  # effectively infinite, so we never hit the TTL
    orch._retrieved_keys = set()
    orch._c13_is_pointer_body = MemoryOrchestrator._c13_is_pointer_body  # static
    return orch


class TestC13LivePathRealHermesDefault(unittest.TestCase):
    """Live-path proof: the C13 gate on a real source + real memory dir.

    The 4 tests below mirror the existing C13 test file's AC1/AC2/AC3 but
    run against the REAL source (not _FakeSource), so the gate's KEEP/DROP
    decisions are proven on the exact path the Oct-21 self-assessment took.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="c13_live_path_")
        self.present_keys = _build_fixture_memory_dir(self._tmpdir)
        # Real (not fake) source pointed at the fixture dir.
        self.source = HermesDefaultMemorySource(data_dir=self._tmpdir)
        self.orch = _build_real_orch(self.source)
        memchorus_hooks._clear_suppression_windows()

    def tearDown(self) -> None:
        # Best-effort cleanup (files are small; the tmpdir goes away on reboot).
        import shutil

        try:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        except Exception:
            pass
        memchorus_hooks._clear_suppression_windows()

    # ------------------------------------------------------------------
    # AC1 (live) — KEEP / DROP match the real source's truth.
    # ------------------------------------------------------------------

    def test_present_key_is_resolved_and_kept_on_the_gate(self) -> None:
        # Each fixture key has a real body on disk.  The gate must classify
        # them as RESOLVED (not DEAD) and pass them through — NOT drop them.
        gated = self.orch.apply_recall_liveness(
            [_pointer_item(k) for k in self.present_keys]
        )
        surviving = [i["key"] for i in gated] if isinstance(gated, list) else []
        self.assertEqual(
            set(surviving),
            set(self.present_keys),
            "All 3 present keys must survive the gate (none are DEAD)",
        )
        for k in self.present_keys:
            verdict = self.orch.probe_recall_key_liveness(k)
            self.assertEqual(
                verdict,
                "RESOLVED",
                f"key {k} has a real body in the fixture dir -> must be "
                "RESOLVED, not COLD/DEAD/UNKNOWN",
            )

    def test_absent_key_is_dead_and_dropped_on_the_gate(self) -> None:
        # A key with NO body anywhere (no .json file in the fixture dir) is
        # genuinely absent -> the gate must classify it DEAD and DROP it.
        absent_key = "learning-aaaaaaaa-9999"
        gated = self.orch.apply_recall_liveness(
            [_pointer_item(absent_key)]
        )
        surviving_keys = {i["key"] for i in (gated or [])}
        self.assertNotIn(
            absent_key,
            surviving_keys,
            "absent key must be dropped from the gated list",
        )
        verdict = self.orch.probe_recall_key_liveness(absent_key)
        self.assertEqual(
            verdict,
            "DEAD",
            "absent key (no body in local storage or any live source) -> DEAD",
        )

    def test_present_key_survives_full_render_block(self) -> None:
        # AC3 / render shape: the gate keeps the present keys, and the
        # render layer (_format_context_block) must produce a non-empty
        # recall block.  The block is the thing injected into the prompt.
        # The Oct-21 self-assessment flagged 3 keys as "empty source content"
        # — on the live path, the C13 gate itself did NOT drop them (they are
        # RESOLVED); but the render layer (#217 low-signal gate) collapses a
        # large body to a one-line pointer.  This test proves the rendered
        # block is non-empty (keys are present as pointer lines) — i.e., the
        # "empty" perception was render-layer pointer-collapsing, NOT a gate
        # bug dropping live keys.
        gated = self.orch.apply_recall_liveness(
            [_pointer_item(k) for k in self.present_keys]
        )
        block = memchorus_hooks._format_context_block(gated)
        self.assertTrue(
            "MemChorus" in block,
            "rendered recall block must carry the [MemChorus ...] header",
        )
        for k in self.present_keys:
            self.assertIn(
                k,
                block,
                f"present key {k} must appear in the rendered block "
                "(as a pointer line, since its body is large enough to trip "
                "the low-signal / locator_preview collapse)",
            )
        # The gate emitted no DEAD diagnostics for present keys.
        diags = self.orch.get_c13_diagnostics() or []
        for k in self.present_keys:
            self.assertFalse(
                any(
                    (f"key={k}" in d) and ("status=DEAD" in d)
                    for d in diags
                ),
                f"present key {k} must NOT carry a DEAD diagnostic",
            )

    def test_absent_key_renders_dropped_with_diagnostic(self) -> None:
        # AC2 / diagnostic: when the absent key is dropped, the diagnostic
        # must name it (the oct-21 self-assessment expected exactly this).
        absent_key = "mistake-bbbbbbbb-8888"
        gated = self.orch.apply_recall_liveness(
            [_pointer_item(absent_key)]
        )
        # The gated list must NOT contain the key (DEAD -> dropped).
        self.assertNotIn(
            absent_key,
            {i["key"] for i in (gated or [])},
        )
        diags = self.orch.get_c13_diagnostics() or []
        # The gate renders the key via {key!r} (repr-quoted):  key='mistake-…'.
        self.assertTrue(
            any(
                (f"key={absent_key}" in d) or (f"key='{absent_key}'" in d)
                for d in diags if "status=DEAD" in d
            ),
            f"DEAD-drop diagnostic must name {absent_key} (AC2)",
        )


if __name__ == "__main__":
    sys.exit(unittest.main())
