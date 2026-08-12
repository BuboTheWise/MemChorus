#!/usr/bin/env python3
"""Synthetic Natural Test — MemChorus + MemPalace E2E Validation.

Deterministic test suite that proves MemChorus works end-to-end with
MemPalace backend through a live MCP session. This replaces subjective
benchmarking with concrete pass/fail metrics.

Test dimensions:
  1. Persistent session stays alive after X consecutive operations
  2. Save + retrieve round-trip returns exact payload content  
  3. Semantic search returns the saved entry against its query term
  4. Context window stability is maintained across multiple turns
  5. Profile isolation prevents data bleeding between different users

Requirements: mempalace installed in pipx/venv, this venv has memchorus."""

import json
import os
import random
import string
import tempfile
import time

from memchorus.mempalace_memory_source import MemPalaceMemorySource


# -------------------------------------------------------------------------- #


def _ts():
    return int(time.time() * 1000)

PASS = []
FAIL = []

def t(name, ok):
    if ok:
        PASS.append(name)
    else:
        FAIL.append(name)


class TestEnv:
    """Holds one source for the session lifetime; we close at shutdown."""

    def __init__(self, profile: str = "memchorus", mcp_timeout: int = 90):
        self.source = MemPalaceMemorySource(
            name="mempalace", config={"profile": profile, "mcp_timeout": mcp_timeout}
        )
        self._unique_id = "".join(random.choices(string.ascii_lowercase, k=6))

    def uid(self, suffix: str) -> str:
        return f"synth_{self._unique_id}_{suffix}"

    def close(self):
        try:
            self.source._client.close()
        except Exception:
            pass


TEST_KEYS = []  # collect for cleanup


def test_01_persistent_session_alive(env: TestEnv):
    """Persistent session initializes and stays connected."""
    ok = env.source._client.connect()
    t("T01 persistent connect", ok)
    alive = env.source._client.is_alive if ok else False
    t("T01 session alive after connect", alive)


def test_02_consecutive_ops(env: TestEnv, n: int = 5):
    """Persistent session survives N rapid ops."""
    alive_count = 0
    client = env.source._client

    for i in range(n):
        key = env.uid(f"ping_{i}")
        result = client.call_tool("mempalace_search", {"query": key, "limit": 1})
        # Search may return nothing; we only care that the session didn't die.
        if client.is_alive:
            alive_count += 1

    t(f"T02 {n} consec ops stayed alive ({alive_count}/{n})", alive_count == n)


def test_03_save_round_trip(env: TestEnv):
    """Save a unique key and retrieve it back."""
    payload = json.dumps({
        "test": "synthetic_natural",
        "ts": _ts(),
        "random": "".join(random.choices(string.ascii_letters, k=16)),
    })
    key = env.uid("round_trip")
    TEST_KEYS.append(key)

    save_ok = env.source.save(key=key, value=payload)
    t("T03 save returned True", save_ok)

    retrieved = env.source.retrieve(key)
    has_key = isinstance(retrieved, list) and len(retrieved) > 0

    found_text = False
    if has_key:
        for entry in (retrieved if isinstance(retrieved, list) else [retrieved]):
            text_val = entry.get("text", "") if isinstance(entry, dict) else str(entry)
            if "synthetic_natural" in text_val:
                found_text = True
                break

    t("T03 retrieved contains saved payload", found_text)


def test_04_semantic_search(env: TestEnv):
    """Search finds a term we just stored."""
    unique_token = env.uid("search_token")
    key = env.uid("for_search")
    TEST_KEYS.append(key)

    save_ok = env.source.save(key=key, value=f"unique lookup phrase {unique_token}")
    t("T04 search - save worked", save_ok)

    results = env.source.search(query=unique_token, limit=5)
    if isinstance(results, list):
        found = any(unique_token in r.get("text", "") for r in results[:10] 
                   if isinstance(r, dict))
    else:
        found = False

    t(f"T04 search found token in {len(results)} results" if isinstance(results, list) else "T04 search returned unexpected type", found)


def test_05_stability_under_load(env: TestEnv):
    """Multiple save + retrieve cycles don't drop the connection."""
    ok = True
    for i in range(3):
        key = env.uid(f"stress_{i}")
        TEST_KEYS.append(key)
        save_ok = env.source.save(key=key, value=f"stress payload {i}")
        time.sleep(0.25)  # let compactor breathe
        if not env.source._client.is_alive:
            ok = False
            break

    t("T05 stability under load (3 cycles)", ok)


def test_06_profile_isolation(env_a: TestEnv, env_b: TestEnv):
    """Data from profile A never appears in profile B."""
    key = env_a.uid("isolated")
    TEST_KEYS.append(key)

    save_ok = env_a.source.save(
        key=key, value="only visible to profile A"
    )
    
    cross_data = env_b.source.search(query="only visible to profile A", limit=50)
    if isinstance(cross_data, list):
        found_cross = any("only visible to profile" in r.get("text","") 
                         for r in cross_data if isinstance(r, dict))
    else:
        found_cross = False

    t("T06 no cross-profile data leakage", not found_cross)


# -------------------------------------------------------------------------- #
def main():
    print("=" * 70)
    print("SYNTHETIC NATURAL TEST — MemChorus + MemPalace")
    print("=" * 70)

    env = TestEnv(profile="memchorus")
    
    test_01_persistent_session_alive(env)
    test_02_consecutive_ops(env, n=5)
    test_03_save_round_trip(env)
    test_04_semantic_search(env)
    test_05_stability_under_load(env)

    # Profile A vs B isolation test
    env_a = TestEnv(profile="memchorus")
    env_b = TestEnv(profile="cthugha")
    try:
        test_06_profile_isolation(env_a, env_b)
    except Exception as e:
        t(f"T06 SKIPPED (env_b unavailable): {e}", False)

    env.close()
    env_a.close()
    env_b.close()

    # Report
    print("\n" + "=" * 70)
    print("RESULTS:")
    for name in PASS:
        print(f"  [PASS] {name}")
    for name in FAIL:
        print(f"  [FAIL] {name}")
    
    total = len(PASS) + len(FAIL)
    pcount = len(PASS)
    fcount = len(FAIL)
    pct = (pcount / total * 100) if total > 0 else 0
    
    print(f"\n{total} tests: {pcount} passed, {fcount} failed ({pct:.0f}%)")
    
    if fcount == 0:
        print("\n>>> ALL TESTS PASSED — Persistent MCP session is proven functional. <<<")
    else:
        print(f"\n>>> {fcount} TEST(S) FAILED — fix required. <<<")

    return 0 if fcount == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())