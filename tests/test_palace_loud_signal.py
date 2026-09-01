"""Operator-facing loud signal for the reader-side --palace auto-rewrite.

Remainder of GH-162: the v2.0.27 ``_normalize_palace_args`` workaround is
already locked behaviourally by :mod:`tests.test_palace_path_alignment`;
this test adds the *operator signal*: when the rewrite actually fires
(parent is an empty shell, the leaf ``palace/`` holds the real data), the
reader must emit a **WARNING**-level log line containing ``Rewrote --palace``
so repeated hits are visible in the operator's log, not buried at INFO level.

The function's external contract (return shape, rewritten value) must stay
byte-identical — only the log level + wording change (see #162 IMPL card).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from memchorus.mempalace_memory_source import _normalize_palace_args

_LOGGER_NAME = "memchorus.mempalace_memory_source"


@pytest.fixture
def shell_parent_layout(tmp_path: Path):
    """Exact Aug-20 layout: 0-byte shell at the parent, real leaf below it."""
    parent = tmp_path / ".mempalace"
    leaf = parent / "palace"
    leaf.mkdir(parents=True)
    (parent / "chroma.sqlite3").write_bytes(b"")
    (leaf / "chroma.sqlite3").write_bytes(b"real-data-marker")
    return parent, leaf


def test_rewrite_emits_warning_with_operator_guidance(
    shell_parent_layout, caplog: pytest.LogCaptureFixture
):
    """RED: passing a shell-parent path must log a WARNING containing
    ``Rewrote --palace`` plus guidance to point at the leaf directly."""
    parent, leaf = shell_parent_layout
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        out = _normalize_palace_args(["--palace", str(parent)])

    # Behaviour must be unchanged (return contract locked by
    # test_palace_path_alignment.py — re-asserted here for this log test).
    assert out == ["--palace", str(leaf)]

    loud = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Rewrote --palace" in r.getMessage()
    ]
    assert loud, (
        "expected a WARNING-level 'Rewrote --palace' log line when the "
        f"auto-rewrite fires; got records: "
        f"{[(r.levelno, r.getMessage()) for r in caplog.records]!r}"
    )
    msg = loud[0].getMessage()
    # Old and new path must both be visible so operators can audit the move.
    assert str(parent) in msg and str(leaf) in msg, msg
    # Guidance: point at the leaf dir directly.
    assert "leaf" in msg.lower(), msg


def test_equals_form_rewrite_is_likewise_loud(
    shell_parent_layout, caplog: pytest.LogCaptureFixture
):
    """The ``--palace=<path>`` equals form must log the same loud signal."""
    parent, leaf = shell_parent_layout
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        out = _normalize_palace_args([f"--palace={parent}"])

    assert out == [f"--palace={leaf}"]
    loud = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Rewrote --palace" in r.getMessage()
    ]
    assert loud, "equals-form rewrite must emit the same WARNING-level signal"


def test_no_rewrite_no_warning(shell_parent_layout, caplog):
    """No rewrite (already-correct leaf) → no ``Rewrote --palace`` warning."""
    _, leaf = shell_parent_layout
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _normalize_palace_args(["--palace", str(leaf)])

    loud = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Rewrote --palace" in r.getMessage()
    ]
    assert not loud, f"a no-op call must not warn; got: {loud!r}"
