"""GH-#299 — ``check_memory_source_registration`` must probe the **actual**
registration mechanism, not a class-level ``_registry``/``subclasses``
attribute that ``MemorySource`` (an ABC) never defined.

MemChorus registration is *explicit*: the orchestrator
(``MemChorusOrchestrator._initialize_default_sources``) instantiates the
concrete sources directly — ``HermesDefaultMemorySource`` always,
``MemPalaceMemorySource`` / ``SessionSearchMemorySource`` best-effort — and
exposes ``register_source()`` for custom sources. The v2.0.80 probe checked
``hasattr(MemorySource, "_registry") or hasattr(MemorySource, "subclasses")``;
neither attribute exists, so **every** healthy install (e.g. the clean 2.0.80
@ 797c9ec6 install on the ``cthugha`` profile, where the live MCP round-trip
proves the MemPalace source is registered and functional) was flagged with a
permanent false WARN.

The post-fix probe aligns with reality: it imports the concrete source
modules (best-effort import failures tolerated, mirroring the orchestrator's
degraded-but-operational behavior) and then asserts the standard Python
subclass registry ``MemorySource.__subclasses__()`` is non-empty — i.e. at
least one concrete source actually exists to be registered.

RED on the pre-fix head (``healthy`` hits the absent ``_registry``/``subclasses``
attributes and returns the false WARN); GREEN after the fix.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from memchorus.install_doctor import (
    FAIL,
    PASS,
    WARN,
    check_memory_source_registration,
)
from memchorus.memory_source import MemorySource


# ---------------------------------------------------------------------------
# healthy install — the #299 acceptance: no false warning
# ---------------------------------------------------------------------------

class TestHealthyInstall:
    def test_healthy_install_reports_pass(self):
        """A healthy install (concrete sources importable) must be PASS —
        not the permanent false WARN of the pre-fix probe."""
        r = check_memory_source_registration()
        assert r.status == PASS, (
            f"expected PASS on a healthy install, got {r.status}: {r.message}"
        )
        assert r.name == "memory_source_registration"

    def test_pass_message_names_registered_sources(self):
        """The PASS message must reflect what is actually registered —
        the resilient core (hermes_default) is present on every healthy
        install, so its class must be named in the report."""
        r = check_memory_source_registration()
        assert "HermesDefaultMemorySource" in r.message

    def test_all_known_concrete_sources_are_subclasses(self):
        """The probe's source list matches the concrete sources the
        orchestrator instantiates (sanity: the modules exist and subclass
        the base)."""
        from memchorus.hermes_memory_source import HermesDefaultMemorySource
        from memchorus.session_search_memory_source import (
            SessionSearchMemorySource,
        )

        direct = MemorySource.__subclasses__()
        assert HermesDefaultMemorySource in direct
        assert SessionSearchMemorySource in direct

    def test_base_abc_has_no_registry_attribute(self):
        """Document the pre-fix root cause: ``MemorySource`` carries neither
        attribute the old probe checked, which is exactly why it warned on
        every healthy install."""
        assert not hasattr(MemorySource, "_registry")
        assert not hasattr(MemorySource, "subclasses")


# ---------------------------------------------------------------------------
# degraded — base present, but no concrete source importable
# ---------------------------------------------------------------------------

class TestNoConcreteSubclass:
    def test_base_present_but_no_subclass_warns(self):
        """If every concrete source module fails to import (broken install),
        the check must WARN — a genuinely degraded state, distinguishable
        from the pre-fix *false* warning."""
        with patch(
            "importlib.import_module", side_effect=ImportError("gone")
        ), patch.object(
            MemorySource, "__subclasses__", staticmethod(lambda: [])
        ):
            r = check_memory_source_registration()
        assert r.status == WARN
        assert "no concrete" in r.message.lower()
        assert r.hint


# ---------------------------------------------------------------------------
# broken — base class itself unimportable
# ---------------------------------------------------------------------------

class TestBaseNotImportable:
    def test_base_import_failure_is_fail(self):
        with patch.dict("sys.modules", {"memchorus.memory_source": None}):
            r = check_memory_source_registration()
        assert r.status == FAIL
        assert "Cannot import MemorySource" in r.message
        assert r.hint
