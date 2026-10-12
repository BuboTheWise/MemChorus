"""Regression test for issue #239 — MCP coprocess orphan leak.

The stderr-filter wrapper built by ``MempalaceMemorySource._filter_command``
starts ``mempalace.mcp_server`` as a child and then blocks on its stderr. When
the Hermes host SIGTERMs the *wrapper* (session end, unhandled exception, or
process-group teardown), the wrapper dies before its child, orphaning the
``mcp_server`` coprocess. That orphan keeps holding file descriptors on
``~/.mempalace/palace/chroma.sqlite3``, and because the wrapper is re-spawned
for every MemPalace session the orphans accumulate (see issue #239 for the
324-open-fd live evidence).

This test is child-agnostic: it exercises the *wrapper's* teardown logic by
giving it a long-lived child (a stand-in for ``mempalace.mcp_server``),
SIGTERMs the wrapper, and asserts the child is reaped within a short grace
period.

  * RED   (pre-fix, origin/master): the wrapper has no signal handler -> the
          wrapper dies on SIGTERM, the child survives -> assertion FAILS.
  * GREEN (post-fix): the wrapper's SIGTERM handler reaps the child -> the
          child is gone -> assertion PASSES.

It runs the *actual* ``_filter_command`` source from the checkout under test
(inserted at the front of ``sys.path``) so the RED/GREEN pair tracks the diff,
not a copy of it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Make the checkout under test win over any installed copy (editable-stale trap).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
elif sys.path[0] != str(_SRC):
    sys.path.remove(str(_SRC))
    sys.path.insert(0, str(_SRC))

# ``_McpClient`` is the class that owns the wrapper builder and the noise
# pattern table; calling its ``@classmethod _filter_command`` directly is the
# truest exercise of the code under test and needs no config/python discovery.
from memchorus.mempalace_memory_source import _McpClient  # noqa: E402

# A long-lived child that records its own PID so we can probe "is it alive?"
# precisely (no pgrep -f self-matching). Stand-in for mempalace.mcp_server.
_CHILD_CODE = (
    "import os, sys, time;\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(str(os.getpid()));\n"
    "time.sleep(300)\n"
)

_GRACE_S = 2.0  # AC1: assert after wrapper exit + 2s grace
_WRAPPER_WAIT_S = 8.0  # allow the wrapper's 5s terminate-window + overhead

# POSIX-only: this module drives a REAL process-group signal (os.kill /
# signal.signal / os.killpg / start_new_session=None) at a spawned
# subprocess pair. None of those primitives exist on win32, so the body is
# not expressible there. The skipif makes the intent self-documenting on
# any runner (the skip reason is what pytest reports on Windows); the
# corresponding platform *deselect* in tests/conftest.py (win32) ensures
# the module contributes zero reports so the #183 all-skipped gate treats
# it as "not selected" (exempt) instead of an offender. On POSIX hosts
# the skipif does not fire and the test runs + gates normally.
pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "POSIX process-group reap — requires os.kill / "
            "signal.signal / os.killpg / start_new_session (unavailable "
            "on win32); see issue #239"
        ),
    ),
]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_wrapper_reaps_child_on_sigterm() -> None:
    """The stderr-filter wrapper must reap its child before it dies."""
    child_pid_file = Path(
        Path(__file__).resolve().parent / ".coprocess_reap_child_pid.test"
    )
    if child_pid_file.exists():
        child_pid_file.unlink()

    # Build the *actual* wrapper command the way the production code does,
    # injecting a long-lived child in place of mempalace.mcp_server.
    child_cmd = [sys.executable, "-c", _CHILD_CODE, str(child_pid_file)]
    python, args = _McpClient._filter_command(sys.executable, child_cmd=child_cmd)
    assert args and args[0] == "-c"

    wrapper = subprocess.Popen([python, *args])

    # Wait for the child to be up and recording its PID (wrapper alive too).
    child_pid = None
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if child_pid_file.exists():
            try:
                child_pid = int(child_pid_file.read_text().strip())
            except ValueError:
                child_pid = None
            if child_pid:
                break
        if wrapper.poll() is not None:
            raise AssertionError(
                f"wrapper exited before child started; rc={wrapper.returncode}"
            )
        time.sleep(0.05)

    assert child_pid and _pid_alive(child_pid), "child never came up / immediately died"

    trap_pid = None
    try:
        # Faithful host-side teardown: SIGTERM the wrapper *only* (not the group).
        os.kill(wrapper.pid, signal.SIGTERM)
        try:
            wrapper.wait(timeout=_WRAPPER_WAIT_S)
        except subprocess.TimeoutExpired:
            wrapper.kill()
            wrapper.wait(timeout=_WRAPPER_WAIT_S)

        time.sleep(_GRACE_S)

        if _pid_alive(child_pid):
            # Defensive: harvest the still-running child PID for the assertion.
            trap_pid = child_pid
            raise AssertionError(
                f"child pid={child_pid} was NOT reaped after wrapper SIGTERM "
                "(pre-fix orphan behaviour — see issue #239)"
            )
    finally:
        # Never leak a 300s sleeper into the rest of the suite.
        if trap_pid is not None:
            try:
                os.kill(trap_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if wrapper.poll() is None:
            wrapper.kill()
        try:
            if child_pid_file.exists():
                child_pid_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":  # pragma: no cover - convenience for manual runs
    import pytest

    raise SystemExit(pytest.main([__file__, "-x", "-q"]))
