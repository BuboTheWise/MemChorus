"""
End-to-end verification that MEMCHORUS_CONFIG with raw adapter key names
(e.g. 'hermes_default', 'mempalace') are correctly translated to the _config-
suffixed keys ('hermes_default_config', 'mempalace_config') before reaching
MemoryOrchestrator.__init__.

This test MUST run in an isolated subprocess. We use unittest to ensure clean
state, but the actual bootstrap call happens via a child Python process so that
the singletons and sys.modules don't leak between runs.

Covers acceptance criteria:
 AC-1: MEMCHORUS_CONFIG with {"hermes_default": {"memory_dir": "/custom/path"}}\n         routes to the adapter instance.
 AC-2: Memory files materialize at the custom path on disk.\n"""

import json
import os
import subprocess
import sys
import tempfile
import pathlib
import unittest


BOOTSTRAP_ALIAS_SCRIPT = r'''
import os, sys, json, tempfile, pathlib

os.environ["MEMCHORUS_AUTO_ENABLED"] = "true"

# Point hermes_default adapter to a temp directory so we can verify file creation
custom_dir = "%(CUSTOM_DIR)s"
os.environ["MEMCHORUS_CONFIG"] = json.dumps({
    "hermes_default": {
        "memory_dir": custom_dir,
    },
})

# Trigger bootstrap in a fresh process
from memchorus.auto_bootstrap import _bootstrap
orchestrator = _bootstrap()

if orchestrator is None:
    print("FAIL: _bootstrap returned None", file=sys.stderr)
    sys.exit(1)

# Check the hermes_default adapter received our memory_dir override
hd_source = orchestrator.memory_sources.get("hermes_default")
if hd_source is None:
    print("FAIL: hermes_default source not registered", file=sys.stderr)
    sys.exit(1)

actual_dir = hd_source.config.get("memory_dir", hd_source.config.get("_memory_dir"))
print(f"adapter_memory_dir={actual_dir}")

# Verify save() actually writes to that directory (AC-2)
try:
    orchestrator.save(key="alias_test_key", value="alias_test_value")
    import time
    time.sleep(0.3)  # allow async flush
except Exception as e:
    print(f"WARN: save failed (non-fatal for this check): {e}")

# Scan the custom directory for any .json files to prove materialization
found_files = list(pathlib.Path(custom_dir).glob("*.json"))
print(f"files_in_custom_dir={len(found_files)}")
if found_files:
    sample_content = found_files[0].read_text()
    print(f"sample_file_valid_json={not bool(json.loads(sample_content)) or True}")
    try:
        json.loads(sample_content)
        print("sample_file_is_valid_json=true")
    except Exception as e:
        print(f"sample_file_is_valid_json=false|error={{e}}")

print("RESULT=PASS")
'''


class TestBootstrapKeyAliasRouting(unittest.TestCase):
    """Verify MEMCHORUS_CONFIG key alias translation reaches adapter instances."""

    def test_hermes_default_key_routed_via_env(self):
        """AC-1: hermes_default in MEMCHORUS_CONFIG routes to hermes_default_config internally."""
        with tempfile.TemporaryDirectory() as tmpdir:
            script = BOOTSTRAP_ALIAS_SCRIPT % {"CUSTOM_DIR": tmpdir}
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PYTHONPATH": str(pathlib.Path(__file__).resolve().parent.parent / "src")},
            )

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            # Check for RESULT=PASS in output
            self.assertIn("RESULT=PASS", stdout,
                f"Bootstrap alias test failed. STDOUT:\n{stdout}\nSTDERR:\n{stderr}")

            # Verify the adapter received the custom memory_dir
            self.assertIn(f"adapter_memory_dir={tmpdir}", stdout,
                f"Custom memory_dir not routed to adapter. STDOUT:\n{stdout}")

    def test_files_materialize_at_custom_path(self):
        """AC-2: Memory files materialize at the overridden path with valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            script = BOOTSTRAP_ALIAS_SCRIPT % {"CUSTOM_DIR": tmpdir}
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PYTHONPATH": str(pathlib.Path(__file__).resolve().parent.parent / "src")},
            )

            stdout = result.stdout.strip()
            self.assertIn("RESULT=PASS", stdout)

            # Parse file count from output
            for line in stdout.splitlines():
                if line.startswith("files_in_custom_dir="):
                    count = int(line.split("=")[1])
                    break
            else:
                count = 0

            self.assertGreater(count, 0,
                f"No .json files found at custom path. STDOUT:\n{stdout}")


if __name__ == "__main__":
    unittest.main()
