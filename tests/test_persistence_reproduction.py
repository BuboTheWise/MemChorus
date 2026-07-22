"""
Reproduction test for the MemChorus persistent storage bug.

This test bootstraps the orchestrator exactly as auto_bootstrap does in the
live gateway environment, then attempts to save actual data and verify it
persists to disk — reproducing the exact failure mode where hooks fire but
data never materializes in storage backends.

Steps:
  1. Boot orchestrator via auto_bootstrap (same path as live gateway)
  3. Inspect memory_sources types (string vs actual MemorySource adapter objects)
  4. Attempt to save test data through recommended_sources pathway
  5. Verify the data actually exists on disk
  6. Clean up test artifacts

Run: PYTHONPATH=src python3 -m pytest tests/test_persistence_reproduction.py -v
"""

import os
import sys
import json
import tempfile
import shutil
from pathlib import Path

# Ensure src/ is on the path so we hit the working copy, not the installed version
TEST_SRC = str(Path(__file__).resolve().parent.parent / "src")
if TEST_SRC not in sys.path:
    sys.path.insert(0, TEST_SRC)

import pytest


_MEMORY_CONFIG_ENV_KEY = "MEMCHORUS_CONFIG"


def boot_orchestrator_with_test_config(config_override: dict | None = None):
    """Boot the orchestrator through auto_bootstrap with a controlled config."""
    from memchorus import auto_bootstrap, MemoryOrchestrator

    # Reset singleton to avoid contamination between tests
    auto_bootstrap._instance = None
    auto_bootstrap._bootstrap_lock = False
    auto_bootstrap._bootstrap_attempts = 0

    # Point hermes_default to a temp directory so we don't pollute the real one
    tmp_dir = tempfile.mkdtemp(prefix="mc_test_")
    override = {
        "hermes_default_config": {
            "memory_dir": tmp_dir,
        },
    }
    if config_override:
        override.update(config_override)

    os.environ[_MEMORY_CONFIG_ENV_KEY] = json.dumps(override)
    orchestrator = auto_bootstrap._bootstrap()

    return orchestrator, tmp_dir


@pytest.fixture(scope="function")
def live_orchestrator():
    """Provide a freshly-booted orchestrator with a temp memory dir."""
    from memchorus import auto_bootstrap

    # Reset singleton state between runs
    try:
        auto_bootstrap._instance = None
        auto_bootstrap._bootstrap_lock = False
        auto_bootstrap._bootstrap_attempts = 0
    except Exception:
        pass

    orchestrator, tmp_dir = boot_orchestrator_with_test_config()
    yield orchestrator, tmp_dir
    # Cleanup temp dir
    shutil.rmtree(tmp_dir, ignore_errors=True)


class TestPersistenceReproduction:
    """End-to-end tests verifying data actually persists to disk."""

    def test_memory_sources_are_adapter_objects(self, live_orchestrator):
        """AC1: memory_sources should contain actual MemorySource instances, not strings.

        This is the core bug: when memory_sources contains string values instead of
        instantiated adapter objects, all save() calls silently fail because you
        can't call .save() on a string.
        """
        from memchorus import HermesDefaultMemorySource

        orchestrator, tmp_dir = live_orchestrator

        print(f"\n--- memory_sources contents ---")
        for name, src in orchestrator.memory_sources.items():
            avail = getattr(src, 'is_available', None)
            if callable(avail):
                avail_val = avail()
            else:
                avail_val = True
            print(f"  {name}: type={type(src).__name__}, available={avail_val}")

        # Assert at least one source is a real MemorySource adapter
        has_real_source = False
        for name, src in orchestrator.memory_sources.items():
            if not isinstance(src, str):
                has_real_source = True
                break

        assert has_real_source, (
            f"memory_sources contains only strings: {orchestrator.memory_sources}"
        )

    def test_save_via_recommended_sources(self, live_orchestrator):
        """AC2: AutoStorageEngine capture pipeline should persist to disk.

        This replicates the exact flow used by the hook callback:
        1. Get recommended sources for write_type='general'
        2. Save through orchestrator.save() using source name from recommended list
        3. Verify file exists on disk
        """
        orchestrator, tmp_dir = live_orchestrator

        # Step A: Get recommended sources (same as AutoStorageEngine.capture_outcome)
        candidate_sources = orchestrator.recommended_sources(write_type="general")
        print(f"\nRecommended sources: {candidate_sources}")
        assert len(candidate_sources) > 0, "No recommended sources returned"

        # Step B: Save via orchestrator (same flow as AutoStorageEngine)
        test_key = "REPRO_test_persistence_001"
        test_payload = {
            "text": "This is a reproduction test to verify that data actually persists to disk storage",
            "categories": ["AUTO", "RESULT"],
            "category": "RESULT",
            "_auto_provenance": True,
            "provenance": "auto_stored",
            "importance_score": 0.5,
        }

        saved = False
        for src_name in candidate_sources:
            result = orchestrator.save(test_key, test_payload, source_name=src_name)
            print(f"Save to {src_name}: result={result}")
            if result:
                saved = True
                break

        assert saved, (
            f"No save succeeded across {len(candidate_sources)} candidate sources. "
            f"Candidates: {candidate_sources}"
        )

    def test_verify_file_on_disk(self, live_orchestrator):
        """AC3: After saving, the data should exist as a file on disk in the memory directory.

        This is the ultimate verification — not just that save() returned True,
        but that actual bytes exist on the filesystem.
        """
        orchestrator, tmp_dir = live_orchestrator

        test_key = "REPRO_verify_file_on_disk"
        test_payload = {
            "text": "Persistence proof: if you can read this file on disk, MemChorus storage works",
            "categories": ["AUTO", "RESULT"],
            "_auto_provenance": True,
        }

        # Save using direct source (bypass recommended_sources entirely)
        ok = orchestrator.save(test_key, test_payload, source_name="hermes_default")
        assert ok, f"save() returned False for hermes_default"

        # Actually scan the directory to prove file exists
        files_in_dir = list(Path(tmp_dir).glob("*.json"))
        print(f"\nFiles in memory dir {tmp_dir}:")
        for f in files_in_dir:
            print(f"  {f.name}")

        # At least one JSON file should exist
        assert len(files_in_dir) > 0, (
            f"No .json files found in {tmp_dir} after save() returned True. "
            "The save succeeded in memory but did not persist to disk."
        )

        # Find the file containing our test key (filename is slugged from the key)
        found = False
        for f in files_in_dir:
            with open(f) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                text_content = data.get("text", "")
                # Match by text content substring or check filename matches slugged key
                slug = test_key.lower().replace("_", "-")
                if "persistence proof" in text_content or slug in f.name:
                    found = True
                    break

        assert found, (
            f"JSON files exist but none contain our test key '{test_key}'. "
            f"Files present: {[f.name for f in files_in_dir]}"
        )

    def test_auto_storage_engine_full_pipeline(self, live_orchestrator):
        """AC4: The complete AutoStorageEngine capture_outcome flow should persist data.

        This is the exact path taken by post_tool_call hooks — simulate the full
        pipeline from detection through persistence.
        """
        orchestrator, tmp_dir = live_orchestrator

        from memchorus.auto_storage_engine import AutoStorageEngine

        engine = AutoStorageEngine(orchestrator)

        # Simulated tool output that should trigger capture
        test_output = (
            "I learned that the MemChorus hooks.py register callback loads save_triggers early, "
            "triggers lazy bootstrap before registration to prevent _instance=None, and maintains "
            "a GC-proof reference while registering 3 lifecycle hooks with the gateway context. "
            "This was verified during debugging."
        )

        result = engine.capture_outcome(test_output)
        print(f"\nCapture result: {result}")

        assert result["saved"] is True, (
            f"AutoStorageEngine did not save despite significant content: {result}"
        )

        # Verify it actually made it to disk
        files_in_dir = list(Path(tmp_dir).glob("*.json"))
        assert len(files_in_dir) > 0, (
            f"No JSON files after AutoStorageEngine capture. Dir: {tmp_dir}"
        )

    def test_retrieve_returns_saved_data(self, live_orchestrator):
        """AC5: retrieve() should actually return data previously saved."""
        orchestrator, tmp_dir = live_orchestrator

        test_key = "REPRO_retrieve_test"
        test_value = {"text": "Retrieval test payload", "categories": ["RESULT"]}

        ok = orchestrator.save(test_key, test_value, source_name="hermes_default")
        assert ok, "save() failed for hermes_default"

        retrieved = orchestrator.retrieve(test_key)
        print(f"\nRetrieved: {retrieved}")

        assert retrieved is not None, "retrieve() returned None despite successful save"
        assert "text" in retrieved, f"Retrieved data missing 'text' key: {retrieved}"
