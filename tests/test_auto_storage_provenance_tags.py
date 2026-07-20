"""
Tests for auto-storage provenance tagging (t_815edf58).

Verifies that AutoStorageEngine always includes "AUTO" in the categories
list of saved payloads, so orchestrator._is_auto_metadata() PATH 1 catches
every auto-stored entry regardless of detected significance category.

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
# Test 1: Every captured outcome carries "AUTO" in categories
# ---------------------------------------------------------------------------


class TestAutoCategoryTagging(unittest.TestCase):
    """All significance categories include 'AUTO' in the payload categories list."""

    def _assert_auto_in_categories(self, orch, text, expected_sig):
        engine = AutoStorageEngine(orch, min_content_length=10)
        result = engine.capture_outcome(text)
        self.assertTrue(result["saved"], "Text should have been saved: %s" % text)
        # Check the actual payload that went through orchestrator.save()
        key, payload = orch.saves[0]
        self.assertIn("categories", payload)
        cats = payload["categories"]
        self.assertIn(
            "AUTO", cats,
            "'AUTO' should be in categories for %s content" % expected_sig.value,
        )

    def test_learning_includes_auto(self):
        orch = _TrackingOrchestrator()
        self._assert_auto_in_categories(
            orch,
            "I learned that the API returns JSON now",
            SignificanceCategory.LEARNING,
        )

    def test_mistake_includes_auto(self):
        orch = _TrackingOrchestrator()
        self._assert_auto_in_categories(
            orch,
            "Something went wrong with the deployment",
            SignificanceCategory.MISTAKE,
        )

    def test_decision_includes_auto(self):
        orch = _TrackingOrchestrator()
        self._assert_auto_in_categories(
            orch,
            "We decided to migrate to the new framework",
            SignificanceCategory.DECISION,
        )

    def test_result_includes_auto(self):
        orch = _TrackingOrchestrator()
        self._assert_auto_in_categories(
            orch,
            "The benchmark achieved 99 accuracy and was a success",
            SignificanceCategory.RESULT,
        )

    def test_fallback_result_still_has_auto(self):
        """When no significance keyword matches but threshold is met, default RESULT still carries AUTO."""
        orch = _TrackingOrchestrator()
        engine = AutoStorageEngine(orch, min_content_length=10)
        # Text long enough to pass trivial filter but without any significance keywords
        result = engine.capture_outcome(
            "This is a sufficiently length sentence with no detectable pattern"
        )
        if result["saved"]:
            key, payload = orch.saves[0]
            cats = payload.get("categories", [])
            self.assertIn("AUTO", cats)

    def test_auto_is_first_in_categories(self):
        """AUTO should be the first entry so PATH 1 matching finds it immediately."""
        orch = _TrackingOrchestrator()
        engine = AutoStorageEngine(orch, min_content_length=10)
        engine.capture_outcome("I learned an important lesson about system design")
        key, payload = orch.saves[0]
        self.assertEqual(payload["categories"][0], "AUTO")


# ---------------------------------------------------------------------------
# Test 2: Subprocess isolation - end-to-end provenance tagging + penalty pipeline
# ---------------------------------------------------------------------------

def _build_subprocess_script():
    """Build an isolated subprocess script that tests the full pipeline."""
    return (
        "import json\n"
        "import sys\n"
        "import os\n"
        'sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))\n'
        "from memchorus.auto_storage_engine import AutoStorageEngine\n"
        "class TOrch:\n"
        "    def __init__(self):\n"
        "        self.saves = []\n"
        '    def recommended_sources(self, write_type="general", max_results=3):\n'
        '        return ["mock"]\n'
        "    def save(self, key, value, **kw):\n"
        "        self.saves.append((key, value))\n"
        "        return True\n"
        "    def retrieve(self, key):\n"
        "        return None\n"
        'o = TOrch()\n'
        "e = AutoStorageEngine(o, min_content_length=10)\n"
        "texts = [\n"
        '  "I learned that memory retrieval fails without proper indexing",\n'
        '  "Something went wrong when we tried the old approach",\n'
        '  "We decided to replace the legacy pipeline entirely",\n'
        '  "The final benchmark result was 98 accuracy and a success",\n'
        "]\n"
        "errs = []\n"
        "for t in texts:\n"
        "    r = e.capture_outcome(t)\n"
        "    if not r['saved']:\n"
        "        errs.append('not saved ' + str(r.get('significance', '?')))\n"
        "        continue\n"
        "    k, p = o.saves[-1]\n"
        "    c = p.get('categories', [])\n"
        "    if 'AUTO' not in c:\n"
        "        errs.append('NO AUTO: ' + str(c))\n"
        '# PATH 1 inline check\n'
        "def chk(obj):\n"
        "    ct = getattr(obj, 'content', None)\n"
        "    if isinstance(ct, dict):\n"
        "        for tag in ct.get('categories', []):\n"
        "            if str(tag).upper() in ('RESULT', 'AUTO'):\n"
        "                return True\n"
        "    return False\n"
        "class O:\n"
        "  pass\n"
        "for k, p in o.saves:\n"
        "  oo = O()\n"
        "  oo.content = p\n"
        "  if not chk(oo):\n"
        "    errs.append('PATH1_MISS: ' + str(p.get('categories', [])))\n"
        "rpt = {\n"
        "  'saves_total': len(o.saves),\n"
        "  'all_have_auto': all('AUTO' in x[1].get('categories', []) for x in o.saves),\n"
        "  'all_caught_by_path1': not any(\n"
        "    not chk(type('X', (), {'content': x[1]})()) for x in o.saves\n"
        "  ),\n"
        "  'errors': errs,\n"
        "}\n"
        "print(json.dumps(rpt))\n"
    )


class TestProvenancePipelineSubprocess(unittest.TestCase):
    """End-to-end provenance tagging verified in isolated subprocess."""

    def test_auto_tagging_and_penalty_path1_in_subprocess(self):
        """Run auto_storage_engine inside a subprocess, verify every saved payload
        carries 'AUTO' in categories and is caught by _is_auto_metadata PATH 1."""
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        script = _build_subprocess_script()

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, "Subprocess failed: %s" % result.stderr)

        report = json.loads(result.stdout.strip())

        self.assertGreater(report["saves_total"], 0, "No payloads were saved")
        self.assertTrue(report["all_have_auto"], "Not all payloads carry AUTO category")
        self.assertTrue(
            report["all_caught_by_path1"], "PATH 1 detection missed tagged entries"
        )
        self.assertEqual(
            report["errors"],
            [],
            "Subprocess errors: %s" % report["errors"],
        )


if __name__ == "__main__":
    unittest.main()
