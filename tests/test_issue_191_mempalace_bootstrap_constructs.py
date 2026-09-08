"""
tests/test_issue_191_mempalace_bootstrap_constructs.py

Regression gate for MemChorus issue #191 — circular import during auto-bootstrap.

Root cause (pre-fix, commit d5f919f / 2.0.46)
--------------------------------------------
Line 26 of ``src/memchorus/mempalace_memory_source.py`` used:

    from memchorus import palace_path

while ``mempalace_memory_source`` was still being imported (i.e. the
``memchorus`` package was only partially initialised).  That attribute access
re-enters ``memchorus.__getattr__('palace_path')`` → ``_trigger_lazy_bootstrap()``
→ ``_bootstrap()`` step 3 → ``from memchorus.mempalace_memory_source import
MemPalaceMemorySource`` → the module is already in ``sys.modules`` but not yet
fully executed → **ImportError: cannot import name 'MemPalaceMemorySource' from
partially initialized module 'memchorus.mempalace_memory_source' (most likely due
to a circular import)**.

The fix (commit 26c26b1) changes the import to:

    import memchorus.palace_path as palace_path

which loads the submodule directly into ``sys.modules`` and bypasses the
package-level ``__getattr__`` re-entrance.

Why ``test_bootstrap_alias_routing.py`` is NOT a discriminator for #191
-----------------------------------------------------------------------
``test_bootstrap_alias_routing.py`` asserts two things:
  1. ``MEMCHORUS_CONFIG`` key-alias routing (``hermes_default`` → custom
     ``memory_dir``), and
  2. file materialization in the custom directory.

Both of these succeed on **both** the pre-fix and post-fix heads.  In the pre-fix
case the bootstrap path does fire the circular-import error, but it is logged as
a WARNING/ERROR (not raised), and the recovery path still registers the
``hermes_default`` source and writes a valid file before the error line is even
emitted.  The alias test checks only the stdout-level functional outcome; it
never inspects the stderr log signature.

The clean, deterministic RED↔GREEN discriminator is the **number of
"circular import" / "will be inactive" log lines emitted during bootstrap**:

  RED   (d5f919f / 2.0.46):  CIRCULAR_LOG_LINES > 0
  GREEN (26c26b1 / 2.0.47+): CIRCULAR_LOG_LINES == 0

This test locks that signature.

Test design
-----------
* Runs in an isolated subprocess (fresh Python, ``sys.executable -c``) so that
  package singletons and ``sys.modules`` do not leak from the pytest parent.
* Sets ``MEMCHORUS_AUTO_ENABLED=true`` to enable auto-bootstrap.
* Attaches a ``logging.Handler`` to the ``memchorus`` logger tree before
  triggering bootstrap, capturing every "circular import" / "will be inactive"
  message that the pre-fix path emits.
* Resolves ``memchorus.MemPalaceMemorySource`` (lazy symbol path) and drives
  ``_bootstrap()`` to complete the full sequence.
* No live MCP connection: ``MemPalaceMemorySource.__init__`` only creates the
  client object and sets ``_connected = False``; the subprocess spawn is lazy
  and deferred to the first data-plane call.
* Uses ``HERMES_HOME`` pointing to a temp directory for full hermetic isolation.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

# ---------------------------------------------------------------------------
# Child-process script — runs in a fresh Python interpreter.
#
# Output contract (stdout lines, one per fact):
#   CLASS_RESOLVED=OK cls=MemPalaceMemorySource
#   BOOTSTRAP=OK sources=['hermes_default', 'mempalace', 'session_history'] mempalace_wired=True
#   CIRCULAR_LOG_LINES=0         ← the discriminator (RED: > 0, GREEN: 0)
#   RESULT=CHILD_DONE
# ---------------------------------------------------------------------------
_CHILD_SCRIPT = r'''
import os, sys, io, logging

# --- environment setup (before any memchorus import) ---
os.environ["MEMCHORUS_AUTO_ENABLED"] = "true"
# HERMES_HOME must be set before the first import; it is resolved lazily but
# set it here so every hermes_home() call in this process is deterministic.
os.environ["HERMES_HOME"] = os.environ["MC_ISSUE_191_HOME"]

# Attach a handler to the root "memchorus" logger so we can count
# "circular import" / "will be inactive" log lines emitted during bootstrap.
# Logging propagates upward: "memchorus.auto_bootstrap" → "memchorus", etc.
_circ_lines = []

class _Issue191Handler(logging.Handler):
    def emit(self, record):
        msg = record.getMessage()
        low = msg.lower()
        if "circular import" in low or "will be inactive" in low:
            _circ_lines.append(msg.strip())

_handler = _Issue191Handler()
logging.getLogger("memchorus").addHandler(_handler)

# Use write_through so we see all output even on abnormal exit.
out = io.TextIOWrapper(sys.stdout.buffer, write_through=True, line_buffering=True)

def p(*a):
    print(*a, file=out)

# --- 1. Lazy symbol resolution (the re-entrant path #191 exercises) ---
try:
    import memchorus
    cls = memchorus.MemPalaceMemorySource
    p("CLASS_RESOLVED=OK cls=" + cls.__name__)
except ImportError as e:
    is_circ = "circular import" in str(e).lower()
    p("CLASS_RESOLVED=FAIL circular=%s exc=%s: %s" % (is_circ, type(e).__name__, e))
except Exception as e:
    p("CLASS_RESOLVED=FAIL %s: %s" % (type(e).__name__, e))

# --- 2. Full _bootstrap() (same sequence as test_bootstrap_alias_routing) ---
try:
    from memchorus.auto_bootstrap import _bootstrap
    o = _bootstrap()
    if o is None:
        p("BOOTSTRAP=NONE_INACTIVE")
    else:
        srcs = sorted(getattr(o, "memory_sources", {}).keys())
        mp_wired = "mempalace" in srcs
        p("BOOTSTRAP=OK sources=%s mempalace_wired=%s" % (srcs, mp_wired))
except Exception as e:
    p("BOOTSTRAP=FAIL %s: %s" % (type(e).__name__, e))

# --- 3. Report circular-import log-line count (THE discriminator) ---
p("CIRCULAR_LOG_LINES=%d" % len(_circ_lines))
for line in _circ_lines[:5]:
    p("  CIRC: " + line[:200])
p("RESULT=CHILD_DONE")
sys.stdout.flush()
'''
# End child script


class TestIssue191BootstrapConstruction(unittest.TestCase):
    """Lock the #191 circular-import regression (RED on d5f919f, GREEN on 26c26b1)."""

    def _run_child(self, tmpdir: str) -> subprocess.CompletedProcess:
        """Run the child script in a fresh subprocess; return the result."""
        result = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "MC_ISSUE_191_HOME": tmpdir,
                "PYTHONPATH": str(
                    pathlib.Path(__file__).resolve().parent.parent / "src"
                ),
            },
        )
        return result

    # ------------------------------------------------------------------ #
    # PRIMARY DISCRIMINATOR — fails on pre-fix, passes on post-fix
    # ------------------------------------------------------------------ #

    def test_no_circular_import_log_lines_during_bootstrap(self):
        """The #191 regression: bootstrap must NOT emit any 'circular import'
        or 'memchorus will be inactive' log lines.

        RED (d5f919f / 2.0.46):   CIRCULAR_LOG_LINES > 0  → this test FAILS
        GREEN (26c26b1 / 2.0.47+): CIRCULAR_LOG_LINES == 0 → this test PASSES
        """
        with tempfile.TemporaryDirectory(prefix="mc_191_") as tmpdir:
            r = self._run_child(tmpdir)
            stdout = r.stdout.strip()

            self.assertIn(
                "RESULT=CHILD_DONE", stdout,
                f"Child process did not complete. "
                f"STDOUT:\n{stdout}\nSTDERR:\n{r.stderr}"
            )

            # The key assertion: zero circular-import / inactive log lines
            self.assertIn(
                "CIRCULAR_LOG_LINES=0", stdout,
                f"Circular-import regression detected in bootstrap (issue #191). "
                f"The 'from memchorus import X' pattern re-enters "
                f"memchorus.__getattr__ mid-bootstrap and raises ImportError. "
                f"Expected CIRCULAR_LOG_LINES=0 but found a non-zero count.\n"
                f"STDOUT:\n{stdout}\n"
                f"STDERR (relevant tail):\n"
                f"{''.join(r.stderr.splitlines(keepends=True)[-6:])}"
            )

    # ------------------------------------------------------------------ #
    # AUXILIARY GUARDS — pass on both heads, prevent unrelated regressions
    # ------------------------------------------------------------------ #

    def test_mempalace_memory_source_resolvable(self):
        """MemPalaceMemorySource must be importable through the lazy
        __getattr__ path (the exact code path that #191 broke)."""
        with tempfile.TemporaryDirectory(prefix="mc_191_") as tmpdir:
            r = self._run_child(tmpdir)
            stdout = r.stdout.strip()

            self.assertIn(
                "RESULT=CHILD_DONE", stdout,
                f"Child process did not complete. STDOUT:\n{stdout}\n"
                f"STDERR:\n{r.stderr}"
            )
            self.assertIn(
                "CLASS_RESOLVED=OK cls=MemPalaceMemorySource", stdout,
                f"MemPalaceMemorySource failed to resolve via lazy __getattr__. "
                f"STDOUT:\n{stdout}\nSTDERR:\n{r.stderr}"
            )

    def test_bootstrap_returns_active_orchestrator(self):
        """_bootstrap() must return a non-None orchestrator (not trigger the
        degraded fallback path)."""
        with tempfile.TemporaryDirectory(prefix="mc_191_") as tmpdir:
            r = self._run_child(tmpdir)
            stdout = r.stdout.strip()

            self.assertIn(
                "RESULT=CHILD_DONE", stdout,
                f"Child process did not complete. STDOUT:\n{stdout}\n"
                f"STDERR:\n{r.stderr}"
            )
            self.assertIn(
                "BOOTSTRAP=OK", stdout,
                f"_bootstrap() returned None (degraded fallback or hard failure). "
                f"STDOUT:\n{stdout}\nSTDERR:\n{r.stderr}"
            )

    def test_mempalace_source_wired_into_orchestrator(self):
        """After a clean bootstrap, the 'mempalace' source must be present in
        the orchestrator's source registry."""
        with tempfile.TemporaryDirectory(prefix="mc_191_") as tmpdir:
            r = self._run_child(tmpdir)
            stdout = r.stdout.strip()

            self.assertIn(
                "RESULT=CHILD_DONE", stdout,
                f"Child process did not complete. STDOUT:\n{stdout}\n"
                f"STDERR:\n{r.stderr}"
            )
            self.assertIn(
                "mempalace_wired=True", stdout,
                f"'mempalace' source not wired into orchestrator after bootstrap. "
                f"STDOUT:\n{stdout}\nSTDERR:\n{r.stderr}"
            )


if __name__ == "__main__":
    unittest.main()
