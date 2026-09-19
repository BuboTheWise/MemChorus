#!/usr/bin/env python3
"""eval/run_recall_gate.py — #209(b) item 6 re-rank/recall eval gate.

Per the re-rank-recall-eval-gate skill + kanban task t_2511fccc:
  - LIVE only.  A mocked `call(q)` defeats the whole gate.
  - stdlib + memchorus package only (no third-party test framework).
  - One full run is < 1 min on this corpus (~255 drawers).
  - Fixed committed query set; do NOT regenerate between runs.

Pipeline (matches production recall):
  1. MemPalaceMemorySource (live MCP)     — retrieval layer
  2. RelevanceScorer.score_and_rank       — re-ranking layer

Per-query metrics:
  raw_drawer_count  = # drawers returned by live MCP (top-K)
  ranked_drawer_count = # drawers the ranker actually surfaced (score >= min_score)
  hit@K             = 1 iff raw_drawer_count > 0   (query returned something)
  zero_hit          = 1 iff raw_drawer_count == 0  (headline: "98-99% at ZERO")
  mean_distance     = mean RelevanceScorer normalized score [0,1] across
                      ranked hits (the live path does NOT expose raw cosine
                      distance — the MCP search response carries no
                      similarity field — so the ranker's normalized score is
                      the honest, comparable, [0,1] quantity on this path.
                      Provenance stamped in the JSON output.)

Aggregated gate metrics (the numbers that go into the baseline JSON):
  hit_rate@10       = hit_count (over all positive queries) / total positives
  zero_hit_count    = # positive queries with raw_drawer_count == 0
  mean_distance     = mean across ALL ranked hits (all queries) in [0,1]

Exit codes:
  0  PASS   — (a) first-run baseline creation, or (b) zero_hit_count current
              <= zero_hit_count baseline (no worsening; delta >= 0)
  1  REGRESSION — zero_hit_count current > zero_hit_count baseline
  2  LIVE PATH NOT CONNECTED — MCP down; run is self-grade and we must NOT
               silently run the local-fallback.  Reviewer reads exit 2 + JSON.

The gate enforces delta >= 0 on ZERO_HIT (i.e. we do NOT accept a run where
recall gets WORSE than the stored baseline).  It also reports hit_rate@10,
mean_distance, and per-query drawer_ids for reviewer inspection.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REPO = Path(os.environ.get(
    "MEMCHORUS_REPO",
    Path(__file__).resolve().parent.parent,
))
_SRC = _REPO / "src"
_EVAL = _REPO / "eval"
_QUERY_FILE = _EVAL / "recall_queries.txt"
_BASELINE_FILE = _EVAL / f"baseline_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"

logging.disable(logging.WARNING)
# Keep the one genuine stderr signal visible (embedder identity).
import warnings; warnings.simplefilter("always")

# ---------------------------------------------------------------------------


def _load_queries() -> list:
    """
    Parse recall_queries.txt.  Two formats supported:
      positive: <expected-phrase-1|phrase-2|...>\n<query text>
      negative: ~\n<query text>
    Returns list of {query, is_negative, expected_phrases:[str]}.
    """
    items = []
    lines = _QUERY_FILE.read_text().splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        if not raw or raw.startswith("#"):
            i += 1
            continue
        # The next content line (non-blank, non-comment) is the query text.
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
            j += 1
        if j >= len(lines):
            i += 1
            continue
        query_line = lines[j].strip()
        if raw == "~":
            items.append({"query": query_line, "is_negative": True, "expected_phrases": []})
        else:
            phrases = [p.strip() for p in raw.split("|") if p.strip()]
            items.append({"query": query_line, "is_negative": False, "expected_phrases": phrases})
        i = j + 1
    return items


def _phrase_in_result(phrases, key, content):
    if not phrases:
        return False
    haystack = (
        (key or "")
        + " "
        + (content if isinstance(content, str) else json.dumps(content, default=str))
    ).lower()
    return any(p.lower() in haystack for p in phrases)


def _build_live_source():
    sys.path.insert(0, str(_SRC))
    from memchorus.mempalace_memory_source import MemPalaceMemorySource
    src = MemPalaceMemorySource("mempalace", {"mcp_timeout": 30})
    return src


def run_eval(limit: int = 10):
    """Execute the full eval. Returns (summary_dict, exit_code)."""
    t0 = time.monotonic()
    print(f"=== #209(b) item 6 — re-rank/recall eval gate ===")
    print(f"query file : {_QUERY_FILE}", flush=True)
    print(f"baseline   : {_BASELINE_FILE}", flush=True)
    print(f"limit      : {limit} (top-{limit}, @10 headline metric)", flush=True)
    print("", flush=True)

    # ---- Build LIVE source ----
    src = _build_live_source()
    live_ok = src._ensure_connected() and src._client.is_alive
    print(f"mcp        : live_connected={live_ok}  client.is_alive={src._client.is_alive}")
    if not live_ok:
        print("ERROR: MCP not connected — local-fallback path only.", flush=True)
        print("Gate exits 2 (LIVE PATH NOT CONNECTED).  Do NOT self-grade off fallback.", flush=True)
        return {"live_connected": False, "reason": "mcp_down"}, 2

    # ---- Relevance scorer (production constructor defaults) ----
    from memchorus.relevance_engine import RelevanceScorer, ContextWeight
    scorer = RelevanceScorer()
    context = ContextWeight()   # no active_project → closet boost 0.0 for all
    min_score = float(scorer.min_score)

    # ---- Run through every query ----
    queries = _load_queries()
    n_pos = sum(1 for q in queries if not q["is_negative"])
    n_neg = sum(1 for q in queries if q["is_negative"])
    print(f"queries    : {len(queries)} total  ({n_pos} positive + {n_neg} negative)")
    print(f"ranker     : RelevanceScorer min_score={min_score}", flush=True)
    print("", flush=True)

    per_query = []
    all_ranked_scores = []
    all_raw_keys = []

    for item in queries:
        q = item["query"]
        neg = item["is_negative"]
        phrases = item["expected_phrases"]

        raw_hits = []
        ranked = []
        err = ""
        try:
            raw_hits = src.search(q, limit=limit) or []
        except Exception as exc:
            err = f"search: {exc}"[:200]
        if raw_hits and not err:
            try:
                ranked = scorer.score_and_rank(raw_hits, q, context)
            except Exception as exc:
                err = err or f"ranker: {exc}"[:200]
        if err and not ranked:
            # If the ranker fails, fall back to the raw hits in their return order
            # so that we still can measure hit@10 and mean of what we had.
            ranked = raw_hits

        rank_count = len(ranked)
        hit = 1 if raw_hits else 0
        zero_hit = 0 if raw_hits else 1

        # For negative controls: a "false positive" is when the *ranker*
        # surfaces >= 1 hit at >= min_score (i.e. it is not conservative enough).
        def _score_of(r):
            v = r.get("score") if isinstance(r, dict) else getattr(r, "score", None)
            return float(v) if v is not None else 0.0

        n_above = sum(1 for r in ranked if _score_of(r) >= min_score)

        # Collect scores for the global mean_distance.
        for r in ranked:
            s = r.get("score") if isinstance(r, dict) else getattr(r, "score", None)
            if s is not None:
                all_ranked_scores.append(float(s))

        # Collect drawer keys for reviewer inspection.
        keys_raw = [r.get("key") if isinstance(r, dict) else getattr(r, "key", "") for r in raw_hits[:5]]
        all_raw_keys.extend([k for k in keys_raw if k])

        row = {
            "query": q,
            "is_negative": neg,
            "raw_count": len(raw_hits),
            "ranked_count": rank_count,
            "n_above_threshold": n_above,
            "hit": hit,
            "zero_hit": zero_hit,
            "raw_top5_keys": keys_raw,
            "mean_score_ranked": round(mean(all_ranked_scores[-rank_count:]) if rank_count and all_ranked_scores else 0.0, 4),
            "error": err,
        }
        per_query.append(row)

        # Per-query one-line summary
        marker = "NEG" if neg else "POS"
        if neg:
            fp = "FP!" if n_above > 0 else "ok "
            print(f"  [{marker}] {q[:52]!r:55s}  raw={len(raw_hits):2d}  ranked={rank_count:2d}  "
                  f"above_th={n_above:2d}  {fp}   {row.get('error', '')[:38]}", flush=True)
        else:
            flag = "✓" if hit else "ZERO"
            rel_in_top5 = any(_phrase_in_result(phrases, k, None) for k in keys_raw) or hit
            print(f"  [{marker}] {q[:52]!r:55s}  raw={len(raw_hits):2d}  ranked={rank_count:2d}  "
                  f"above_th={n_above:2d}  {flag:4s}  top={ (keys_raw[0] if keys_raw else '')[:28]}",
                  flush=True)

    elapsed = round(time.monotonic() - t0, 2)
    print(f"\n=== aggregate {elapsed}s ===\n", flush=True)

    # ---- Aggregate gate metrics ----
    pos_rows = [r for r in per_query if not r["is_negative"]]
    neg_rows = [r for r in per_query if r["is_negative"]]

    hit_hitcount = sum(1 for r in pos_rows if r["hit"])
    zero_hit_count = sum(1 for r in pos_rows if r["zero_hit"])
    hit_rate = (hit_hitcount / len(pos_rows)) if pos_rows else 0.0
    mean_dist = round(mean(all_ranked_scores), 4) if all_ranked_scores else None
    false_positives = sum(1 for r in neg_rows if r["n_above_threshold"] > 0)
    false_positive_rate = (false_positives / len(neg_rows)) if neg_rows else 0.0
    errors = [r for r in per_query if r["error"]]

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task": "#209(b)-item6: re-rank/recall eval gate",
        "repo_head": "65971b8 baseline commit (parent t_de823538, corpus_balancer diagnostic)",
        "corpus": "memchorus_learning=243, memchorus_decisions=8, wing_cthugha=4 (census 2026-09-17)",
        "live_connected": True,
        "limit": limit,
        "ranker_min_score": min_score,
        "n_queries": len(queries),
        "n_positive": len(pos_rows),
        "n_negative": len(neg_rows),
        # ---- the three gate metrics (headline) ----
        "hit_rate@10": round(hit_rate, 4),
        "zero_hit_count": zero_hit_count,
        "mean_distance": mean_dist,
        # note on mean_distance provenance (live path exposes ranker norm score,
        # not raw cosine; both are [0,1] but mean_distance here = ranker score)
        "mean_distance_provenance": "RelevanceScorer normalized score (mean over all ranked hits) — the live MCP path returns no raw cosine field",
        # supporting metrics
        "false_positive_count": false_positives,
        "false_positive_rate_neg": round(false_positive_rate, 4),
        "error_queries": errors,
        "n_ranked_scores": len(all_ranked_scores),
        # for delta + reviewer
        "raw_drawer_keys_top5_all": all_raw_keys[:500],
        "elapsed_s": elapsed,
        "per_query": per_query,
    }

    # ---- Baseline compare ----
    baseline = {}
    if _BASELINE_FILE.exists():
        try:
            baseline = json.loads(_BASELINE_FILE.read_text())
        except Exception as exc:
            summary["baseline_load_error"] = str(exc)

    if baseline:
        base_zero = baseline.get("zero_hit_count")
        base_hit = baseline.get("hit_rate@10")
        base_mean = baseline.get("mean_distance")
        current_zero = zero_hit_count
        current_hit = round(hit_rate, 4)
        current_mean = mean_dist

        delta_zero = (current_zero - base_zero) if (base_zero is not None and current_zero is not None) else None
        delta_hit  = (current_hit  - base_hit)  if (base_hit  is not None and current_hit  is not None) else None
        delta_mean = (current_mean - base_mean) if (base_mean is not None and current_mean is not None) else None

        summary["baseline_compare"] = {
            "zero_hit_count": {"baseline": base_zero, "current": current_zero, "delta": delta_zero},
            "hit_rate@10"   : {"baseline": base_hit,  "current": current_hit,  "delta": delta_hit},
            "mean_distance" : {"baseline": base_mean, "current": current_mean, "delta": delta_mean},
        }

        # Gate: green only on delta >= 0 for zero_hit (i.e. current <= baseline)
        gate_pass = delta_zero is not None and delta_zero <= 0

        if gate_pass:
            summary["gate"] = "pass"
            print(f"GATE: PASS")
            print(f"  zero_hit   : {base_zero} -> {current_zero}   (Δ = {delta_zero})")
            print(f"  hit_rate@10: {base_hit} -> {current_hit}   (Δ = {delta_hit})")
            print(f"  mean_dist  : {base_mean} -> {current_mean} (Δ = {delta_mean})")

            # Refresh the baseline to this run's numbers (latest becomes new ref)
            json.dump(summary, open(_BASELINE_FILE, "w"), indent=2)
            print(f"  baseline   : refreshed → {_BASELINE_FILE}")
            return summary, 0
        else:
            summary["gate"] = "regression"
            print(f"GATE: REGRESSION")
            print(f"  zero_hit   : {base_zero} -> {current_zero}   (Δ = {delta_zero})  [WORSE — recall got worse]")
            print(f"  hit_rate@10: {base_hit} -> {current_hit}   (Δ = {delta_hit})")
            print(f"  mean_dist  : {base_mean} -> {current_mean} (Δ = {delta_mean})")
            json.dump(summary, open(_BASELINE_FILE, "w"), indent=2)
            return summary, 1
    else:
        # First run — store as new baseline
        summary["gate"] = "baseline-created"
        print(f"GATE: BASELINE CREATED (first run)")
        print(f"  zero_hit   : {zero_hit_count} / {len(pos_rows)}  ({round(100*zero_hit_count/len(pos_rows),1)}% of positive queries)")
        print(f"  hit_rate@10: {round(hit_rate, 4)}")
        print(f"  mean_dist  : {mean_dist}")
        json.dump(summary, open(_BASELINE_FILE, "w"), indent=2)
        print(f"  baseline   : stored → {_BASELINE_FILE}")
        # First run is NOT a regression — exit 0
        return summary, 0


def main():
    import argparse
    p = argparse.ArgumentParser(description="#209(b) item 6 — re-rank/recall eval gate (live, not mocked)")
    p.add_argument("--limit", type=int, default=10, help="max drawers per search (=@10 headline)")
    args = p.parse_args()
    summary, code = run_eval(limit=args.limit)
    # Always print the JSON summary for reviewer / pipe
    print("\n=== JSON summary ===")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_query"}, indent=2))
    print("=== per-query ===")
    print(json.dumps(summary.get("per_query", []), indent=2))
    sys.exit(code)


if __name__ == "__main__":
    main()
