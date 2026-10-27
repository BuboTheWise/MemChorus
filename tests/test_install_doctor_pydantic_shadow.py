"""Unit tests for issue #250 — ``install_doctor`` must distinguish

* **"pydantic not installed"** (distribution absent from
  ``importlib.metadata``) from
* **"pydantic present but unimportable"** — the distribution is registered in
  metadata (a version is resolvable) yet ``import pydantic`` raises
  ``ImportError``. This is the cross-ABI ``pydantic_core`` shadow: the C
  extension was built for a different Python than the one import resolves to,
  or a local ``pydantic`` file/directory shadows the real package.

TDD discipline (RED is mandatory first):

* ``test_present_but_unimportable_reports_shadow_not_missing`` must FAIL on the
  pre-fix doctor (which maps *any* ``ImportError`` onto the single flat string
  "pydantic not installed") and PASS after the fix emits a shadow / ABI
  diagnostic.
* ``test_genuinely_missing_still_reports_not_installed`` guards the opposite
  branch so the fix does not swallow the legitimate "not installed" diagnosis.

The scenarios are faked deterministically so they are independent of whatever
pydantic the test environment happens to carry:

* "import fails"     -> ``sys.modules["pydantic"] = None`` (CPython then raises
  ``ImportError: import of pydantic halted; None in sys.modules``) — the same
  pattern ``test_install_doctor.py::test_fail_when_pydantic_missing`` already uses.
* "metadata present"  -> ``importlib.metadata.version("pydantic")`` resolves.
* "metadata absent"   -> ``importlib.metadata.version("pydantic")`` raises
  ``PackageNotFoundError``.
"""

from __future__ import annotations

from unittest.mock import patch

import importlib.metadata as imp_meta

import memchorus.install_doctor as doctor
from memchorus.install_doctor import (
    FAIL,
    check_dependency_integrity,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _versions_present(name: str) -> str:
    """importlib.metadata sees both pydantic and PyYAML as installed."""
    return {
        "pydantic": "2.9.0",
        "pyyaml": "6.0.1",
    }[name.lower()]


def _pydantic_missing(name: str) -> str:
    """importlib.metadata does NOT resolve pydantic, but does resolve PyYAML."""
    if name.lower() == "pydantic":
        raise imp_meta.PackageNotFoundError(name)
    return "6.0.1"


# ---------------------------------------------------------------------------
# Issue #250 — present-but-unimportable must NOT be reported as "not installed"
# ---------------------------------------------------------------------------

def test_present_but_unimportable_reports_shadow_not_missing():
    # Faked scenario: the pydantic distribution is installed (version 2.9.0 is
    # resolvable in importlib.metadata) yet `import pydantic` raises ImportError
    # — the classic cross-ABI pydantic_core shadow.
    with patch.dict("sys.modules", {"pydantic": None}), patch.object(
        doctor.imp_meta, "version", side_effect=_versions_present
    ):
        r = check_dependency_integrity()

    # It *is* a failure — importing still breaks — so FAIL is correct.
    assert r.status == FAIL
    assert r.name == "dependency_integrity"

    # THE discriminator (issue #250): a present-but-unimportable pydantic must
    # NOT be misreported as "not installed".
    low = r.message.lower()
    assert "not installed" not in low, (
        "doctor conflated a present-but-unimportable pydantic with 'not "
        f"installed'; message was: {r.message!r}"
    )

    # ...and must instead name the shadow / ABI-mismatch, surface the real
    # ImportError detail, and carry an actionable hint.
    assert any(
        word in low
        for word in ("unimportable", "shadow", "abi", "cross-abi", "pydantic_core")
    ), f"expected a shadow/ABI diagnostic in message, got: {r.message!r}"
    assert r.hint, "shadow/ABI failure should carry a fix hint"


def test_genuinely_missing_still_reports_not_installed():
    # Guard: when the distribution is genuinely absent from importlib.metadata
    # AND the import fails, the doctor must keep the honest "not installed"
    # diagnosis (so we don't accidentally invert it into a shadow diagnostic).
    with patch.dict("sys.modules", {"pydantic": None}), patch.object(
        doctor.imp_meta, "version", side_effect=_pydantic_missing
    ):
        r = check_dependency_integrity()

    assert r.status == FAIL
    assert "not installed" in r.message.lower()
    assert "unimportable" not in r.message.lower()
    assert "shadow" not in r.message.lower()
