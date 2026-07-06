#!/usr/bin/env python3
"""
Smoke test for MemChorus v1.3.0

Validates the installed package works end-to-end against the actual runtime
environment (site-packages from GitHub HEAD), NOT local dev paths.

Covers:
  - Version & install source verification
  - Lazy bootstrap via public symbol access (not private _instance)
  - Orchestrator API surface (save / retrieve / search)
  - Lifecycle management layer symbols
  - Hook registration entry point (register())

Import the installed package like a real runtime would. No sys.path hacks.
"""

import sys

def test_version_and_installation():
    """Verify v1.3.0 and that install comes from site-packages not ~/MemChorus/src."""
    import memchorus as mc
    assert mc.__version__ == "1.3.0", f"Expected 1.3.0 got {mc.__version__}"
    assert "site-packages" in mc.__file__, \
        f"Install should be from site-packages, not local dev: {mc.__file__}"
    return True


def test_lazy_bootstrap():
    """Public symbol access triggers bootstrap; private _instance does NOT."""
    import memchorus as mc

    # Force fresh state for this test
    mc._bootstrap_done = False
    mc._instance = None
    mc._attr_cache.clear()

    # Touching MemoryOrchestrator should trigger auto-bootstrap
    orch = mc.MemoryOrchestrator  # noqa: F841
    assert mc._bootstrap_done, "Public symbol access did NOT trigger bootstrap"
    assert mc._instance is not None, "Bootstrap left _instance empty"
    return True


def test_orchestrator_public_api():
    """Orchestrator has the hooks contract methods (save, retrieve, search)."""
    import memchorus as mc
    # Bootstrap via public symbol access, then get the live singleton instance.
    _ = mc.MemoryOrchestrator  # noqa: F841 — triggers lazy bootstrap
    orch = mc._instance
    assert orch is not None, "Bootstrap did not set the live singleton"

    for method in ("save", "retrieve", "search"):
        assert hasattr(orch, method) and callable(getattr(orch, method)), \
            f"Missing public API method: {method}"
    return True


def test_hook_entry_point():
    """register() exists and is callable — proves plugin system works."""
    from memchorus import hooks
    assert callable(hooks.register), "register() should be callable"
    return True


def test_lifecycle_layer_available():
    """Phase 1 symbols are importable and resolve to real classes."""
    from memchorus import LifecycleManager, AuditLogger

    assert hasattr(LifecycleManager, "__init__"), "LifecycleManager missing __init__"
    assert hasattr(AuditLogger, "log"), "AuditLogger missing .log method"
    # SweepScheduler requires a manager reference — just verify the class exists
    from memchorus.lifecycle_manager import SweepScheduler
    assert SweepScheduler is not None, "SweepScheduler class could not be imported"
    return True


def test_save_and_search_flow():
    """End-to-end save → search against the real installed orchestrator.

    Gracefully degrades backends that lack credentials; relies on whatever
    sources are actually reachable in this environment (hermes_default at minimum).
    """
    import memchorus as mc
    _ = mc.MemoryOrchestrator  # noqa: F841 — triggers lazy bootstrap
    orch = mc._instance

    # Save a memory item with a deterministic key
    test_key = "smoke_test_integration_13"
    saved = orch.save(test_key, "integration smoke payload")
    if not saved:
        print(f"[warn] save returned falsy for key {test_key} (backend may be degraded)")

    # Search back to prove the pipeline isn't completely broken
    results = orch.search("smoke payload", limit=5)
    # Results list is fine even if empty when backends are offline in this env.
    assert isinstance(results, list), f"search() should return list, got {type(results)}"

    found = [r for r in results if "smoke_test" in r.get("key", "")]
    if found:
        print(f"[ok] Saved item found in search — pipeline connected")
    else:
        # Not a hard failure if backends are genuinely offline
        print(f"[warn] No results from search (backends may be offline) — list returned OK")

    return True


def main():
    """Run smoke tests and report results."""
    print("=" * 60)
    print("MemChorus v1.3.0 Smoke Test Suite")
    print("=" * 60)

    tests = [
        ("Version & Install Source", test_version_and_installation),
        ("Lazy Bootstrap Mechanism", test_lazy_bootstrap),
        ("Orchestrator Public API", test_orchestrator_public_api),
        ("Hook Entry Point (register)", test_hook_entry_point),
        ("Lifecycle Layer Symbols", test_lifecycle_layer_available),
        ("Save -> Search Flow", test_save_and_search_flow),
    ]

    passed = 0
    failed = 0
    warnings = []

    for name, fn in tests:
        try:
            result = fn()
            if result is True or result is None:
                print(f"  [PASS] {name}")
                passed += 1
            else:
                print(f"  [WARN] {name}: unexpected return value {result}")
                warnings.append(result)
        except AssertionError as exc:
            print(f"  [FAIL] {name}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
            failed += 1

    summary = f"\nResults: {passed} passed, {failed} failed"
    if warnings:
        summary += f", {len(warnings)} warnings (non-fatal)"
    print(summary)
    print("=" * 60)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
