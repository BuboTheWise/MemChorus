"""Multi-run memory persistence tests via subprocess isolation.

Each test spawns real Python processes (fresh imports, clean module cache)
sharing a common on-disk storage directory. Memories written by one process MUST
be recallable by subsequent ones -- no in-process caching can fake this result."""

import subprocess
import sys
import tempfile
import shutil
import os
import json

import pytest

_CHILD = os.path.join(os.path.dirname(__file__), "_persistence_child.py")

NUM_STORE_RUNS = 3
PAYLOADS_PER_RUN = 5


def _spawn(env_vars):
    """Run a child Python subprocess and return its parsed JSON output."""
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    for k, v in env_vars.items():
        env[k] = str(v)

    result = subprocess.run(
        [sys.executable, _CHILD],
        capture_output=True, text=True, timeout=30, env=env,
    )
    if result.returncode != 0 or not result.stdout.strip().startswith("{"):
        return None
    try:
        return json.loads(result.stdout.strip())
    except Exception:
        return None


class TestMultiRunPersistence:

    @pytest.fixture
    def store_dir(self):
        tmp = tempfile.mkdtemp(prefix="mempersist_test_")
        yield tmp
        shutil.rmtree(tmp, ignore_errors=True)

    def test_store_and_recall_two_processes(self, store_dir):
        """Store 5 memories in Process A, retrieve all in Process B."""
        payloads = ["two_process_item_" + str(i) for i in range(5)]
        data_store = _spawn({
            "RUN_ID": "0",
            "MODE": "store",
            "PAYLOADS_JSON": json.dumps(payloads),
            "STORE_DIR": store_dir,
        })
        assert data_store is not None, "Store subprocess failed"
        assert data_store["ok"]
        assert all(item["saved_ok"] for item in data_store["results"])

        keys = [item["key"] for item in data_store["results"]]
        data_recall = _spawn({
            "RUN_ID": "100",
            "MODE": "recall",
            "EXPECTED_IDS": ",".join(keys),
            "STORE_DIR": store_dir,
        })
        assert data_recall is not None, "Recall subprocess failed"
        assert data_recall["matched_count"] == len(keys)
        assert len(data_recall["missing_ids"]) == 0

    def test_multi_run_persistence(self, store_dir):
        """Store across 3 runs (15 items), recall all from a 4th process."""
        per_run_keys = []
        for run_id in range(NUM_STORE_RUNS):
            payloads = ["r" + str(run_id) + "_item" + str(
                i) for i in range(PAYLOADS_PER_RUN)]
            data = _spawn({
                "RUN_ID": str(run_id),
                "MODE": "store",
                "PAYLOADS_JSON": json.dumps(payloads),
                "STORE_DIR": store_dir,
            })
            assert data is not None, "Store run " + str(run_id) + " failed"
            for item in data["results"]:
                per_run_keys.append(item["key"])

        all_expected = NUM_STORE_RUNS * PAYLOADS_PER_RUN
        data_all = _spawn({
            "RUN_ID": "200",
            "MODE": "recall",
            "EXPECTED_IDS": ",".join(per_run_keys),
            "STORE_DIR": store_dir,
        })
        assert data_all is not None
        assert data_all["matched_count"] == all_expected
        assert len(data_all["missing_ids"]) == 0

    def test_incremental_recall_windows(self, store_dir):
        """Recall after each run should grow progressively."""
        per_run_keys = []
        for run_id in range(NUM_STORE_RUNS):
            payloads = ["win" + str(run_id) + "_p" + str(
                i) for i in range(PAYLOADS_PER_RUN)]
            data = _spawn({
                "RUN_ID": str(run_id),
                "MODE": "store",
                "PAYLOADS_JSON": json.dumps(payloads),
                "STORE_DIR": store_dir,
            })
            assert data is not None
            per_run_keys.append([item["key"] for item in data["results"]])

        for run_id in range(NUM_STORE_RUNS):
            expect = per_run_keys[:run_id + 1]
            flat_keys = [k for batch in expect for k in batch]
            data = _spawn({
                "RUN_ID": str(300 + run_id),
                "MODE": "recall",
                "EXPECTED_IDS": ",".join(flat_keys),
                "STORE_DIR": store_dir,
            })
            assert data is not None
            expected_count = PAYLOADS_PER_RUN * (run_id + 1)
            assert data["matched_count"] == expected_count

    def test_content_integrity_after_restart(self, store_dir):
        """Verify exact content text survives subprocess boundary."""
        original_texts = [
            "memory_persistence_proof_run_0_item_alpha",
            "memory_persistence_proof_run_0_item_beta",
            "memory_persistence_proof_run_0_item_gamma",
        ]
        data_store = _spawn({
            "RUN_ID": "0",
            "MODE": "store",
            "PAYLOADS_JSON": json.dumps(original_texts),
            "STORE_DIR": store_dir,
        })
        assert data_store is not None

        keys = [item["key"] for item in data_store["results"]]
        for key, expected_text in zip(keys, original_texts):
            from memchorus.hermes_memory_source import HermesDefaultMemorySource
            src = HermesDefaultMemorySource(name="verify", config={"memory_dir": store_dir})
            retrieved = src.retrieve(key)
            assert retrieved == expected_text, (
                "Content mismatch for key " + str(key))
