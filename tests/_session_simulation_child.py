#!/usr/bin/env python3
"""Child worker for test_session_simulation.py subprocess tests.

Receives env vars, runs the requested operation against a shared on-disk
memory store directory, and outputs structured JSON to stdout."""

import json
import os
import sys
import tempfile


# ---------------------------------------------------------------------------
def _make_orch(store_dir: str):
    """Create an orchestrator with a hermes_default source pointing at *store_dir*."""
    from memchorus.orchestrator import MemoryOrchestrator
    from memchorus.hermes_memory_source import HermesDefaultMemorySource

    orch = MemoryOrchestrator({
        "default_source": "hermes_default",
        "enforce_on_read": False,
        "enforce_on_write": False,
    })
    orch.register_source(
        HermesDefaultMemorySource(name="hermes_default", config={"memory_dir": store_dir}),
    )
    return orch


# ---------------------------------------------------------------------------
def _run_store(store_dir: str, payloads: list) -> dict:
    """Store items directly via orchestrator.save() in this process."""
    orch = _make_orch(store_dir)

    results = []
    for payload in payloads:
        if isinstance(payload, dict):
            key = payload["key"]
            content = payload["content"]
        else:
            key = f"sim_{hash(payload)}"
            content = str(payload)
        saved = orch.save(key, content)
        results.append({"key": key, "saved_ok": bool(saved)})

    return {"ok": True, "count": len(results), "results": results}


# ---------------------------------------------------------------------------
def _run_recall(store_dir: str, keys: list) -> dict:
    """Recall items via content-based search and verify integrity."""
    orch = _make_orch(store_dir)

    out = []
    for key in keys:
        hits = []
        # Search broader — content terms + key itself — since orchestrator.search()
        # indexes by content similarity, not by key exact match.
        items = orch.search(key, limit=50)
        for it in items:
            item_key = it.get("key") or ""
            item_content = str(it.get("content", it.get("value", "")))
            # Match if the stored key appears anywhere in the result tuple
            if key.lower() in item_key.lower() or key.lower() in item_content.lower():
                hits.append({
                    "key": item_key,
                    "content": item_content[:500],
                    "score": it.get("score", 0.0),
                })
        out.append({"search_key": key, "found_count": len(hits), "hits": hits})

    matched = sum(1 for r in out if len(r["hits"]) > 0)
    return {"ok": True, "matched_count": matched, "results": out}


# ---------------------------------------------------------------------------
def _run_hook_simulation(store_dir: str) -> dict:
    """Test the full behavioral chain: detection → noise filtering → save → recall.

    Uses AutoStorageEngine + orchestrator directly because MemChorusHooks relies
    on auto_bootstrap() which tries to initialize MCP servers that don't exist in
    subprocess isolation. The behavior pipeline being tested is identical:
      1. BehavioralTrigger detects decision points in text
      2. Noise patterns reject error boilerplate before save
      3. AutoStorageEngine.write() persists content via orchestrator
      4. Orchestrator.search() recalls from disk"""

    orch = _make_orch(store_dir)

    from memchorus.auto_storage_engine import AutoStorageEngine
    from memchorus.behavioral_trigger import BehavioralTrigger
    from memchorus.auto_storage_engine import _is_noise, _detect_significance

    engine = AutoStorageEngine(orchestrator=orch, min_content_length=30)
    btrigger = BehavioralTrigger()

    turns = []

    # --------------------------------------------------------------- Turn 1 ---------------------------------------------------------------
    # Store starts empty — verify no artifacts exist yet
    initial_files = [f for f in os.listdir(store_dir) if f.endswith(".json")]
    turns.append({
        "turn": 1,
        "phase": "init_empty_store",
        "artifact_count": len(initial_files),
    })

    # --------------------------------------------------------------- Turn 2 ---------------------------------------------------------------
    # Meaningful learning statement — should pass all filters and save to disk
    text_learn = "I learned that the migration requires checking for incompatible data types first"
    bt_result1 = btrigger.detect(text_learn)
    sig1 = _detect_significance(text_learn)
    noise1 = _is_noise(text_learn)

    cap1 = engine.capture_outcome(text_learn)
    turns.append({
        "turn": 2,
        "phase": "capture_learning_statement",
        "behavioral_detected": len(bt_result1) > 0,
        "significance_categories": [s.value for s in sig1],
        "is_noise": noise1,
        "saved": cap1["saved"],
        "saved_key": cap1.get("key", ""),
    })

    # --------------------------------------------------------------- Turn 3 ---------------------------------------------------------------
    # Error-only output — should be rejected by noise filter (not saved)
    text_error = "Error: command not found: pg_dump"
    bt_result2 = btrigger.detect(text_error)
    noise2 = _is_noise(text_error)
    cap2 = engine.capture_outcome(text_error)

    turns.append({
        "turn": 3,
        "phase": "reject_error_output",
        "behavioral_detected": len(bt_result2) > 0,
        "is_noise": noise2,
        "saved": cap2["saved"],
        "reason": cap2.get("reason", ""),
    })

    # --------------------------------------------------------------- Turn 4 ---------------------------------------------------------------
    # Decision point — should pass detection + save via significance keywords
    text_decision = "I decided to use pgloader for the actual data transfer instead of manual migration"
    bt_result3 = btrigger.detect(text_decision)
    sig3 = _detect_significance(text_decision)
    noise3 = _is_noise(text_decision)
    cap3 = engine.capture_outcome(text_decision)

    turns.append({
        "turn": 4,
        "phase": "capture_decision_statement",
        "behavioral_detected": len(bt_result3) > 0,
        "significance_categories": [s.value for s in sig3],
        "is_noise": noise3,
        "saved": cap3["saved"],
        "saved_key": cap3.get("key", ""),
    })

    # --------------------------------------------------------------- Turn 5 ---------------------------------------------------------------
    # Verify disk persistence — JSON files should exist on disk now
    post_files = [f for f in os.listdir(store_dir) if f.endswith(".json")]
    turns.append({
        "turn": 5,
        "phase": "verify_disk_persistence",
        "artifact_count": len(post_files),
        "initial_artifact_count": len(initial_files),
        "new_artifacts": len(post_files) - len(initial_files),
    })

    # --------------------------------------------------------------- Turn 6 ---------------------------------------------------------------
    # Recall test — fresh orchestrator should find what engine stored in turns 2 & 4
    recall_orch = _make_orch(store_dir)
    recall_items = recall_orch.search("migration", limit=20)

    # Check which items contain expected content fragments
    has_learn_mention = any("learn" in str(i.get("content", i.get("text", ""))).lower()
                            for i in recall_items)
    has_decisions = any("decid" in str(i.get("content", i.get("text", ""))).lower()
                        for i in recall_items)

    turns.append({
        "turn": 6,
        "phase": "recall_saved_content",
        "total_results": len(recall_items),
        "has_learning_recall": has_learn_mention,
        "has_decision_recall": has_decisions,
    })

    return {"ok": True, "turn_results": turns, "store_dir": store_dir}


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mode = os.environ.get("MODE", "")
    store_dir = os.environ.get("STORE_DIR", tempfile.mkdtemp())

    if mode == "store":
        payloads = json.loads(os.environ["PAYLOADS_JSON"])
        res = _run_store(store_dir, payloads)
    elif mode == "recall":
        keys_list = [k for k in os.environ.get("EXPECTED_IDS", "").split(",") if k]
        res = _run_recall(store_dir, keys_list)
    elif mode == "hook_simulation":
        res = _run_hook_simulation(store_dir)
    elif mode == "list_artifacts":
        """Just count JSON artifacts on disk — no imports needed."""
        count = sum(1 for f in os.listdir(store_dir) if f.endswith(".json"))
        res = {"artifact_count": count}
    else:
        res = {"ok": False, "error": f"Unknown mode: {mode}"}

    print(json.dumps(res))
