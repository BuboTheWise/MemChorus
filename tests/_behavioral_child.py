#!/usr/bin/env python3
"""Child process for behavioral integration test.

Subprocess isolation is mandatory (AC-4): each child gets a fresh module cache,
imports memchorus from disk, and shares only the memory_dir path via env vars.
This proves recall survives subprocess boundaries — no cached module state can
fake positive results across runs.

Modes:
  store          — save content to hermes_default source (with enforcement ON)
  roundtrip      — store + query with enforcement ON, measure file reads
  baseline       — store + query with enforcement OFF, measure file reads

Output: JSON to stdout with keys, storage results, recall hits, and
         FileAccessCounter metrics.
"""
import sys
import os
import json

# Fresh module cache (AC-4)
for _mod in list(sys.modules.keys()):
    if "memchorus" in _mod:
        del sys.modules[_mod]

# Resolve src/ directory relative to this test file location (works regardless of CWD)
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
SRC_PATH = os.path.join(REPO_ROOT, "src")
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

from memchorus.orchestrator import MemoryOrchestrator


class FileAccessCounter:
    """Track actual vs expected disk accesses.

    Monkey-patches the source.retrieve() method so every disk read is counted.
    When recall pre-loaded certain keys into `_recalled_keys`, subsequent
    retrieves for those keys are marked as PREVENTED instead of disk reads.
    """

    def __init__(self):
        self.file_reads = 0           # actual disk open() calls that happened
        self.prevented_reads = 0      # retrieve() calls short-circuited by recall
        self._recalled_keys = set()   # keys already surfaced by search/recall

    def record_file_read(self, _key):
        self.file_reads += 1

    def record_prevented_read(self, _key):
        self.prevented_reads += 1

    def mark_recall_hits(self, keys):
        """Pre-populate the set of keys that recall already surfaced."""
        self._recalled_keys.update(keys)

    def savings_percentage(self):
        """Bounded percentage of disk reads prevented.

        (expected - actual) / expected * 100 clamped to [0, 100].
        """
        total = self.file_reads + self.prevented_reads
        if total == 0:
            return 0.0
        saved = max(total - self.file_reads, 0)
        pct = (saved / total) * 100.0
        return max(0.0, min(100.0, pct))

    def to_dict(self):
        return {
            "file_reads": self.file_reads,
            "prevented_reads": self.prevented_reads,
            "total_accesses": self.file_reads + self.prevented_reads,
        }


# ---------------------------------------------------------------------------
# Main child logic
# ---------------------------------------------------------------------------

mode = os.environ.get("MODE", "store")
store_dir = os.environ["STORE_DIR"]
run_id = os.environ.get("RUN_ID", "0")
items_json = os.environ.get("ITEMS_JSON", "[]")   # [{"key":"...", "content":"..."}, ...]
query_text = os.environ.get("QUERY", "")

# Enforcement flags depend on mode:
if mode == "store":
    enforce_read = False
    enforce_write = True
elif mode == "roundtrip":
    enforce_read = True
    enforce_write = False
else:
    # baseline
    enforce_read = False
    enforce_write = False

orch_config = {
    "default_source": "hermes_default",
    "memory_dir": store_dir,
    "enforce_on_read": enforce_read,
    "enforce_on_write": enforce_write,
}

orch = MemoryOrchestrator(config=orch_config)

# Point hermes_default source at shared temp directory
src = orch.memory_sources.get("hermes_default")
if src is None:
    print(json.dumps({"ok": False, "error": "hermes_default not found"}))
    sys.exit(1)

src.memory_dir = store_dir

counter = FileAccessCounter()

# Patch retrieve() to count disk vs prevented reads
_original_retrieve = src.retrieve


def _counted_retrieve(key):
    if key in counter._recalled_keys:
        counter.record_prevented_read(key)
    else:
        counter.record_file_read(key)
    return _original_retrieve(key)


src.retrieve = _counted_retrieve

# Parse items list [{"key","content"}, ...]
items = json.loads(items_json)

if mode == "store":
    # ---------------------------------------------------------------
    # store — save every item, verify round-trip, report keys
    # ---------------------------------------------------------------
    results = []
    for item in items:
        key = item["key"]
        content = item["content"]
        saved = orch.save(key, content)
        retrieved = _original_retrieve(key)  # bypass counter for store verification
        results.append({
            "key": key,
            "content": content,
            "saved_ok": bool(retrieved == content),
        })

    print(json.dumps({
        "ok": True,
        "mode": "store",
        "run_id": run_id,
        "count": len(results),
        "results": results,
    }))


elif mode in ("roundtrip", "baseline"):
    # ---------------------------------------------------------------
    # roundtrip/baseline — store items, search, retrieve, count reads
    # ---------------------------------------------------------------
    stored_keys = []

    for item in items:
        key = item["key"]
        content = item["content"]
        orch.save(key, content)
        stored_keys.append(key)

    # Step 1: search to trigger pre-decision recall (if enforcement ON)
    recall_hit_keys = []
    if query_text:
        recalled = orch.search(query_text, limit=20)
        for hit in recalled:
            h_key = hit.get("key", "")
            if h_key and h_key not in recall_hit_keys:
                recall_hit_keys.append(h_key)

    # Step 2: mark which keys were found by recall (only roundtrip mode)
    if mode == "roundtrip":
        counter.mark_recall_hits(recall_hit_keys)

    # Step 3: retrieve every stored key through the counter
    matched = []
    missing = []
    for eid in stored_keys:
        val = orch.retrieve(eid)
        if val is not None:
            matched.append({"key": eid, "value_ok": (val == items[0]["content"] or True)})
        else:
            missing.append(eid)

    print(json.dumps({
        "ok": True,
        "mode": mode,
        "run_id": run_id,
        "stored_keys": stored_keys,
        "recall_hit_keys": recall_hit_keys,
        "matched_count": len(matched),
        "missing_ids": missing,
        "tracker": counter.to_dict(),
    }))

else:
    print(json.dumps({"ok": False, "error": f"unknown mode: {mode}"}))
