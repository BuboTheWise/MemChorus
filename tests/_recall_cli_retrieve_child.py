"""Child-process driver for the recall-``retrieve`` test suite (spec-253 + #255).

Run directly by the pytest harness (``subprocess.run``)::

    python tests/_recall_cli_retrieve_child.py <key> <mode> [--json]

Modes
-----
``hit``
    Pre-seed a real ``MemoryOrchestrator``'s retrieve-cache with a canned body
    for the key, then call ``recall_cli.main(["retrieve", key, ...])``.
    Expects exit 0, body on stdout, nothing on stderr.

``miss``
    Register an orchestrator whose cache is empty for the key and which has a
    stub source that returns ``None``.  Expects exit 1, "cold cache or
    never saved" on stderr.

``noorche``
    Stub ``memchorus.get_orchestrator`` to return ``None`` — i.e. emulate a
    standalone CLI invocation (crontab / shell / test script) running in a
    fresh interpreter where the in-process registry is empty.  This driver
    then just calls ``recall_cli.main(["retrieve", key, ...])`` and lets the
    CLI's own fallback decide the outcome:

      * Issue #255 fix: with no orchestrator the CLI degrades to a bare
        ``MemPalaceMemorySource`` that reads the local JSON cache.  So the
        exit code is a pure function of the cache state, which the *pytest*
        side controls by pointing ``HERMES_HOME`` at a temp dir and
        (optionally) seeding ``<cache>/<key>.json``:

        - cache empty        → exit 1, "cold cache or never saved"
        - key seeded in cache → exit 0, body on stdout

        Pre-#255 this mode always returned exit 2 ("no orchestrator
        registered") regardless of cache state — the bug the #255 regression
        tests pin.

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
        # Standalone CLI: no live orchestrator in this fresh process.  Let the
        # CLI's own #255 fallback (bare MemPalace source over the local cache)
        # decide the outcome; cache state is controlled by the pytest side
        # via HERMES_HOME + seeding.
        memchorus.get_orchestrator = lambda *a, **kw: None  # type: ignore[assignment]
    elif mode == "hit":
        orch = MemoryOrchestrator({"default_source": "_stub", "recall": {}})
        orch._retrieve_cache[key] = ("resolved-body:" + key, _time.monotonic())
        memchorus.get_orchestrator = lambda *a, **kw: orch  # type: ignore[assignment]
    elif mode == "miss":
        orch = MemoryOrchestrator({"default_source": "_stub", "recall": {}})
        orch._retrieve_cache["__not_this_key__"] = (
            "other",
            _time.monotonic(),
        )
        memchorus.get_orchestrator = lambda *a, **kw: orch  # type: ignore[assignment]
    else:
        print(f"unknown mode: {mode!r}", file=sys.stderr)
        return 99

    # ``main`` resolves get_orchestrator at call time via
    # ``from memchorus import get_orchestrator``, so the attribute patch above
    # is the seam.  Mirror it onto the module the CLI imports from to be safe.
    recall_cli.get_orchestrator = memchorus.get_orchestrator
    args = ["retrieve", key]
    if as_json:
        args.append("--json")
    return recall_cli.main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
