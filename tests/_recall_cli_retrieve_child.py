"""Child-process driver for the #253 spec test suite.

Run directly by the pytest harness (``subprocess.run``):

    python tests/_recall_cli_retrieve_child.py <key> <mode> [--json]

Modes
-----
``hit``
    Pre-seed ``MemoryOrchestrator._retrieve_cache`` with a canned body for
    the key, then call ``recall_cli.main(["retrieve", key, ...])``.
    Expects exit 0, body on stdout, nothing on stderr.

``miss``
    Register an orchestrator whose cache is empty for the key and which has
    a stub source that returns ``None``.  Expects exit 1, "cold cache or
    never saved" on stderr.

``noorche``
    Stub ``memchorus.get_orchestrator`` to return ``None``.  Expects exit 2,
    an error message on stderr.

The pytest file (``tests/test_recall_cli_retrieve.py``) invokes this script
via ``subprocess.run`` and asserts the returncode plus the correct stdout /
stderr — matching the spec's "run the CLI subcommand via subprocess" language.
"""
from __future__ import annotations

import sys
import time as _time

import memchorus
from memchorus import MemoryOrchestrator
from memchorus import recall_cli


class _StubSource:
    """Duck-typed memory source: retrieve() → None for every key."""

    name = "_stub"
    supports_recall = True
    supports_enforce = False

    def retrieve(self, key: str):
        return None

    def recall(self, **kw):
        return None

    def record(self, **kw):
        return 0

    def enforce(self, **kw):
        return None


def _seed_orchestrator(key: str, body: object) -> None:
    """Build a minimal orchestrator and inject a cache entry for *key*."""
    orch = MemoryOrchestrator({"default_source": "_stub", "recall": {}})
    stub = _StubSource()
    orch._default_source_name = "_stub"
    reg = orch.memory_sources
    reg.clear()
    try:
        reg.setdefault("_stub", stub)  # type: ignore[arg-type]
    except Exception:
        reg["_stub"] = stub  # type: ignore[index]
    # Directly seed the LRU cache so ``retrieve()`` serves the body without
    # touching the real source (which would return None).
    orch._retrieve_cache[key] = (body, _time.monotonic())


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"usage: {sys.argv[0]} <key> <mode> [--json]", file=sys.stderr)
        return 99

    key, mode = argv[0], argv[1]
    as_json = "--json" in argv[2:]

    if mode == "noorche":
        memchorus.get_orchestrator = lambda *a, **kw: None  # type: ignore[assignment]
    elif mode == "hit":
        _seed_orchestrator(key, "resolved-body:" + key)
        memchorus.get_orchestrator = (lambda key=key: next(o for o in [orch]
                         if o is not None))  # patched below
    elif mode == "miss":
        _seed_orchestrator("__not_this_key__", "other")
    else:
        print(f"unknown mode: {mode!r}", file=sys.stderr)
        return 99

    if mode in ("hit", "miss"):
        # Point the module-level get_orchestrator at our seeded orchestrator.
        _orches = [MemoryOrchestrator({"default_source": "_stub", "recall": {}})]
        orch = _orches[0]
        if mode == "hit":
            orch._retrieve_cache[key] = ("resolved-body:" + key, _time.monotonic())
        memchorus.get_orchestrator = lambda *a, **kw: orch  # type: ignore[assignment]

    recall_cli.get_orchestrator = memchorus.get_orchestrator
    args = ["retrieve", key]
    if as_json:
        args.append("--json")
    return recall_cli.main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
