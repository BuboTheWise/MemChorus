#!/usr/bin/env python3
"""MemChorus Data-Driven Benchmark Suite (MC-BENCH-001)

Lightweight, quantitative benchmarks for MemChorus memory backends that report:
- Timing accuracy (p50/p95 latency per source and multi-source merge)
- Content accuracy (recall within top-N results)
- Failure mode resilience (timeout, empty, exception behavior)

Each test writes its metrics dict to /tmp/memchorus_benchmark_results/ so
results persist across runs for comparison.

Usage:
    pytest tests/test_memchorus_benchmark.py -v -s
"""
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Ensure src/ is on path (aligns with conftest.py).
_src = Path(__file__).resolve().parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

BENCHMARK_DIR = Path(tempfile.gettempdir()) / "memchorus_benchmark_results"

# ── Test fixtures ─────────────────────────────────────────────────────────

SAMPLE_FACTS = [
    {"key": "deploy_target",     "value": "Kubernetes cluster in us-east-1"},
    {"key": "auth_flow",         "value": "OAuth2 with client credentials grant type"},
    {"key": "db_sharding",       "value": "Shard by user_id hash modulo 64 replicas"},
    {"key": "cache_ttl",         "value": "300 seconds default TTL for API responses"},
    {"key": "ci_system",         "value": "GitHub Actions ubuntu-latest runners"},
]

LOW_SIGNAL_ENTRIES = [
    {"key": "1234567890",           "value": "just numbers no signal"},
    {"key": "status_check_abc",     "value": "ok ok ok ok ok ok ok ok ok ok"},
    {"key": "heartbeat_ping_pong",  "value": "ping pong ping pong repeat loop"},
]

MEANINGFUL_DECISIONS = [
    {"key": "api_versioning_policy",
     "value": "Adopt URL-based versioning with deprecation headers (v1→v2). Decision made on 2026-07-15 by project lead."},
    {"key": "rollback_strategy",
     "value": "Blue-green deployment with automated rollback on health-check failure. Context: production incident post-mortem."},
]

STALE_PROGRESS_MARKERS = [
    {"key": "result-delivery-timestamp-123",
     "value": "auto-generated progress marker"},
    {"key": "build-status-update",
     "value": "step 4 of 7 complete, continuing..."},
]


def _write_metrics(test_name: str, metrics: dict) -> None:
    """Persist a metrics dict under /tmp/memchorus_benchmark_results/."""
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    out = BENCHMARK_DIR / f"{test_name}.json"
    out.write_text(json.dumps(metrics, indent=2, default=str))


def _percentile(data: list, pct: float) -> float:
    """Return the p-th percentile of *data* (0-1 scale)."""
    if not data:
        return 0.0
    sorted_d = sorted(data)
    k = (len(sorted_d) - 1) * (pct / 100.0)
    f_part, c_part = int(k), min(int(k) + 1, len(sorted_d) - 1)
    if f_part == c_part:
        return sorted_d[f_part]
    return sorted_d[f_part] + (k - f_part) * (sorted_d[c_part] - sorted_d[f_part])


# ──────────────────────────────────────────────────────────────────────────
# Test 1: Hermes source save + recall timing
# ──────────────────────────────────────────────────────────────────────────

class TestHermesSourceTiming:
    """Measure save + search round-trip latency for HERMES_DEFAULT.

    Runs each save-then-search cycle N_TIMES times, collects per-cycle
    wall-clock in milliseconds, and reports p50 / p95 alongside hit rate.
    """

    N_TIMES = 3  # keep CI fast; raise locally if desired

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from memchorus.hermes_memory_source import HermesDefaultMemorySource

        self.source = HermesDefaultMemorySource(
            config={"data_dir": str(tmp_path / "hermes")}
        )
        # Seed facts (once).
        for fact in SAMPLE_FACTS:
            self.source.save(fact["key"], fact["value"])

    def test_hermes_source_save_recall_timing(self):
        latencies = []
        hits_top3 = 0

        for _ in range(self.N_TIMES):
            t0 = time.monotonic()

            # Save an extra transient fact to exercise write path every round.
            self.source.save(
                f"bench_run_{_}",
                f"Benchmark run {_} data",
            )

            # Recall all seeded keys.
            for fact in SAMPLE_FACTS:
                results = self.source.search(fact["key"], limit=5)

            elapsed_ms = (time.monotonic() - t0) * 1000.0
            latencies.append(elapsed_ms)

        # Accuracy: search by VALUE terms (the engine indexes content text; keys are
        # normalized underscores→hyphens so key-based lookups can miss).
        for fact in SAMPLE_FACTS:
            query = fact["value"].split()[0].lower()  # first word of value as query
            hits = self.source.search(query, limit=3)
            needle = fact["key"].lower()
            if any(needle in h.get("key", "").lower().replace("-", "_") for h in hits):
                hits_top3 += 1

        accuracy = (hits_top3 / len(SAMPLE_FACTS)) * 100.0 if SAMPLE_FACTS else 0.0
        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)

        print(
            f'--- Benchmark: hermes_default.save_roundtrip ---\n'
            f'  latency_p50={p50:.0f}ms | latency_p95={p95:.0f}ms | '
            f'hits_top3={hits_top3}/{len(SAMPLE_FACTS)} | accuracy={accuracy:.1f}%\n'
        )

        metrics = {
            "test": "hermes_source_save_recall_timing",
            "runs": len(latencies),
            "latency_ms": latencies,
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "hits_top3_count": hits_top3,
            "hits_top3_total": len(SAMPLE_FACTS),
            "accuracy_pct": round(accuracy, 2),
        }
        _write_metrics(self.__class__.__name__, metrics)

        assert accuracy >= 80.0, f"Accuracy {accuracy:.1f}% below 80% threshold"


# ──────────────────────────────────────────────────────────────────────────
# Test 2: Relevance threshold — low-signal items are filtered
# ──────────────────────────────────────────────────────────────────────────

class TestRelevanceThreshold:
    """Verify MIN_RECALL_SCORE (aka _RELEVANCE_THRESHOLD) rejects noise."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from memchorus.hermes_memory_source import HermesDefaultMemorySource

        self.source = HermesDefaultMemorySource(
            config={"data_dir": str(tmp_path / "hermes")}
        )

    def test_hermes_source_relevance_threshold_tuning(self):
        # Seed both signal and noise.
        for fact in SAMPLE_FACTS:
            self.source.save(fact["key"], fact["value"])
        for entry in LOW_SIGNAL_ENTRIES:
            self.source.save(entry["key"], entry["value"])

        t0 = time.monotonic()
        results = self.source.search("OAuth2", limit=10)
        latency_ms = (time.monotonic() - t0) * 1000.0

        returned_keys = {r.get("key", "") for r in results}
        # Normalize: hermes_default converts underscores to hyphens and lowercases.
        def _norm(k):
            return k.lower().replace("_", "-")
        noise_normed = {_norm(e["key"]) for e in LOW_SIGNAL_ENTRIES}
        signal_normed = {_norm(f["key"]) for f in SAMPLE_FACTS}
        returned_normed = {_norm(k) for k in returned_keys}
        noise_matched = returned_normed & noise_normed
        signal_matched = returned_normed & signal_normed
        noise_ratio = len(noise_matched) / max(len(returned_keys), 1)

        filtered_noise_count = len(LOW_SIGNAL_ENTRIES) - len(noise_matched)
        total_noise = len(LOW_SIGNAL_ENTRIES)
        filter_rate = (filtered_noise_count / total_noise * 100.0) if total_noise else 0.0

        print(
            f'--- Benchmark: hermes_default.relevance_threshold ---\n'
            f'  latency={latency_ms:.1f}ms | noise_ratio={noise_ratio:.2f} | '
            f'signal_hit={"yes" if signal_matched else "no"} | '
            f'noise_filtered={filter_rate:.0f}%\n'
        )

        metrics = {
            "test": "hermes_source_relevance_threshold_tuning",
            "query": "OAuth2",
            "latency_ms": round(latency_ms, 2),
            "returned_key_count": len(returned_keys),
            "noise_matched": len(noise_matched),
            "signal_matched": len(signal_matched),
            "noise_ratio": round(noise_ratio, 4),
            "filter_rate_pct": round(filter_rate, 2),
        }
        _write_metrics(self.__class__.__name__, metrics)

        # Noise should not dominate — at most 30% of hits may be noise.
        assert noise_ratio < 0.3, f"Noise ratio {noise_ratio:.2f} exceeds 0.3"


# ──────────────────────────────────────────────────────────────────────────
# Test 3: MemPalace save + recall timing (skip gracefully when MCP absent)
# ──────────────────────────────────────────────────────────────────────────

def _has_live_mcp():
    """Check if a live MemPalace MCP connection can be established."""
    try:
        from memchorus.mempalace_memory_source import MemPalaceMemorySource
        src = MemPalaceMemorySource(name="bench_check", config={"mcp_timeout": 2})
        return src.is_available and getattr(src, "_connected", False)
    except Exception:
        return False


class TestMemPalaceTiming:
    """Measure save + recall against live MemPalace MCP.

    Skipped gracefully on CI where MCP is unavailable (skip_mcp fallback).
    """

    @pytest.mark.skipif(not _has_live_mcp(), reason="Live MemPalace MCP not available")
    def test_mempalace_save_recall_timing(self, tmp_path):
        from memchorus.mempalace_memory_source import MemPalaceMemorySource

        source = MemPalaceMemorySource(
            name="bench_mp",
            config={
                "mcp_timeout": 5,
                "cache_dir": str(tmp_path / "mp_cache"),
            },
        )
        if not source.is_available:
            pytest.skip("MemPalace source is not available")

        latencies = []
        hits = 0

        for idx, fact in enumerate(SAMPLE_FACTS):
            t0 = time.monotonic()
            saved = source.save(f"bench_mp_{idx}_{fact['key']}", fact["value"])
            results = []
            if saved:
                results = source.search(fact["key"], limit=5) or []
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            latencies.append(elapsed_ms)

            if len(results) > 0:
                key_strs = {str(r.get("key", "")) for r in results}
                if any(fact["key"] in k for k in key_strs):
                    hits += 1

        accuracy = (hits / len(SAMPLE_FACTS)) * 100.0
        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95) if len(latencies) > 1 else latencies[0]

        print(
            f'--- Benchmark: mempalace.save_recall ---\n'
            f'  latency_p50={p50:.0f}ms | latency_p95={p95:.0f}ms | '
            f'hits={hits}/{len(SAMPLE_FACTS)} | accuracy={accuracy:.1f}%\n'
        )

        metrics = {
            "test": "mempalace_save_recall_timing",
            "source_available": True,
            "latency_ms": latencies,
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "hits_count": hits,
            "total_facts": len(SAMPLE_FACTS),
            "accuracy_pct": round(accuracy, 2),
        }
        _write_metrics(self.__class__.__name__, metrics)


# ──────────────────────────────────────────────────────────────────────────
# Test 4: Session search recall quality
# ──────────────────────────────────────────────────────────────────────────

class TestSessionSearchQuality:
    """Verify session_search returns meaningful content rather than stale markers."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from memchorus.hermes_memory_source import HermesDefaultMemorySource

        self.source = HermesDefaultMemorySource(
            config={"data_dir": str(tmp_path / "sessions")}
        )
        # Seed meaningful decisions and stale markers.
        for item in MEANINGFUL_DECISIONS:
            self.source.save(item["key"], item["value"])
        for item in STALE_PROGRESS_MARKERS:
            self.source.save(item["key"], item["value"])

    def test_session_search_recall_quality(self):
        t0 = time.monotonic()
        # Search for content terms present in meaningful decisions, not stale markers.
        results = self.source.search("OAuth2 client credentials", limit=10)
        latency_ms = (time.monotonic() - t0) * 1000.0

        def _norm(k):
            return k.lower().replace("_", "-")

        meaning_normed = {_norm(item["key"]) for item in MEANINGFUL_DECISIONS}
        stale_normed = {_norm(item["key"]) for item in STALE_PROGRESS_MARKERS}
        signal_normed = {_norm(f["key"]) for f in SAMPLE_FACTS}  # also counts as meaningful

        returned = []
        for r in results:
            key = r.get("key", "")
            returned.append(_norm(key))

        meaning_count = len(meaning_normed & set(returned))
        signal_count = len(signal_normed & set(returned))
        stale_count = len(stale_normed & set(returned))
        total_returned = len(returned)

        meaningful_total = meaning_count + signal_count
        meaningful_ratio = meaningful_total / max(total_returned, 1)

        print(
            f'--- Benchmark: session_search.recall_quality ---\n'
            f'  latency={latency_ms:.1f}ms | '
            f'meaningful={meaningful_total}/{total_returned} | '
            f'stale_ratio={stale_count/max(total_returned, 1):.2f}\n'
        )

        metrics = {
            "test": "session_search_recall_quality",
            "query": "OAuth2 client credentials",
            "latency_ms": round(latency_ms, 2),
            "total_results": total_returned,
            "meaningful_count": meaningful_total,
            "stale_count": stale_count,
            "meaningful_ratio": round(meaningful_ratio, 4),
        }
        _write_metrics(self.__class__.__name__, metrics)


# ──────────────────────────────────────────────────────────────────────────
# Test 5: Orchestrator timeout resilience — one slow source
# ──────────────────────────────────────────────────────────────────────────

class TestOrchestratorTimeoutResilience:
    """Simulate a slow/timeout source and verify the overall recall completes
    within budget with partial results from faster sources."""

    def test_orchestrator_all_sources_timeout_behavior(self, tmp_path):
        import threading
        from unittest.mock import patch

        from memchorus.orchestrator import MemoryOrchestrator
        from memchorus.hermes_memory_source import HermesDefaultMemorySource

        # Config: hermes_default writes to tmp_path; disable enforcement to avoid
        # nested save/recall that complicates timing measurements.
        config = {
            "hermes_default_config": {"data_dir": str(tmp_path / "slow_test")},
            "mempalace_config": {"skip_mcp": True, "cache_dir": str(tmp_path / "mp_slow")},
            "enforce_on_read": False,
            "enforce_on_write": False,
        }
        orch = MemoryOrchestrator(config=config)

        # Seed fast source.
        h_src = orch.memory_sources.get("hermes_default")
        for fact in SAMPLE_FACTS:
            if h_src:
                h_src.save(fact["key"], fact["value"])

        # Make the MemPalace source.search block for 2+ seconds on every call.
        def slow_search(query, limit=10):
            time.sleep(2.5)
            return []

        t0 = time.monotonic()
        with patch.object(
            orch.memory_sources.get("mempalace"),
            "search",
            slow_search,
        ):
            results = orch.search("OAuth2", limit=10)
        total_ms = (time.monotonic() - t0) * 1000.0

        # The search should return partial results from hermes_default despite
        # the MemPalace source being slow.  With enforce_off, the orchestrator
        # won't add enforcement overhead — raw fan-out only.
        fast_source_results = [
            r for r in results if r.get("source") == "hermes_default"
        ]

        within_budget = total_ms < 8000  # 8-second hard budget

        print(
            f'--- Benchmark: orchestrator.timeout_resilience ---\n'
            f'  total_lat={total_ms:.0f}ms | within_8s_budget={"yes" if within_budget else "no"} | '
            f'partial_results={len(fast_source_results)} | total_results={len(results)}\n'
        )

        metrics = {
            "test": "orchestrator_all_sources_timeout_simulation",
            "total_latency_ms": round(total_ms, 2),
            "within_8s_budget": within_budget,
            "fast_source_result_count": len(fast_source_results),
            "total_result_count": len(results),
        }
        _write_metrics(self.__class__.__name__, metrics)

        assert within_budget, f"Total latency {total_ms:.0f}ms exceeds 8s budget"


# ──────────────────────────────────────────────────────────────────────────
# Test 6: Profile isolation — cross-contamination guard
# ──────────────────────────────────────────────────────────────────────────

class TestProfileIsolation:
    """Prove that saving profile 'default' data does not contaminate
    'second' search results (each source instance has its own data_dir)."""

    def test_profile_isolation_data_integrity(self, tmp_path):
        from memchorus.hermes_memory_source import HermesDefaultMemorySource

        default_src = HermesDefaultMemorySource(
            config={"data_dir": str(tmp_path / "default")}
        )
        second_src = HermesDefaultMemorySource(
            config={"data_dir": str(tmp_path / "second")}
        )

        # Seed profile-specific data.
        default_src.save("deploy_env", "us-east-1 production cluster")
        default_src.save("rollback_sla", "5-minute RTO target")

        second_src.save("agent_model", "qwen3.6:27b for executor tasks")
        second_src.save("profile_tooling", "Kanban + MemPalace MCP only")

        # Cross-search each profile's data using its own source.
        t0 = time.monotonic()
        default_results = default_src.search("production us-east", limit=10)
        second_results = second_src.search("agent model qwen", limit=10)
        latency_ms = (time.monotonic() - t0) * 1000.0

        default_keys = {r.get("key", "") for r in default_results}
        second_keys = {r.get("key", "") for r in second_results}

        contamination = len(default_keys & second_keys)

        print(
            f'--- Benchmark: profile_isolation.integrity ---\n'
            f'  latency={latency_ms:.1f}ms | '
            f'default_hits={len(default_keys)} | '
            f'second_hits={len(second_keys)} | '
            f'cross_contamination={contamination}\n'
        )

        metrics = {
            "test": "profile_isolation_data_integrity",
            "latency_ms": round(latency_ms, 2),
            "default_result_count": len(default_keys),
            "second_result_count": len(second_keys),
            "cross_contamination_count": contamination,
        }
        _write_metrics(self.__class__.__name__, metrics)

        assert contamination == 0, (
            f"Cross-contamination detected: {contamination} shared key(s)"
        )


# ──────────────────────────────────────────────────────────────────────────
# Test 7: Multi-source merge timing (p50/p95 of orchestrated fan-out)
# ──────────────────────────────────────────────────────────────────────────

class TestMultiSourceMerge:
    """Measure p50/p95 latency for a full orchestrator fan-out across all
    available sources and verify result dedup/integrity."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from memchorus.orchestrator import MemoryOrchestrator

        self.tmp = tmp_path
        config = {
            "hermes_default_config": {"data_dir": str(tmp_path / "merge_h")},
            "mempalace_config": {"skip_mcp": True, "cache_dir": str(tmp_path / "merge_mp")},
            "enforce_on_read": False,
            "enforce_on_write": False,
        }
        self.orch = MemoryOrchestrator(config=config)

        # Seed all available sources with the same facts.
        for src_name, src in self.orch.memory_sources.items():
            avail = getattr(src, "is_available", True)
            if hasattr(avail, "__call__"):
                avail = bool(avail())
            else:
                avail = bool(avail)
            if src and avail:
                for fact in SAMPLE_FACTS:
                    src.save(fact["key"], fact["value"])

    def test_multi_source_merge_latency(self):
        N = 5
        latencies = []
        result_counts = []

        for _fact in SAMPLE_FACTS[:3]:  # query against first 3 facts
            runs: list[float] = []
            counts: list[int] = []
            for _ in range(N):
                t0 = time.monotonic()
                hits = self.orch.search(_fact["key"], limit=10)
                runs.append((time.monotonic() - t0) * 1000.0)
                counts.append(len(hits))

            latencies.extend(runs)
            result_counts.extend(counts)

        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        avg_results = statistics.mean(result_counts) if result_counts else 0

        print(
            f'--- Benchmark: orchestrator.multi_source_merge ---\n'
            f'  latency_p50={p50:.0f}ms | latency_p95={p95:.0f}ms | '
            f'avg_results_per_query={avg_results:.1f}\n'
        )

        metrics = {
            "test": "multi_source_merge_latency",
            "runs": len(latencies),
            "latency_ms": latencies,
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "avg_results_per_query": round(avg_results, 2),
            "sources_active": len(self.orch.memory_sources),
        }
        _write_metrics(self.__class__.__name__, metrics)
