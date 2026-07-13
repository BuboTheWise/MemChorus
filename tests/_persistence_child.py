#!/usr/bin/env python3
"""Child worker for multi-run persistence subprocess tests.

Invoked by test_multi_run_persistence.py via separate Python processes.
Uses env vars for configuration. Outputs JSON to stdout."""

import sys, json, os

sys.path.insert(0, "/home/bubo/MemChorus/src")

from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource

run_id   = int(os.environ.get("RUN_ID", "0"))
mode     = os.environ.get("MODE", "store")
store    = os.environ.get("STORE_DIR", "/tmp/memtest")
payloads = json.loads(os.environ.get("PAYLOADS_JSON", "[]"))
exp_raw  = os.environ.get("EXPECTED_IDS", "")
expected = [x.strip() for x in exp_raw.split(",") if x.strip()]

os.makedirs(store, exist_ok=True)

orc = MemoryOrchestrator(config={})
src = HermesDefaultMemorySource(name="persist", config={"memory_dir": store})
orc.memory_sources["persist"] = src

if mode == "store":
    results = []
    for idx, text in enumerate(payloads):
        key_id = "r{}_item{}".format(run_id, idx)
        saved = orc.save(key_id, text, source_name="persist")
        ok = bool(saved)
        results.append({"key": key_id, "saved_ok": ok})
    out = {
        "ok": True, "mode": "store", "run": run_id,
        "count": len(results), "results": results,
    }
    print(json.dumps(out))

elif mode == "recall":
    found_ids = []
    missing_ids = []
    for eid in expected:
        hit = src.retrieve(eid)
        if hit is not None:
            found_ids.append(eid)
        else:
            missing_ids.append(eid)
    out = {
        "ok": True, "mode": "recall", "run": run_id,
        "found_ids": found_ids, "missing_ids": missing_ids,
        "total_expected": len(expected), "matched_count": len(found_ids),
    }
    print(json.dumps(out))
