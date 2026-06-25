"""MemChorus Benchmark Suite

Measures search recall, latency, and result quality before and after
orchestrator integration with multiple sources. Outputs delta reports proving
(or disproving) that multi-source routing helps versus single-source baselines.

Usage:
    # Baseline (Hermes-only):
    python3 -m pytest tests/benchmark_memchorus.py::TestBaseline -v -k "not GAP"

    # Post-integration (orchestrator + both sources):
    python3 -m pytest tests/benchmark_memchorus.py::TestPostIntegration -v -k "not GAP"

    # Generate comparison report from saved benchmark JSON:
    python3 tests/benchmark_memchorus.py --report
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import pytest
except ImportError:
    print("pytest required for benchmarks")
    sys.exit(1)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BENCHMARK_DIR = Path(os.path.expanduser("~/.hermes/memchorus_benchmarks"))

# Known facts to seed into memory sources during benchmark runs.
FACTS = [
    {"key": "project_python_version", "value": "Python 3.12 minimum required"},
    {"key": "deployment_target",      "value": "Kubernetes cluster in us-east-1"},
    {"key": "auth_mechanism",         "value": "OAuth2 with client credentials flow"},
    {"key": "database_sharding",      "value": "Shard by user_id hash modulo 64"},
    {"key": "cache_ttl_default",      "value": "300 seconds for API responses"},
    {"key": "monitoring_stack",       "value": "Prometheus + Grafana dashboards"},
    {"key": "ci_runner",             "value": "GitHub Actions ubuntu-latest"},
    {"key": "code_review_policy",     "value": "Two approvals required for master merges"},
    {"key": "incident_response",      "value": "PagerDuty rotation with 15min SLA window"},
    {"key": "data_retention",         "value": "90-day log retention policy"},
    {"key": "feature_flag_system",    "value": "LaunchDarkly integration for canary deploys"},
    {"key": "test_framework",         "value": "pytest with coverage minimum 80pct threshold"},
]

# Queries search by key names (which actually match in this source) so we get
# non-zero baseline recall. After orch integration with multi-source fan-out, 
# even broader semantic queries become possible and should be added here later.
QUERIES = [
    ("project_python_version",      ["project_python_version"]),
    ("deployment_target",           ["deployment_target"]),
    ("auth_mechanism",              ["auth_mechanism"]),
    ("database_sharding",           ["database_sharding"]),
    ("cache_ttl_default",           ["cache_ttl_default"]),
    ("monitoring_stack",            ["monitoring_stack"]),
    ("ci_runner",                   ["ci_runner"]),
    ("code_review_policy",          ["code_review_policy"]),
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _seed_source(source, facts=None):
    """Save known facts into a memory source."""
    if facts is None:
        facts = FACTS
    saved = 0
    for entry in facts:
        ok = source.save(entry["key"], entry["value"])
        if ok:
            saved += 1
    return saved


def _run_queries(search_func, queries=None):
    """Run search queries and measure recall / latency.

    Returns a list of dicts with latency_ms, recall_count, recall_rate, top_result_key.
    """
    if queries is None:
        queries = QUERIES

    results = []
    for query_str, expected_keys in queries:
        start = time.monotonic()
        hits = search_func(query_str)
        elapsed_ms = (time.monotonic() - start) * 1000.0

        matched = set()
        for h in hits:
            key = h.get("key", "") if isinstance(h, dict) else str(h)
            for ek in expected_keys:
                if ek.lower() in key.lower() or key.lower() in ek.lower():
                    matched.add(ek)

        total_expected = len(expected_keys)
        recall_count = len(matched & set(expected_keys))
        recall_rate = recall_count / total_expected if total_expected else 0.0

        top_key = ""
        if hits:
            first = hits[0]
            top_key = first.get("key", "") if isinstance(first, dict) else str(first)

        results.append({
            "query": query_str,
            "latency_ms": round(elapsed_ms, 2),
            "recall_count": recall_count,
            "total_expected": total_expected,
            "recall_rate": round(recall_rate, 3),
            "top_result_key": top_key[:60],
        })

    return results


# --------------------------------------------------------------------------- #
# Unit tests for the benchmark helpers themselves
# --------------------------------------------------------------------------- #

def test_run_queries_returns_correct_structure():
    """Verify _run_queries produces expected dict keys and types."""
    def mock_search(q):
        return [{"key": "auth_mechanism", "content": "OAuth2 flow", "source": "test"}]

    results = _run_queries(mock_search, [("oauth test", ["auth_mechanism"])])
    assert len(results) == 1
    r = results[0]
    assert "latency_ms" in r
    assert "recall_count" in r
    assert "total_expected" in r
    assert "recall_rate" in r
    assert 0 <= r["recall_rate"] <= 1.0


def test_run_queries_empty_results():
    """Ensure empty hits return zero recall without crashing."""
    def mock_search(q):
        return []

    results = _run_queries(mock_search, [("nothing here", ["missing_key"])])
    assert results[0]["recall_count"] == 0
    assert results[0]["recall_rate"] == 0.0


def test_seed_source_counts_saves():
    """Verify _seed_source returns correct save count."""
    class FakeSource:
        def save(self, key, value):
            return True

    count = _seed_source(FakeSource(), FACTS[:3])
    assert count == 3


# --------------------------------------------------------------------------- #
# Baseline test class -- single Hermes source only
# --------------------------------------------------------------------------- #

class TestBaseline:
    """Run benchmarks against a single HermesDefaultMemorySource.

    This establishes the before-integration baseline showing what a lone memory
    source achieves without any orchestrator routing or secondary enhancement.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from memchorus.hermes_memory_source import HermesDefaultMemorySource
        self.source = HermesDefaultMemorySource(
            config={"data_dir": str(tmp_path / "hermes_data")}
        )

    def test_seed_facts(self):
        saved = _seed_source(self.source, FACTS)
        assert saved == len(FACTS), "Only {}/{} facts seeded".format(saved, len(FACTS))

    def test_benchmark_search_latency_and_recall(self):
        """Measure per-query latency and recall rate via single source."""
        results = _run_queries(self.source.search, QUERIES)
        avg_latency = sum(r["latency_ms"] for r in results) / len(results)
        avg_recall = sum(r["recall_rate"] for r in results) / len(results)

        print()
        print("[BASELINE] Single-source results:")
        print("  Average latency:   {:.1f} ms per query".format(avg_latency))
        print("  Average recall:    {:.3f}".format(avg_recall))
        for r in results:
            status = "GOOD" if r["recall_rate"] >= 0.5 else "LOW"
            msg = "  [{}] {} -> {}/{}, {}ms".format(
                status, r["query"], r["recall_count"],
                r["total_expected"], r["latency_ms"])
            print(msg)

        report = {
            "mode": "baseline_single_source",
            "timestamp": datetime.utcnow().isoformat(),
            "fact_count": len(FACTS),
            "query_count": len(QUERIES),
            "avg_latency_ms": round(avg_latency, 2),
            "avg_recall_rate": round(avg_recall, 3),
            "per_query": results,
        }
        BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
        bpath = str(BENCHMARK_DIR / "baseline.json")
        with open(bpath, "w") as f:
            json.dump(report, f, indent=2)


# --------------------------------------------------------------------------- #
# Post-integration test class -- orchestrator with both sources
# --------------------------------------------------------------------------- #

class TestPostIntegration:
    """Run benchmarks through the orchestrator fan-out to all registered sources.

    After integrating multiple memory sources behind a single orchestrator, these
    numbers show whether the extra routing actually helps or just adds overhead.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from memchorus.orchestrator import MemoryOrchestrator
        self.tmp_path = tmp_path

        # Orchestrator auto-registers hermes_default + mempalace sources.
        # We override them via config so test stays isolated in tmp_path.
        config = {
            "hermes_default_config": {"data_dir": str(tmp_path / "hermes_data")},
            "mempalace_config": {"skip_mcp": True, "cache_dir": str(tmp_path / "mp_cache")},
        }
        self.orch = MemoryOrchestrator(config=config)

    def test_seed_both_sources(self):
        h_src = self.orch.memory_sources.get("hermes_default")
        m_src = self.orch.memory_sources.get("mempalace")
        assert h_src is not None, "hermes_default source not registered"
        assert m_src is not None, "mempalace source not registered"
        saved_h = _seed_source(h_src, FACTS)
        saved_m = _seed_source(m_src, FACTS)
        assert saved_h == len(FACTS), "Only {}/{} seeded to hermes_default".format(saved_h, len(FACTS))
        assert saved_m > 0, "MemPalace save returned zero"

    def test_benchmark_orchestrator_search(self):
        """Measure per-query latency and recall rate through orchestrator."""
        results = _run_queries(self.orch.search, QUERIES)
        avg_latency = sum(r["latency_ms"] for r in results) / len(results)
        avg_recall = sum(r["recall_rate"] for r in results) / len(results)

        print()
        print("[POST-INTEGRATION] Multi-source orchestrator results:")
        print("  Average latency:   {:.1f} ms per query".format(avg_latency))
        print("  Average recall:    {:.3f}".format(avg_recall))
        for r in results:
            status = "GOOD" if r["recall_rate"] >= 0.5 else "LOW"
            msg = "  [{}] {} -> {}/{}, {}ms".format(
                status, r["query"], r["recall_count"],
                r["total_expected"], r["latency_ms"])
            print(msg)

        report = {
            "mode": "post_integration_orchestrator",
            "timestamp": datetime.utcnow().isoformat(),
            "fact_count": len(FACTS),
            "query_count": len(QUERIES),
            "source_count": len(self.orch.memory_sources),
            "avg_latency_ms": round(avg_latency, 2),
            "avg_recall_rate": round(avg_recall, 3),
            "per_query": results,
        }
        BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
        ppath = str(BENCHMARK_DIR / "post_integ.json")
        with open(ppath, "w") as f:
            json.dump(report, f, indent=2)


# --------------------------------------------------------------------------- #
# CLI helper -- generate comparison report when both baselines exist
# --------------------------------------------------------------------------- #

def _generate_delta_report():
    """Compare baseline vs post-integration and print delta metrics."""
    base_path = BENCHMARK_DIR / "baseline.json"
    post_path = BENCHMARK_DIR / "post_integ.json"

    if not (base_path.exists() and post_path.exists()):
        print("Benchmark reports missing.")
        print("  Expected: {}".format(base_path))
        print("  Expected: {}".format(post_path))
        return False

    with open(str(base_path)) as f:
        baseline = json.load(f)
    with open(str(post_path)) as f:
        post = json.load(f)

    lat_d = post["avg_latency_ms"] - baseline["avg_latency_ms"]
    rec_d = post["avg_recall_rate"] - baseline["avg_recall_rate"]

    print()
    print("=" * 70)
    print("MemChorus Benchmark Comparison Report")
    print("=" * 70)
    print("Facts tested:    {}".format(baseline["fact_count"]))
    print("Queries run:     {}".format(baseline["query_count"]))
    print()

    vl = "PASS" if lat_d < 50 else "FAIL"
    vr = "PASS" if rec_d > 0 else "NEUTRAL"
    print("Metric              | Baseline   | Post       | Delta    | Verdict")
    print("-" * 70)
    tmpl1 = "{:<20} | {:>9.1f} | {:>8.2f} | {:+8.2f} | {}"
    print(tmpl1.format("Avg Latency (ms)", baseline["avg_latency_ms"],
                       post["avg_latency_ms"], lat_d, vl))
    tmpl2 = "{:<20} | {:>9.3f} | {:>8.3f} | {:+8.3f} | {}"
    print(tmpl2.format("Avg Recall Rate", baseline["avg_recall_rate"],
                       post["avg_recall_rate"], rec_d, vr))

    # Per-query detail
    print()
    for bq, pq in zip(baseline["per_query"], post["per_query"]):
        print("  Q: {}".format(bq["query"]))
        print("    Base: {}/{} ({:.0f}%), {}ms".format(
            bq["recall_count"], bq["total_expected"],
            bq["recall_rate"] * 100, bq["latency_ms"]))
        print("    Post: {}/{} ({:.0f}%), {}ms".format(
            pq["recall_count"], pq["total_expected"],
            pq["recall_rate"] * 100, pq["latency_ms"]))

    overall = "PASS" if rec_d > 0 and lat_d < 50 else "NEEDS WORK"
    print()
    print("Verdict: {}".format(overall))
    print("=" * 70)

    comparison = {
        "delta_latency_ms": round(lat_d, 2),
        "delta_recall_rate": round(rec_d, 3),
        "verdict": overall,
        "generated": datetime.utcnow().isoformat(),
    }
    rp = str(BENCHMARK_DIR / "comparison_report.json")
    with open(rp, "w") as f:
        json.dump(comparison, f, indent=2)

    return True


if __name__ == "__main__":
    if "--report" in sys.argv:
        _generate_delta_report()
    else:
        print("Running full benchmark suite...")
        for cls_name in ["TestBaseline", "TestPostIntegration"]:
            result = pytest.main([__file__ + "::" + cls_name, "-v"])
            if result != 0:
                print("{} exited with code {}".format(cls_name, result))
        _generate_delta_report()
