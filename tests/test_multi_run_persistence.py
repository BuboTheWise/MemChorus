"""Multi-run memory persistence tests via subprocess isolation.

Each test spawns real Python processes (fresh imports, clean module cache)
sharing a common on-disk storage directory. Memories written by one process MUST
be recallable by subsequent ones -- no in-process caching can fake this result."""

import subprocess, sys, tempfile, shutil, os, json
import pytest

_CHILD = os.path.join(os.path.dirname(__file__), "_persistence_child.py")
NUM_STORE_RUNS = 3
PAYLOADS_PER_RUN = 5


def _spawn(env_vars):
    """Run a child Python subprocess and return parsed JSON output."""
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    for k, v in env_vars.items():
        env[k] = str(v)
    result = subprocess.run([sys.executable, _CHILD], capture_output=True, text=True, timeout=30, env=env)
    if result.returncode != 0 or not result.stdout.strip().startswith("{"):
        err_log = result.stderr[-200:] if result.stderr else "no stderr"
        raise RuntimeError("Subprocess failed rc=" + str(result.returncode) + ": " + err_log)
    return json.loads(result.stdout.strip())


class TestMultiRunPersistence:

    @pytest.fixture
    def store_dir(self):
        tmp = tempfile.mkdtemp(prefix="mempersist_")
        yield tmp
        shutil.rmtree(tmp, ignore_errors=True)

    def test_store_and_recall_two_processes(self, store_dir):
        """Store in Process A (run 0), retrieve all in Process B (run 1)."""
        payloads = ["two_process_item_" + str(i) for i in range(5)]
        ds = _spawn({"RUN_ID": "0", "MODE": "store", "PAYLOADS_JSON": json.dumps(payloads), "STORE_DIR": store_dir})
        assert all(r["saved_ok"] for r in ds["results"])
        keys = [r["key"] for r in ds["results"]]
        dr = _spawn({"RUN_ID": "1", "MODE": "recall", "EXPECTED_IDS": ",".join(keys), "STORE_DIR": store_dir})
        assert dr["matched_count"] == len(keys)

    def test_multi_run_store_all_recalled(self, store_dir):
        """Store across 3 separate processes (15 items total), recall everything from a 4th."""
        per_run_keys = []
        for rid in range(NUM_STORE_RUNS):
            d = _spawn({"RUN_ID": str(rid), "MODE": "store", "PAYLOADS_JSON": json.dumps(["r" + str(rid) + "_item_" + str(i) for i in range(PAYLOADS_PER_RUN)]), "STORE_DIR": store_dir})
            per_run_keys.extend([r["key"] for r in d["results"]])
        dr = _spawn({"RUN_ID": "100", "MODE": "recall", "EXPECTED_IDS": ",".join(per_run_keys), "STORE_DIR": store_dir})
        assert dr["matched_count"] == NUM_STORE_RUNS * PAYLOADS_PER_RUN

    def test_content_integrity_across_boundaries(self, store_dir):
        """Verify exact text content survives subprocess teardown/restart."""
        originals = ["alpha_memory_1", "beta_memory_2", "gamma_memory_3"]
        ds = _spawn({"RUN_ID": "0", "MODE": "store", "PAYLOADS_JSON": json.dumps(originals), "STORE_DIR": store_dir})
        for i, item in enumerate(ds["results"]):
            dr = _spawn({"RUN_ID": str(i + 2), "MODE": "recall", "EXPECTED_IDS": item["key"], "STORE_DIR": store_dir})
            assert dr["matched_count"] == 1
            assert dr["results"][0]["value"] == originals[i]


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
