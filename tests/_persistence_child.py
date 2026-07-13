"""Child worker for multi-run persistence subprocess tests."""
import sys, os, json, hashlib

# Busted cached modules so each subprocess gets a fresh copy
for mod in list(sys.modules.keys()):
    if "memchorus" in mod:
        del sys.modules[mod]
sys.path.insert(0, "/home/bubo/MemChorus/src")

from memchorus.hermes_memory_source import HermesDefaultMemorySource

run_id = int(os.environ["RUN_ID"])
mode = os.environ["MODE"]
store_dir = os.environ["STORE_DIR"]
src = HermesDefaultMemorySource(name="subproc_" + str(run_id), config={"memory_dir": store_dir})

if mode == "store":
    payloads = json.loads(os.environ["PAYLOADS_JSON"])
    results = []
    for payload in payloads:
        key = "persist_r" + str(run_id) + "_" + hashlib.md5(payload.encode()).hexdigest()[:8]
        saved = src.save(key, payload)
        retrieved = src.retrieve(key)
        results.append({"key": key, "payload": payload, "saved_ok": (retrieved == payload)})
    print(json.dumps({"ok": True, "mode": "store", "run_id": run_id, "count": len(results), "results": results}))
elif mode == "recall":
    expected_ids = os.environ["EXPECTED_IDS"].split(",")
    matched = []
    missing = []
    for eid in expected_ids:
        val = src.retrieve(eid)
        if val is not None:
            matched.append({"key": eid, "retrieved": True, "value": val})
        else:
            missing.append(eid)
    print(json.dumps({"ok": True, "mode": "recall", "run_id": run_id, "matched_count": len(matched), "results": matched, "missing_ids": missing}))
else:
    print(json.dumps({"ok": False, "error": "unknown mode"}))
