#!/usr/bin/env python3
"""Child process for multi-pass persistence proof.
Runs in isolated Python interpreter with no module caching.
Outputs exactly one line of JSON to stdout."""
import sys, os, json, hashlib

os.environ["PYTHONPATH"] = ""

STORE_DIR = os.environ.get("STORE_DIR", "/tmp/mcp_bench_store")
MODE = os.environ["MODE"]  # "seed" or "recall"
PASS_ID = os.environ.get("PASS_ID", "0")

def _run():
    from memchorus.orchestrator import MemoryOrchestrator
    
    pkg = sys.modules["memchorus"].__file__
    
    orch = MemoryOrchestrator(config={
        "hermes_default_config": {"memory_dir": os.path.join(STORE_DIR, "memories")},
        "mempalace_config": {"skip_mcp": True, "cache_dir": os.path.join(STORE_DIR, "mp_cache")},
    })
    
    if MODE == "seed":
        payload = json.loads(os.environ["PAYLOADS"])
        saved = 0
        for key, val in payload:
            ok = orch.save(key, val)
            if ok:
                saved += 1
        
        # Count physical files on disk right after seeding
        disk_files = []
        mem_path = os.path.join(STORE_DIR, "memories")
        if os.path.isdir(mem_path):
            disk_files = sorted([f for f in os.listdir(mem_path) if f.endswith(".json")])
        
        file_hashes = {}
        for fn in disk_files:
            fp = os.path.join(mem_path, fn)
            with open(fp, "r") as fh:
                raw = fh.read()
            file_hashes[fn] = hashlib.sha256(raw.encode()).hexdigest()[:16]
        
        print(json.dumps({
            "mode": MODE,
            "pass_id": PASS_ID,
            "saved_count": saved,
            "disk_file_count": len(disk_files),
            "package_path": pkg,
            "file_hashes": file_hashes,
            "store_dir": STORE_DIR,
        }))
    
    elif MODE == "recall":
        queries = json.loads(os.environ["QUERIES"])
        results = []
        for q_info in queries:
            import time
            t0 = time.monotonic()
            hits = orch.search(q_info["query"], limit=5)
            ms = (time.monotonic() - t0) * 1000.0
            
            wants_key = q_info["expects_key"]
            found = False
            top_hit_key = ""
            if hits:
                found = any(wants_key.lower().replace("_", "-") in h.get("key", "").lower() for h in hits)
                top_hit_key = hits[0].get("key", "")[:60]
            
            results.append({
                "query": q_info["query"],
                "latency_ms": round(ms, 2),
                "hit_count": len(hits),
                "expected_key": wants_key,
                "correct_at_top": found,
                "top_key": top_hit_key,
            })
        
        disk_files = []
        mem_path = os.path.join(STORE_DIR, "memories")
        if os.path.isdir(mem_path):
            disk_files = sorted([f for f in os.listdir(mem_path) if f.endswith(".json")])
        
        file_hashes = {}
        for fn in disk_files:
            fp = os.path.join(mem_path, fn)
            with open(fp, "r") as fh:
                raw = fh.read()
            file_hashes[fn] = hashlib.sha256(raw.encode()).hexdigest()[:16]
        
        print(json.dumps({
            "mode": MODE,
            "pass_id": PASS_ID,
            "query_results": results,
            "correct_count": sum(1 for r in results if r["correct_at_top"]),
            "total_queries": len(results),
            "avg_latency_ms": round(sum(r["latency_ms"] for r in results) / max(len(results), 1), 2),
            "disk_file_count": len(disk_files),
            "file_hashes": file_hashes,
            "package_path": sys.modules["memchorus"].__file__,
        }))

if __name__ == "__main__":
    try:
        _run()
    except Exception as e:
        print(json.dumps({"error": str(e), "traceback": "CHILD_PROCESS_FAILED"}))
        sys.exit(1)
