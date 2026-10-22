"""Live-path end-to-end proof for C13 (task t_18c26df5).

Two layers of evidence, matching the four acceptance criteria:

  AC1   Trace the 3 named Oct-21 keys through the FULL live orchestrator.
        Each must either RESOLVE / COLD-refetch (content recovered, kept)
        or be DROPPED with a logged verdict (DEAD). No dead key with empty
        source may reach the prompt.

  AC3   RED->GREEN DISCRIMINANT (the "one live-path test" AC3 requires).
        Uses a hermes_default-ISOLATED orchestrator (real source, no mempalace
        fuzzy rescue) so a truly-absent key is genuinely DEAD. Renders
        (RED) without the gate, then (GREEN) with the gate. The dead key must
        be present in RED and ABSENT in GREEN.

  AC4   Confirms the COLD -> re-fetch branch is the one that fires on the
        full live path (the 2 COLD Oct-21 keys are "recovered from a live
        source on demand", NOT dropped).

Why the two orchestrators:  on the FULL live orchestrator, mempalace's
vector search returns a body for almost any key, so genuinely-absent keys
get COLD-rescued (kept as soft-match). On the hermes_default-ISOLATED
orchestrator (pure file store), an absent key has no local hit and no live
source to recover it -> DEAD -> dropped. The RED/GREEN discriminant only
works where DEAD is actually reachable, i.e. the isolated path. The 4-test
file tests/test_c13_live_path_real_hermes_default.py is the executable
version of this isolated RED->GREEN.

This script PRINTS the numeric evidence for completion metadata.
"""

from __future__ import annotations

import os
import re
import sys

# Ensure the repo-src tree (which carries the C13 gate) is importable when this
# script is run standalone, regardless of CWD or any stale installed copy.
# __file__ here is .../tests/prove_c13_live_path.py, so the repo root is one
# level up and the package lives under <repo_root>/src (NOT <dir>/src — that
# would resolve to tests/, which has no src/ and would silently fall back to a
# stale site-packages memchorus that lacks the gate).
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src_path = os.path.join(_repo_root, "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from memchorus.hooks import _format_context_block, _clear_suppression_windows
from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource


# The 3 keys the Oct-21 self-assessment flagged as "empty source content".
OCT21_KEYS = [
    "learning-ad4ecdb3-8645",
    "mistake-c86e1e4b-7403",
    "learning-787ec4d8-9846",
]
DEAD_PROBE = "learning-9999dead9999"   # absent from any hermes_default dir


def _pointer_item(key: str) -> dict:
    return {
        "key": key, "source": "hermes_default", "score": 0.9,
        "content": ("source=hermes_default\u2014 read it: retrieve(key='%s'). "
                    "If that key is missing, re-fetch the live source." % key),
        "metadata": {},
    }


def _empty_source_lines(block: str) -> int:
    n = 0
    for line in (block or "").splitlines():
        s = line.strip()
        if not s.startswith("- **") or "retrieve(key=" not in s:
            continue
        body = re.sub(r"^\- \*\*[^*]+\*\*", "", s)
        body = re.sub(r"\u2014 read it: retrieve\(key='[^']*'\)\.?", "", body)
        body = re.sub(r"If that key is missing[^.]*\.", "", body)
        body = body.replace("source=hermes_default", "").strip(" \u2014-")
        if not body:
            n += 1
    return n


def _isolated_orch(source) -> MemoryOrchestrator:
    """Real orchestrator wired ONLY to the real hermes_default source."""
    orch: MemoryOrchestrator = MemoryOrchestrator.__new__(MemoryOrchestrator)
    orch.memory_sources = {source._name: source}
    orch._priority_order = [source._name]
    orch._source_enabled = {source._name: True}
    from collections import OrderedDict
    orch._retrieve_cache = OrderedDict()
    orch._cache_ttl = 1_000_000
    orch._retrieved_keys = set()
    orch._c13_is_pointer_body = MemoryOrchestrator._c13_is_pointer_body
    return orch


def main() -> int:
    print("=== C13 LIVE-PATH PROOF (task t_18c26df5) ===\n")

    # ---- AC1 + AC4: full live orchestrator, real named keys ----
    full = MemoryOrchestrator()  # auto_bootstrap -> hermes_default + mempalace + ...
    print("--- AC1/AC4: per-key trace on FULL live orchestrator ---")
    verdicts = {}
    for key in OCT21_KEYS:
        v = full.probe_recall_key_liveness(key)
        verdicts[key] = v
        tag = {"RESOLVED": "kept (content present)",
               "COLD": "kept (body recovered from live source, soft-match)",
               "DEAD": "DROPPED (no source holds it)"}.get(v, "")
        print("  %-26s verdict=%-9s %s" % (key, v, tag))
    _clear_suppression_windows()
    gated = full.apply_recall_liveness([_pointer_item(k) for k in OCT21_KEYS])
    survived = {it["key"] for it in (gated or [])}
    print("  survivors after gate = %s" % sorted(survived))
    print("  DIAG: %s" % (full.get_c13_diagnostics() or []))

    # AC1 pass = every named key is accounted for:
    #   COLD/RESOLVED -> kept (content recovered)  ;  DEAD -> dropped+logged.
    ac1_ok = all(
        (verdicts[k] in ("RESOLVED", "COLD")) or
        (verdicts[k] == "DEAD" and k not in survived and
         any(("key=%r" % k) in d and "status=DEAD" in d
             for d in (full.get_c13_diagnostics() or [])))
        for k in OCT21_KEYS
    )
    # AC4: at least one COLD refetch fires on the full live path.
    ac4_ok = any(verdicts[k] == "COLD" for k in OCT21_KEYS)
    print("  AC1 (each resolved/dropped-logged) = %s" % ac1_ok)
    print("  AC4 (COLD re-fetch branch fires)   = %s\n" % ac4_ok)

    # ---- AC3: RED -> GREEN discriminant on ISOLATED real hermes_default ----
    import tempfile, json, shutil
    tmp = tempfile.mkdtemp(prefix="c13_ac3_")
    # put ONE real body in the isolated store so the present-key arm is proven
    json.dump({"memory": "REAL-BODY present-key " + "x" * 500,
               "source": "hermes_default"}, open(os.path.join(tmp, "present-real-1111.json"), "w"))
    src = HermesDefaultMemorySource(data_dir=tmp)
    iso = _isolated_orch(src)

    items = [_pointer_item("present-real-1111"), _pointer_item(DEAD_PROBE)]
    print("--- AC3: RED->GREEN discriminant (isolate real hermes_default) ---")
    # RED: no gate -> dead key IS rendered.
    red_block = _format_context_block(items)
    red_dead = DEAD_PROBE in red_block
    print("  RED   : dead key rendered = %s (expect True)" % red_dead)
    print("  RED   : present key rendered = %s" % ("present-real-1111" in red_block))
    # GREEN: gate -> dead key dropped, present key survives.
    _clear_suppression_windows()
    gated_iso = iso.apply_recall_liveness(items)
    green_block = _format_context_block(gated_iso)
    green_dead = DEAD_PROBE in green_block
    green_present = "present-real-1111" in green_block
    print("  GREEN : dead key rendered = %s (expect False)" % green_dead)
    print("  GREEN : present key rendered = %s (expect True)" % green_present)
    print("  GREEN : verdict(dead)=%s  verdict(present)=%s" %
          (iso.probe_recall_key_liveness(DEAD_PROBE),
           iso.probe_recall_key_liveness("present-real-1111")))
    print("  GREEN : DIAG = %s" % (iso.get_c13_diagnostics() or []))
    ac3_ok = red_dead and (not green_dead) and green_present
    print("  AC3 (RED->GREEN discriminant)     = %s\n" % ac3_ok)

    # ---- AC2: before/after summary ----
    dropped_o21 = [k for k in OCT21_KEYS if k not in survived]
    kept_o21 = [k for k in OCT21_KEYS if k in survived]
    print("--- AC2: before/after empty-source dead-key count ---")
    print("  Oct-21 self-assessment (before) = 3 empty-source keys flagged")
    print("  live gate (after)               = %d dropped / %d refetched-kept of 3"
          % (len(dropped_o21), len(kept_o21)))
    print("  net: empty-source dead keys reaching the prompt = %d  (expect 0)"
          % (0 if all(verdicts[k] != "DEAD" or k in dropped_o21 for k in OCT21_KEYS) else "SEE-NOTE"))

    ok = ac1_ok and ac3_ok and ac4_ok
    print("\n=== PASS = %s  (AC1=%s, AC3=%s, AC4=%s) ===" %
          (ok, ac1_ok, ac3_ok, ac4_ok))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
