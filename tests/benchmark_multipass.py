#!/usr/bin/env python3
"""Multi-pass decision intelligence benchmark for MemChorus v1.5.0+.

Physical proof protocol: Each pass runs in a separate Python subprocess
with PYTHONPATH cleared, so no module caching or in-memory state can fake results.
Outputs tamper-evident metrics including disk file SHA-256 hashes, timestamps,
and recall improvement curves across passes.

Usage:
    python3 tests/benchmark_multipass.py --report ~/memchorus_bench_report_2026.json

Methodology documented in: live-verification-enforcement skill
Intended as polygraph-style verifiable test for system truthfulness."""

import sys, os, json, subprocess, time, hashlib, argparse
from datetime import datetime

# Domain facts organized by knowledge depth tiers
LEVEL_1_CORE = [
    ("python_minimum_version", "Python 3.12 minimum required for all Hermes Agent deployments and MemChorus package compatibility"),
    ("k8s_deploy_region", "Kubernetes cluster infrastructure runs in us-east-1 availability zone with three node groups for redundancy"),
    ("oauth_flow_type", "Service-to-service authentication uses OAuth2 client credentials flow with rotating refresh tokens every 3600 seconds"),
]

LEVEL_2_INFR = [
    ("db_sharding_strategy", "PostgreSQL database sharded by user identifier hash modulo sixty-four partition segments across availability zones"),
    ("cache_ttl_seconds", "Redis response cache time-to-live default set to three hundred seconds with per-route override capability enabled"),
    ("prometheus_grafana", "Monitoring stack: Prometheus metrics scraped on port nine thousand ninety Grafana dashboards auto-provisioned via Loki logging pipeline"),
]

LEVEL_3_OPS = [
    ("ci_runner_image", "GitHub Actions continuous integration uses ubuntu-latest runners with parallel test matrix across Python three twelve through four point two zero"),
    ("pr_approval_requirement", "Pull request merge policy enforces two human approvals plus green CI checks before merging protected master branch"),
    ("pagerduty_sla", "Incident response SLA maintained by PagerDuty on-call rotation with fifteen minute acknowledgment target and thirty minute resolution escalation window"),
]

ALL_FACTS = LEVEL_1_CORE + LEVEL_2_INFR + LEVEL_3_OPS

# Decision queries that simulate real agent questions requiring memory recall
L1_QUERIES = [
    {"query": "What version of Python is required for deployment", "expects_key": "python_minimum_version"},
    {"query": "Where is the Kubernetes infrastructure deployed geographically", "expects_key": "k8s_deploy_region"},
    {"query": "How do services authenticate to each other", "expects_key": "oauth_flow_type"},
]

L2_QUERIES = [
    {"query": "database sharding partition strategy", "expects_key": "db_sharding_strategy"},
    {"query": "cache TTL configuration seconds", "expects_key": "cache_ttl_seconds"},
    {"query": "monitoring metrics dashboard stack", "expects_key": "prometheus_grafana"},
]

L3_QUERIES = [
    {"query": "CI build runner image version", "expects_key": "ci_runner_image"},
    {"query": "merge approval policy requirements", "expects_key": "pr_approval_requirement"},
    {"query": "incident response time SLA target", "expects_key": "pagerduty_sla"},
]

CHILD_SCRIPT = os.path.join(os.path.dirname(__file__), "_multipass_child.py")


def spawn(mode: str, pass_id: str, payloads=None, queries=None, store_dir: str = None) -> dict:
    """Spawn isolated child process with clean Python interpreter."""
    if not store_dir:
        store_dir = os.environ.get("STORE_DIR", os.path.expanduser("~/.hermes/memchorus_multipass_store"))
    os.makedirs(store_dir, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    env["MODE"] = mode
    env["PASS_ID"] = pass_id
    env["STORE_DIR"] = store_dir
    if payloads:
        env["PAYLOADS"] = json.dumps(payloads)
    if queries:
        env["QUERIES"] = json.dumps(queries)

    start_t = time.time()
    result = subprocess.run(
        [sys.executable, CHILD_SCRIPT],
        capture_output=True, text=True, timeout=60, env=env,
    )
    elapsed_s = round(time.time() - start_t, 3)
    
    if result.returncode != 0:
        return {"error": result.stderr.strip(), "elapsed_s": elapsed_s}
    
    try:
        data = json.loads(result.stdout.strip())
        data["process_elapsed_s"] = elapsed_s
        return data
    except json.JSONDecodeError:
        return {"error": f"Child output not JSON: {result.stdout[:200]}", "elapsed_s": elapsed_s}


def run_benchmark(report_path: str):
    """Execute full multi-pass benchmark and save report."""
    store_dir = os.path.expanduser("~/.hermes/memchorus_multipass_store")
    
    # Clear previous state for reproducibility
    import shutil
    mem_path = os.path.join(store_dir, "memories")
    if os.path.isdir(mem_path):
        for f in os.listdir(mem_path):
            fp = os.path.join(mem_path, f)
            if os.path.isfile(fp) and f.endswith(".json"):
                os.remove(fp)
    
    passes = []
    timestamp = datetime.now().isoformat()
    git_commit = "unknown"
    try:
        from git import Repo
        repo = Repo(os.path.expanduser("~/MemChorus"))
        git_commit = repo.head.commit.hexsha[:10]
    except Exception:
        pass
    
    print("=" * 78)
    print("MEMCHORUS MULTI-PASS DECISION INTELLIGENCE BENCHMARK")
    print(f"Timestamp: {timestamp}")
    print(f"Child script: {CHILD_SCRIPT}")
    print(f"Git revision: {git_commit}")
    print("=" * 78)
    
    # PASS 1: Cold start query - no memory seeded yet
    print("\n[Pass 1] COLD START - Querying before any knowledge seeded")
    p1 = spawn("recall", "1", queries=L1_QUERIES, store_dir=store_dir)
    passes.append(p1)
    if "error" in p1:
        print(f"  ERROR: {p1['error']}")
    else:
        n_query = p1["total_queries"]
        n_correct = p1["correct_count"]
        recall = n_correct / max(n_query, 1) * 100
        lat = p1["avg_latency_ms"]
        print(f"  Recall: {n_correct}/{n_query} ({recall:.0f}%) | Avg latency: {lat:.1f}ms")
        print(f"  Disk files: {p1['disk_file_count']}")
    
    # Seed Level 1 facts
    print("\n[Seeding] Loading Level 1 core deployment facts (3 items)")
    s1 = spawn("seed", "s1", payloads=LEVEL_1_CORE, store_dir=store_dir)
    passes.append(s1)
    if "error" in s1:
        print(f"  ERROR: {s1['error']}")
    else:
        print(f"  Saved: {s1['saved_count']}/{len(LEVEL_1_CORE)} | Disk files: {s1['disk_file_count']}")
    
    # PASS 2: After Level 1 seeded - queries should improve
    print("\n[Pass 2] WARM - Querying after L1 facts seeded")
    p2 = spawn("recall", "2", queries=L1_QUERIES, store_dir=store_dir)
    passes.append(p2)
    if "error" in p2:
        print(f"  ERROR: {p2['error']}")
    else:
        n_q = p2["total_queries"]
        n_c = p2["correct_count"]
        recall = n_c / max(n_q, 1) * 100
        lat = p2["avg_latency_ms"]
        print(f"  Recall: {n_c}/{n_q} ({recall:.0f}%) | Avg latency: {lat:.1f}ms")
    
    # Seed Level 2 facts
    print("\n[Seeding] Loading Level 2 infrastructure facts (3 items)")
    s2 = spawn("seed", "s2", payloads=LEVEL_2_INFR, store_dir=store_dir)
    passes.append(s2)
    if "error" in s2:
        print(f"  ERROR: {s2['error']}")
    else:
        print(f"  Saved: {s2['saved_count']}/{len(LEVEL_2_INFR)} | Disk files: {s2['disk_file_count']}")
    
    # PASS 3: After L1+L2 seeded - combined query set (L1+L2 questions)
    print("\n[Pass 3] WARMER - Querying after L1+L2 facts seeded")
    p3 = spawn("recall", "3", queries=L1_QUERIES + L2_QUERIES, store_dir=store_dir)
    passes.append(p3)
    if "error" in p3:
        print(f"  ERROR: {p3['error']}")
    else:
        n_q = p3["total_queries"]
        n_c = p3["correct_count"]
        recall = n_c / max(n_q, 1) * 100
        lat = p3["avg_latency_ms"]
        print(f"  Recall: {n_c}/{n_q} ({recall:.0f}%) | Avg latency: {lat:.1f}ms")
    
    # Seed Level 3 facts
    print("\n[Seeding] Loading Level 3 operations facts (3 items)")
    s3 = spawn("seed", "s3", payloads=LEVEL_3_OPS, store_dir=store_dir)
    passes.append(s3)
    if "error" in s3:
        print(f"  ERROR: {s3['error']}")
    else:
        print(f"  Saved: {s3['saved_count']}/{len(LEVEL_3_OPS)} | Disk files: {s3['disk_file_count']}")
    
    # PASS 4: Fully saturated - all 9 facts loaded, all queries asked
    all_queries = L1_QUERIES + L2_QUERIES + L3_QUERIES
    print("\n[Pass 4] SATURATED - Querying with full knowledge base")
    p4 = spawn("recall", "4", queries=all_queries, store_dir=store_dir)
    passes.append(p4)
    if "error" in p4:
        print(f"  ERROR: {p4['error']}")
    else:
        n_q = p4["total_queries"]
        n_c = p4["correct_count"]
        recall = n_c / max(n_q, 1) * 100
        lat = p4["avg_latency_ms"]
        print(f"  Recall: {n_c}/{n_q} ({recall:.0f}%) | Avg latency: {lat:.1f}ms")
    
    # PASS 5: Persistence verification - completely new process loads previously saved data
    print("\n[Pass 5] PERSISTENCE - New process loads old data, queries same questions")
    p5 = spawn("recall", "5", queries=all_queries, store_dir=store_dir)
    passes.append(p5)
    if "error" in p5:
        print(f"  ERROR: {p5['error']}")
    else:
        n_q = p5["total_queries"]
        n_c = p5["correct_count"]
        recall = n_c / max(n_q, 1) * 100
        lat = p5["avg_latency_ms"]
        print(f"  Recall: {n_c}/{n_q} ({recall:.0f}%) | Avg latency: {lat:.1f}ms")
    
    # ====== PHYSICAL PROOF OUTPUT ====== 
    print("\n" + "=" * 78)
    print("PHYSICAL PROOF - DISK FILE EVIDENCE")
    print("=" * 78)
    if "file_hashes" in p4:
        for fname, h in sorted(p4["file_hashes"].items()):
            fsize = os.path.getsize(os.path.join(mem_path, fname)) if (os.path.exists(os.path.join(mem_path, fname))) else -1
            mtime = os.path.getmtime(os.path.join(mem_path, fname)) if os.path.exists(os.path.join(mem_path, fname)) else 0
            ts_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {fname}: SHA256={h}... | size={fsize}b | mtime={ts_str}")
    
    # ====== SUMMARIZED METRICS TABLE ======
    print("\n" + "=" * 78)
    print("MULTI-PASS BENCHMARK RESULTS")
    print("=" * 78)
    print(f"{'Pass':<6} {'Label':<18} {'Recall':>7} {'Correct':>9} {'Latency':>8} {'Files':>6}")
    print("-" * 78)
    
    labels = ["Cold Start", "Warm (L1)", "Warmer (L1+L2)", "Saturated (All)", "Persistence"]
    pass_indices = [0, 2, 4, 6, 7]  # recall pass indices in passes list
    for display_num, (idx, label) in enumerate(zip(pass_indices, labels), start=1):
        p = passes[idx] if idx < len(passes) else {}
        if "error" in p:
            print(f"{display_num:<6} {label:<18} ERRORED  {p.get('error', '?')[:40]}")
        else:
            n_q = p.get("total_queries", 0)
            n_c = p.get("correct_count", 0)
            recall_pct = (n_c / max(n_q, 1)) * 100 if isinstance(n_q, int) else 0.0
            lat = p.get("avg_latency_ms", 0.0)
            print(f"{display_num:<6} {label:<18} {recall_pct:>6.0f}% {n_c}/{n_q}   {lat:>7.1f}ms  {p.get('disk_file_count', '?'):>5}")
    
    # Calculate key metric: recall improvement from cold to saturated
    if "total_queries" in passes[0] and "total_queries" in passes[6]:
        cold_recall = (passes[0]["correct_count"] / max(passes[0]["total_queries"], 1)) * 100
        sat_recall = (passes[6]["correct_count"] / max(passes[6]["total_queries"], 1)) * 100
        improvement = sat_recall - cold_recall
        
        print("-" * 78)
        print(f"\nRecall improvement (Cold -> Saturated): +{improvement:.1f} percentage points")
        print(f"Total physical files on disk: {passes[6].get('disk_file_count', '?')}")
        
        if sat_recall > cold_recall and sat_recall >= 50.0:
            verdict = "PASS - Accumulated memory demonstrably improves decisions"
        else:
            verdict = "NEEDS WORK - Insufficient improvement across passes"
        print(f"Verdict: {verdict}")
    else:
        print("\nCould not compute improvement metric (insufficient data)")
    
    # Save machine-readable report
    report = {
        "timestamp": timestamp,
        "benchmark_type": "multi_pass_decision_intelligence",
        "methodology_url": "live-verification-enforcement skill",
        "child_script": CHILD_SCRIPT,
        "subprocess_isolation": True,
        "git_revision": git_commit,
        "passes": passes,
        "total_facts_loaded": len(ALL_FACTS),
        "store_directory": store_dir,
    }
    
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    
    print(f"\nFull metrics report saved to: {report_path}")
    # Hash the report itself for tamper evidence
    with open(report_path, "rb") as f:
        raw = f.read()
    report_hash = hashlib.sha256(raw).hexdigest()
    print(f"Report SHA-256: {report_hash}")
    print("=" * 78)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MemChorus multi-pass benchmark")
    parser.add_argument("--report", default=os.path.expanduser("~/.hermes/memchorus_multipass_report.json"))
    args = parser.parse_args()
    run_benchmark(args.report)
