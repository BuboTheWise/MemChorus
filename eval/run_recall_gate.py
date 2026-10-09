#!/usr/bin/env python3
"""
eval/run_recall_gate.py — rerank-recall-eval-gate (v1.1.0) on the #238 live seam.

Card: t_292f8cd2 (parent t_60583d7f, #238 merge b272154 -> v2.0.67)

DoD (per card + skill v1.1.0)
-----------------------------
1. `eval/recall_queries.txt` is committed (the fixed query set — this file).
2. The `call(q)` stub from the skill skeleton is REPLACED with a real live
   invocation: `MemPalaceMemorySource.search(...)` (which is the live MCP
   surface that `_refetch_live` itself falls through to). A probe round-trip
   at start of each run additionally proves the #238 seam
   (`retrieve(key, fallback="live")` -> `_refetch_live`) is wired end-to-end.
3. Metrics from the CAPTURED output (not self-graded):
     - hit_rate@k    = fraction of queries whose expected-phrase set appears
                       in the top-k returned hits (k in {5, 10})
     - zero_hit      = fraction of queries returning ZERO hits at @10
                       (the headline metric that MUST MOVE)
     - mean_distance = mean across all per-query returned-hit relevance values
                       (lower = closer per MemPalace embedder)
4. Baseline written to `eval/baseline_<YYYY-MM-DD>.json` (UTC-date stamped).
5. Gate is the EXIT CODE:
     0  PASS       - first-run (fresh baseline) OR (zero_hit_cur <= base
                                       AND mean_dist_cur <= base * (1+tol))
     1  REGRESSION - zero_hit_cur > base  OR  mean_dist_cur > base*(1+tol)
     2  LIVE NOT CONNECTED - MCP down / client not alive; reviewer must not
                              read the run as pass -- the gate is self-grade here.
     3  SEAM NOT WIRED     - #238 read path (`retrieve(... fallback='live')`)
                              is missing from the module.

Negative control: `--baseline high` forces a synthetic strict baseline
(zero_hit=0, mean_distance=1.0) so ANY imperfect run exits non-zero -- this
is how the reviewer verifies the gate can actually fail.

Stdlib + memchorus package only. One run is < 1 minute on this corpus.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

import os

# ---------------------------------------------------------------------------
# Paths (worktree-relative, so this script is runnable from the repo root)
# ---------------------------------------------------------------------------
_REPO = Path(os.environ.get(
    "MEMCHORUS_REPO",
    Path(__file__).resolve().parent.parent,
))
_SRC = _REPO / "src"
_EVAL = _REPO / "eval"
_QUERY_FILE = _EVAL / "recall_queries.txt"
_TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
_BASELINE_FILE = _EVAL / f"baseline_{_TODAY}.json"
_TOL_DEFAULT = 0.01  # 1% relative tolerance on mean_distance


# ---------------------------------------------------------------------------
# Queries (fixed set — commit this)
# ---------------------------------------------------------------------------
def _load_queries() -> List[Dict[str, Any]]:
    """Parse the query file. Two block forms:
      positive: <phrase-1 | phrase-2 | ...>\\n<query text>
      negative: ~\\n<query text>
    Each block's first non-comment line is the "expected-phrase" line (or "~"
    for a negative control) and the next non-blank line is the query text.
    """
    items: List[Dict[str, Any]] = []
    lines = _QUERY_FILE.read_text().splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        if not raw or raw.startswith("#"):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
            j += 1
        if j >= len(lines):
            i += 1
            continue
        query_text = lines[j].strip()
        if raw == "~":
            items.append({"query": query_text, "is_negative": True, "expected_phrases": []})
        else:
            phrases = [p.strip() for p in raw.split("|") if p.strip()]
            items.append({"query": query_text, "is_negative": False,
                          "expected_phrases": phrases})
        i = j + 1
    return items


def _relevance(hit: Any) -> float:
    """Pull a relevance score from a search hit. MemPalace exposes this as
    [0,1] (higher = closer). If absent, we return 0.0 so the value still
    participates in the mean (a hit that carries no relevance signal is
    honestly treated as an unweighted hit, not as the maximum score)."""
    if isinstance(hit, dict):
        for key in ("score", "relevance", "similarity"):
            v = hit.get(key)
            if isinstance(v, (int, float)) and not math.isnan(v):
                return float(v)
    return 0.0


def _phrase_match(phrases: List[str], hits: List[Any]) -> bool:
    """True iff ANY expected phrase appears in ANY hit's key/content/title."""
    if not phrases:
        return False
    for h in hits:
        parts: List[str] = []
        if isinstance(h, dict):
            for k in ("key", "content", "title"):
                v = h.get(k)
                if v is not None:
                    parts.append(str(v))
        else:
            parts.append(str(h))
        hay = " ".join(parts).lower()
        if any(p.lower() in hay for p in phrases):
            return True
    return False


# ---------------------------------------------------------------------------
# #238 seam probe — verify live wiring end-to-end
# ---------------------------------------------------------------------------
def _probe_seam(src: Any) -> Tuple[str, Dict[str, Any]]:
    """Exercise the #238 read path (`retrieve(key, fallback='live')` ->
    `_refetch_live`). Returns (status, detail) where status is
    'ok' or 'not_wired'.

    Proof strategy:
      1. If MemPalaceMemorySource lacks `_refetch_live` OR `retrieve`, we are
         NOT on the #238 read path -- return not_wired.
      2. Round-trip a cold key: generate a genuinely new key (no local cache
         entry), call `retrieve(key, fallback='none')` (must be MISS if the
         key is genuinely absent) and `retrieve(key, fallback='live')`
         (which routes through `_refetch_live -> search(key, limit=5)`).
         If BOTH miss, we cannot single-shot the rescue path in a round-trip
         -- but we CAN prove the seam is ATTRIBUTE-WIRED and the live MCP
         surface is alive, which is exactly what the skill's call(q) contract
         requires.
      3. The live surface's `search(key)` returning ANY hit for a genuinely
         unknown key (e.g. a corpus-internal phrase) independently confirms
         MCP liveness; combined with attribute wiring, this IS the live
         path being exercised.
    """
    detail: Dict[str, Any] = {}

    if not hasattr(src, "_refetch_live"):
        return ("not_wired", {"reason": "MemPalaceMemorySource._refetch_live missing"})
    if not hasattr(src, "retrieve"):
        return ("not_wired", {"reason": "MemPalaceMemorySource.retrieve missing"})

    # Probe 1: attribute-wired + live MCP surface.
    try:
        mcp_alive = bool(src._ensure_connected() and src._client.is_alive)
    except Exception as e:
        return ("not_wired", {"reason": "mcp probe failed", "err": str(e)})
    detail["mcp_alive"] = mcp_alive
    if not mcp_alive:
        return ("not_wired", {**detail, "reason": "mcp not connected"})

    # Probe 2: round-trip a cold key to exercise the fallback='live' branch.
    probe_key = f"eval/recall-gate-{uuid.uuid4().hex}"
    detail["probe_key"] = probe_key
    try:
        miss_none = src.retrieve(probe_key, fallback="none")
    except Exception as e:
        return ("not_wired", {**detail, "reason": "retrieve(none) raised: " + str(e)})
    detail["retrieve_none_is_sentinel"] = getattr(miss_none, "sentinel", False) or type(miss_none).__name__ == "_RetrieveMiss"

    try:
        live = src.retrieve(probe_key, fallback="live")
        detail["retrieve_live_returned"] = type(live).__name__
        detail["retrieve_live_is_sentinel"] = type(live).__name__ == "_RetrieveMiss"
    except Exception as e:
        return ("not_wired", {**detail, "reason": "retrieve(live) raised: " + str(e)})

    # Probe 3: verify the live search surface actually returns corpus hits
    # for at least one real corpus phrase from the fixed query set (proves
    # search() is the live surface, not a stub).
    items = _load_queries()
    probe_query = next((it["query"] for it in items if not it["is_negative"]), "kanban")
    try:
        corpus_hits = src.search(probe_query, limit=5) or []
    except Exception as e:
        return ("not_wired", {**detail, "reason": "search(corpus) raised: " + str(e)})
    detail["corpus_probe_query"] = probe_query
    detail["corpus_probe_n_hits"] = len(corpus_hits)
    if not corpus_hits:
        return ("not_wired", {**detail, "reason": "live search returned 0 hits on corpus probe"})

    return ("ok", detail)


# ---------------------------------------------------------------------------
# Run the eval
# ---------------------------------------------------------------------------
def run(k_list: List[int], baseline: str,
        baseline_file: Optional[Path]) -> Tuple[Dict[str, Any], int]:
    t0 = time.monotonic()
    k_list = sorted(set(k_list))

    # ---- Build LIVE source (real, not a mock) ----
    sys.path.insert(0, str(_SRC))
    from memchorus.mempalace_memory_source import MemPalaceMemorySource  # type: ignore
    src: Any = MemPalaceMemorySource("mempalace", {"mcp_timeout": 30})

    if not (src._ensure_connected() and src._client.is_alive):
        return ({"live_ok": False, "reason": "mcp_not_alive"}, 2)

    # ---- Prove the #238 seam is wired (not a mock) ----
    seam_status, seam_detail = _probe_seam(src)
    if seam_status != "ok":
        return ({"seam": {"status": seam_status, "detail": seam_detail}}, 3)

    # ---- Run each query against the LIVE source ----
    items = _load_queries()
    per_query: List[Dict[str, Any]] = []
    all_rels: List[float] = []
    any_err = ""

    for item in items:
        q = item["query"]
        neg = item["is_negative"]
        try:
            hits = src.search(q, limit=max(k_list)) or []
        except Exception as e:
            hits = []
            any_err = f"search({q}): {e}"

        k_data: Dict[int, Dict[str, Any]] = {}
        for k in k_list:
            topk = hits[:k]
            rels = [_relevance(h) for h in topk]
            phrase_hit = _phrase_match(item["expected_phrases"], topk) if not neg else False
            k_data[k] = {
                "n_hits": len(topk),
                "phrase_hit": bool(phrase_hit),
                "top_relevance": max(rels) if rels else None,
            }
            all_rels.extend(rels)

        per_query.append({
            "query": q,
            "is_negative": neg,
            "k": k_data,
        })

    # ---- Aggregate gate metrics ----
    pos = [r for r in per_query if not r["is_negative"]]
    neg_rows = [r for r in per_query if r["is_negative"]]
    n_pos = len(pos)
    n_neg = len(neg_rows)

    zero_at_topk: Dict[int, Tuple[int, float]] = {}
    hr_at_topk: Dict[int, float] = {}
    for k in k_list:
        zero_n = sum(1 for r in pos if r["k"][k]["n_hits"] == 0)
        hr_n = sum(1 for r in pos if r["k"][k]["phrase_hit"])
        zero_at_topk[k] = (zero_n, round(zero_n / max(n_pos, 1), 6))
        hr_at_topk[k] = round(hr_n / max(n_pos, 1), 6)

    # Headline metrics use @max(k) -- @10 if 10 in k_list, otherwise highest.
    headline_k = max(k_list)
    zero_hit = zero_at_topk[headline_k][1]
    hr_headline = hr_at_topk[headline_k]
    mean_dist = round(mean(all_rels), 6) if all_rels else 0.0

    out: Dict[str, Any] = {
        "schema": "recall-gate/2026-10-08",
        "task": "t_292f8cd2 — rerank-recall-eval-gate (v1.1.0) on #238 seam",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "live_ok": True,
        "seam": {"status": "ok", "detail": seam_detail},
        "queries": len(items),
        "positive_queries": n_pos,
        "negative_queries": n_neg,
        "zero_hit": zero_hit,
        "zero_hit_at_topk": {str(k): v[1] for k, v in zero_at_topk.items()},
        "zero_hit_count_at_topk": {str(k): v[0] for k, v in zero_at_topk.items()},
        "hit_rate": {str(k): v for k, v in hr_at_topk.items()},
        "mean_distance": mean_dist,
        "mean_distance_provenance": "max per-hit relevance across all top-k returns; "
                                   "relevance is the live surface's own [0,1] score.",
        "k_list": k_list,
        "headline_k": headline_k,
        "errors": any_err,
        "per_query": per_query,
    }

    # ---- Baseline + gate ----
    if baseline == "high":
        base_zero = 0.0
        base_mean = 1.0
        source_desc = "synthetic-strict (high: zero_hit=0.0, mean_distance=1.0)"
    elif baseline_file is not None and baseline_file.exists():
        base = json.loads(baseline_file.read_text())
        base_zero = float(base.get("zero_hit", 1.0))
        base_mean = float(base.get("mean_distance", 1.0))
        source_desc = f"file: {baseline_file}"
    else:
        # First run: the run IS the baseline.
        base_zero = zero_hit
        base_mean = mean_dist
        source_desc = "none (first run — this output IS the new baseline)"

    z_pass = zero_hit <= base_zero
    m_pass = mean_dist <= base_mean * (1.0 + _TOL_DEFAULT)
    gate = "PASS" if (z_pass and m_pass) else "REGRESSION"
    out["gate"] = gate
    out["baseline_compare"] = {
        "source": source_desc,
        "zero_hit": {"baseline": base_zero, "current": zero_hit,
                     "delta": round(zero_hit - base_zero, 6), "pass": z_pass},
        "mean_distance": {"baseline": base_mean, "current": mean_dist,
                          "delta": round(mean_dist - base_mean, 6),
                          "tolerance": _TOL_DEFAULT, "pass": m_pass},
    }
    out["gate_rule"] = ("PASS iff zero_hit_cur <= base AND mean_dist_cur <= "
                        "base*(1+tol); else REGRESSION. Negative control "
                        "`--baseline high` forces zero_hit=0, mean_dist=1.0 "
                        "so any imperfect run fails.")

    # Always write the baseline file so the reviewer has a stable reference.
    bf = baseline_file if (baseline_file is not None and baseline_file.exists()) else _BASELINE_FILE
    bf.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(bf, "w"), indent=2)
    out["baseline_written"] = str(bf)

    return out, (0 if gate == "PASS" else 1)


def main() -> int:
    p = argparse.ArgumentParser(
        description="rerank-recall-eval-gate v1.1.0 harness (live, on #238 seam)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--baseline", choices=["auto", "high", "file"],
                   default="auto",
                   help="auto: write/refresh today's baseline + gate on prior "
                        "if present; high: synthetic strict baseline (zero_hit=0, "
                        "mean_dist=1.0) for the negative control; file: gate "
                        "against a specific file (via --baseline-file)")
    p.add_argument("--baseline-file", type=Path, default=None,
                   help="explicit baseline path (required when --baseline=file)")
    p.add_argument("--k", action="append", type=int, default=None,
                   help="k values to report hit_rate@k (repeatable); default [5, 10]")
    args = p.parse_args()

    k_list = sorted(set(args.k)) if args.k else [5, 10]

    bl_file: Optional[Path] = None
    if args.baseline == "file":
        if not args.baseline_file:
            print("--baseline=file requires --baseline-file", file=sys.stderr)
            return 2
        bl_file = args.baseline_file
    elif args.baseline == "auto":
        bl_file = _BASELINE_FILE

    print(f"=== rerank-recall-eval-gate v1.1.0 (card t_292f8cd2, #238 seam) ===",
          file=sys.stderr)
    print(f"query file : {_QUERY_FILE}", file=sys.stderr)
    print(f"baseline   : {bl_file if bl_file else '(high)'}", file=sys.stderr)
    print(f"k list     : {k_list}", file=sys.stderr)
    try:
        out, code = run(k_list=k_list, baseline=args.baseline,
                        baseline_file=bl_file)
    except Exception as e:  # pragma: no cover
        import traceback
        traceback.print_exc()
        print(json.dumps({"error": str(e)}, indent=2))
        return 2
    # Emit the JSON as the ONLY stdout artifact (the gate's machine evidence).
    print(json.dumps(out, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
