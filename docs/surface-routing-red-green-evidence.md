# #226 RED→GREEN evidence — write-time surface-routing classifier

Kanban: t_2c7423c5 (req #3) · Spec: MemChorus-Surface-Routing-Spec §6
Reproducer: 2026-09-26 · HEAD at capture: 902271f + uncommitted fuzz layer

## What this proves

Before the classifier (or with it stubbed to the old all-drawer sink), the
acceptance cases for MEMORY (R1) and KG (R2) routing FAIL. With the
classifier wired in, the SAME 10 selection passes. The 2 drawer rows
(AC-4, AC-5) pass in both states because the old behavior already routed
plain notes to DRAWER — the classifier must not regress them.

## Reproducer

```bash
# RED — baseline tree with classifier replaced by an all-drawer stub:
#   git worktree add --detach /tmp/mc226red-wt 06c0e5c
#   cp <repo>/src/memchorus/surface_routing.py /tmp/mc226red-wt/src/memchorus/
#   cp <repo>/tests/test_surface_routing.py     /tmp/mc226red-wt/tests/
#   (stub: pytest_red_stub.py pins routing_decision()/route() → DRAWER/R3)
cd /tmp/mc226red-wt && PYTHONPATH=src python -m pytest -p pytest_red_stub \
  tests/test_surface_routing.py -k "acceptance_criteria or r0_explicit" -q \
  --no-header --tb=short
#   → 8 failed, 2 passed

# GREEN — the committed classifier:
#   cd <repo> && PYTHONPATH=src python -m pytest \
#   tests/test_surface_routing.py -k "acceptance_criteria or r0_explicit" -q --no-header
#   → 10 passed
```

## RED (all-drawer baseline) — raw pytest output

```
E   AssertionError: textbook KG: typed triple + temporal window: expected KG rule=R2, got DRAWER rule=R3
E   assert 'DRAWER' == 'KG'
E     
E     - KG
E     + DRAWER
___________________ test_r0_explicit_kg_vetoed_when_expired ____________________
[gw2] linux -- Python 3.11.15 ~/.hermes/hermes-agent/venv/bin/python3
tests/test_surface_routing.py:250: in test_r0_explicit_kg_vetoed_when_expired
    assert result.rule == "R4"
E   AssertionError: assert 'R3' == 'R4'
E     
E     - R4
E     + R3
_____________________ test_r0_explicit_memory_is_deferred ______________________
[gw3] linux -- Python 3.11.15 ~/.hermes/hermes-agent/venv/bin/python3
tests/test_surface_routing.py:226: in test_r0_explicit_memory_is_deferred
    assert route("whatever", RouteContext(caller_explicit=SURFACE_MEMORY)) == SURFACE_MEMORY
E   AssertionError: assert 'DRAWER' == 'MEMORY'
E     
E     - MEMORY
E     + DRAWER
=============================== warnings summary ===============================
../../.hermes/hermes-agent/venv/lib/python3.11/site-packages/_hypothesis_pytestplugin.py:487
../../.hermes/hermes-agent/venv/lib/python3.11/site-packages/_hypothesis_pytestplugin.py:487
../../.hermes/hermes-agent/venv/lib/python3.11/site-packages/_hypothesis_pytestplugin.py:487
../../.hermes/hermes-agent/venv/lib/python3.11/site-packages/_hypothesis_pytestplugin.py:487
  ~/.hermes/hermes-agent/venv/lib/python3.11/site-packages/_hypothesis_pytestplugin.py:487: UserWarning: Skipping collection of '.hypothesis' directory - this usually means you've explicitly set the `norecursedirs` pytest config option, replacing rather than extending the default ignores.
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_surface_routing.py::test_route_acceptance_criteria[AC-6] - ...
FAILED tests/test_surface_routing.py::test_route_acceptance_criteria[AC-1] - ...
FAILED tests/test_surface_routing.py::test_route_acceptance_criteria[AC-3] - ...
FAILED tests/test_surface_routing.py::test_route_acceptance_criteria[AC-7] - ...
FAILED tests/test_surface_routing.py::test_r0_explicit_kg_is_deferred_when_current
FAILED tests/test_surface_routing.py::test_route_acceptance_criteria[AC-2] - ...
FAILED tests/test_surface_routing.py::test_r0_explicit_kg_vetoed_when_expired
FAILED tests/test_surface_routing.py::test_r0_explicit_memory_is_deferred - A...
8 failed, 2 passed, 4 warnings in 1.42s
```

## GREEN (classifier wired) — raw pytest output

```
../../.hermes/hermes-agent/venv/lib/python3.11/site-packages/_hypothesis_pytestplugin.py:487
  ~/.hermes/hermes-agent/venv/lib/python3.11/site-packages/_hypothesis_pytestplugin.py:487: UserWarning: Skipping collection of '.hypothesis' directory - this usually means you've explicitly set the `norecursedirs` pytest config option, replacing rather than extending the default ignores.
    warnings.warn(

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
10 passed, 4 warnings in 1.38s
```
