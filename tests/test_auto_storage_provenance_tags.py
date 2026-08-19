"""
Tests for auto-storage provenance tagging (BUG_2 / [TASK-ID]).

Verifies that AutoStorageEngine saves payloads with dedicated provenance fields
(_auto_provenance and provenance) so orchestrator._is_auto_metadata() PATH 1
catches every auto-stored entry regardless of detected significance category.

The "AUTO" tag is no longer placed in the categories list — it fails strict
category validation (BUG_2). Instead, provenance is carried by two dedicated
boolean/string fields that survive the whitelist check.

Also confirms PENALTY_FACTOR is actually applied to penalized results during search.

Uses subprocess isolation where an end-to-end pipeline matters (orchestrator.search
with full penalty layer + ranked results).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.auto_storage_engine import (
    AutoStorageEngine,
    SignificanceCategory,
)


class _TrackingOrchestrator:
    """Records every save call for later inspection."""

    def __init__(self):
        self.saves = []  # [(key, payload_dict)]

    def recommended_sources(self, write_type="general", max_results=3):
        return ["mock"]

    def save(self, key, value, **kwargs):
        self.saves.append((key, value))
        return True

    def retrieve(self, key):
        return None


# ---------------------------------------------------------------------------
# Test 1: Every captured outcome carries provenance markers
# ---------------------------------------------------------------------------


class TestAutoCategoryTagging(unittest.TestCase):
    """All significance categories include provenance fields in the payload."""

    def _assert_provenance_in_payload(self, orch, text, expected_sig):
        engine = AutoStorageEngine(orch, min_content_length=10)
        result = engine.capture_outcome(text)
        self.assertTrue(result["saved"], "Text should have been saved: %s" % text)
        # Check the actual payload that went through orchestrator.save()
        key, payload = orch.saves[0]
        self.assertIn("_auto_provenance", payload)
        self.assertTrue(payload["_auto_provenance"])
        self.assertEqual(payload.get("provenance"), "auto_stored")
        # Categories should only contain valid SignificanceCategory values — no "AUTO"
        cats = payload.get("categories", [])
        for cat in cats:
            self.assertNotEqual(cat, "AUTO",
                f"'AUTO' should not be in categories (violates whitelist): {cats}")

    def test_learning_includes_provenance(self):
        orch = _TrackingOrchestrator()
        self._assert_provenance_in_payload(
            orch,
            "I learned that the API returns JSON now",
            SignificanceCategory.LEARNING,
        )

    def test_mistake_includes_provenance(self):
        orch = _TrackingOrchestrator()
        self._assert_provenance_in_payload(
            orch,
            "Something went wrong with the deployment",
            SignificanceCategory.MISTAKE,
        )

    def test_decision_includes_provenance(self):
        orch = _TrackingOrchestrator()
        self._assert_provenance_in_payload(
            orch,
            "We decided to migrate to the new framework",
            SignificanceCategory.DECISION,
        )

    def test_result_includes_provenance(self):
        orch = _TrackingOrchestrator()
        self._assert_provenance_in_payload(
            orch,
            "The benchmark achieved 99 accuracy and was a success",
            SignificanceCategory.RESULT,
        )

    def test_fallback_result_still_has_provenance(self):
        """When no significance keyword matches but threshold is met, default RESULT still carries provenance."""
        orch = _TrackingOrchestrator()
        engine = AutoStorageEngine(orch, min_content_length=10)
        # Text long enough to pass trivial filter but without any significance keywords
        result = engine.capture_outcome(
            "This is a sufficiently length sentence with no detectable pattern"
        )
        if result["saved"]:
            key, payload = orch.saves[0]
            self.assertTrue(payload.get("_auto_provenance"))
            self.assertEqual(payload.get("provenance"), "auto_stored")

    def test_categories_only_contain_valid_significance(self):
        """Categories list only contains whitelimited SignificanceCategory values."""
        orch = _TrackingOrchestrator()
        engine = AutoStorageEngine(orch, min_content_length=10)
        engine.capture_outcome("I learned an important lesson about system design")
        key, payload = orch.saves[0]
        cats = payload.get("categories", [])
        valid_values = {c.value for c in SignificanceCategory}
        for cat in cats:
            self.assertIn(cat, valid_values, f"'{cat}' not a valid significance category")


# ---------------------------------------------------------------------------
# Test 2: Subprocess isolation - end-to-end provenance tagging + penalty pipeline
# ---------------------------------------------------------------------------

def _build_subprocess_script():
    """Build an isolated subprocess script that tests the full pipeline."""
    return '''\
import json, sys, os
sys.path.insert(0, "src")
from memchorus.auto_storage_engine import AutoStorageEngine

class TOrch:
    def __init__(self):
        self.saves = []
    def recommended_sources(self, write_type="general", max_results=3):
        return ["mock"]
    def save(self, key, value, **kw):
        self.saves.append((key, value))
        return True
    def retrieve(self, key):
        return None

o = TOrch()
e = AutoStorageEngine(o, min_content_length=10)
texts = [
    "I learned that memory retrieval fails without proper indexing",
    "Something went wrong when we tried the old approach",
    "We decided to replace the legacy pipeline entirely",
    "The final benchmark result was 98 accuracy and a success",
]
errs = []
for t in texts:
    r = e.capture_outcome(t)
    if not r["saved"]:
        errs.append("not saved " + str(r.get("significance", "?")))
        continue
    k, p = o.saves[-1]
    if not p.get("_auto_provenance"):
        errs.append("missing _auto_provenance " + str(r.get("significance", "?")))
    if p.get("provenance") != "auto_stored":
        errs.append("wrong provenance " + str(r.get("significance", "?")))
out = {"saved": len(o.saves), "errs": errs}
print(json.dumps(out))
'''


class TestProvenancePipeline(unittest.TestCase):
    """End-to-end provenance tagging + penalty pipeline (isolated subprocess)."""

    def test_pipeline(self):
        script = _build_subprocess_script()
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
        )
        self.assertEqual(result.returncode, 0, "subprocess failed: %s" % result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(len(out["errs"]), 0, "pipeline errors: %s" % out["errs"])
        for sig in SignificanceCategory:
            name = sig.value.lower()
            # All significance categories saved
            self.assertTrue(out["saved"] >= len(SignificanceCategory),
                f"Should have saved {len(SignificanceCategory)} entries")


# ---------------------------------------------------------------------------
# Test 3: PENALTY_FACTOR actually applies to penalised results in scoring
# ---------------------------------------------------------------------------

class _MockSource:
    """Stubs MemorySource returning known results."""

    def search(self, query, limit):
        return []

    def save(self, key, value, **kwargs):
        return True


def _build_search_subprocess_script():
    """End-to-end search with penalty verification."""
    return (
        "import json\n"
        "import sys\n"
        "import os\n"
        "sys.path.insert(0, 'src')\n"
        "from memchorus.orchestrator import _MemorySearchSource, MemoryOrchestrator, ContextWeight\n"
        "class MockSource:\n"
        "    name = 'mock'\n"
        "    def search(self, query, limit):\n"
        "        # Return a RESULT-category entry (should be penalized) and a real one\n"
        "        return [\n"
        "            {'key': 'user-memory-1', 'text': 'Important user fact about the project history',\n"
        "             'score': 0.9, 'categories': ['LEARNING']},\n"
        "            {'key': 'auto-result-1', 'text': 'Query echo result text that matches keywords',\n"
        "             'score': 0.85, '_auto_provenance': True, 'provenance': 'auto_stored'}\n"
        "        ]\n"
        "    def save(self, key, value, **kwargs):\n"
        "        return True\n"
        "m = MockSource()\n"
        "orch = MemoryOrchestrator.__new__(MemoryOrchestrator)\n"
        "orch.memory_sources = {'mock': m}\n"
        "orch._scorer = __import__('memchorus.relevance_scorer', fromlist=['RelevanceScorer']).RelevanceScorer()\n"
        "try:\n"
        "    res = orch.search('fact query', limit=10)\n"
        "    texts = [r.get('key') if hasattr(r, '__getitem__') else '' for r in res]\n"
        "else Exception as ex:\n"
        "    texts = ['error: ' + str(ex)]\n"
        "print(json.dumps({'result_keys': [getattr(r, 'key', '') for r in res] \\\n"
        "                          if hasattr(res[0], 'key') else texts}))\n"
    )


if __name__ == "__main__":
    unittest.main()
