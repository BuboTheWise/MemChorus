#!/usr/bin/env python3
"""
test_issue_325_routing_source.py — GH-325: routing.source marker injection.

A ``mempalace_routing`` section without ``routing.source`` is a silent
no-op fallback — wing-scoped writes succeed but land on wings that are
unresolvable by drawer/room queries.  These tests pin the fix:

1. ``ensure_routing_source()`` injects the marker when missing, preserves an
   explicit value, and reports non-dict inputs as False.
2. ``_DEFAULT_MEMPALACE_ROUTING`` carries ``routing.source: chroma``.
3. ``auto_bootstrap._bootstrap()`` produces an orchestrator config whose
   ``mempalace_routing`` section is marked active — both for legacy profiles
   (YAML section present without the marker) and when the section is absent
   (defaults apply).
4. ``auto_init.generate_config()`` output includes ``routing.source: chroma``.

Runs against live imports — no mocking beyond env / temp YAML.
"""

import json
import os
import sys
import unittest

# Ensure src/ is first on the path so memchorus resolves from this repo.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memchorus.auto_bootstrap import (  # noqa: E402
    _DEFAULT_MEMPALACE_ROUTING,
    _bootstrap,
    ensure_routing_source,
    ROUTING_SOURCE_CHROMA,
)
from memchorus.auto_init import generate_config  # noqa: E402


def _reset_bootstrap_state():
    import memchorus
    memchorus._bootstrap_done = False  # noqa: SLF001
    if hasattr(memchorus, "_instance"):
        delattr(memchorus, "_instance")


# --------------------------------------------------------------------------- #
# ensure_routing_source unit tests
# --------------------------------------------------------------------------- #

class TestEnsureRoutingSource(unittest.TestCase):
    """The injection helper: idempotent, non-destructive, honest."""

    def test_injects_when_missing(self):
        routing = {"wing_map": {"DECISION": ["decisions"]}}
        self.assertTrue(ensure_routing_source(routing))
        self.assertEqual(routing["routing.source"], "chroma")

    def test_injects_into_empty_dict(self):
        routing: dict = {}
        self.assertTrue(ensure_routing_source(routing))
        self.assertEqual(routing, {"routing.source": "chroma"})

    def test_preserves_existing_value(self):
        routing = {"routing.source": "sqlite"}
        self.assertTrue(ensure_routing_source(routing))
        self.assertEqual(routing["routing.source"], "sqlite")

    def test_rejects_non_dict_inputs(self):
        for bad in (None, "chroma", 42, ["wing_map"]):
            self.assertFalse(ensure_routing_source(bad))

    def test_idempotent(self):
        routing = {"wing_map": {}}
        ensure_routing_source(routing)
        before = dict(routing)
        ensure_routing_source(routing)
        self.assertEqual(routing, before)


# --------------------------------------------------------------------------- #
# Default constant
# --------------------------------------------------------------------------- #

class TestDefaultRoutingConstant(unittest.TestCase):
    def test_default_carries_marker(self):
        self.assertEqual(
            _DEFAULT_MEMPALACE_ROUTING.get("routing.source"), "chroma")

    def test_default_wing_map_intact(self):
        wm = _DEFAULT_MEMPALACE_ROUTING["wing_map"]
        self.assertEqual(wm["DECISION"], "memchorus_decisions")
        self.assertEqual(wm["LEARNING"], "memchorus_learning")
        self.assertEqual(wm["RESULT"], "memchorus_general")
        self.assertEqual(wm["default"], "memchorus_general")


# --------------------------------------------------------------------------- #
# Bootstrap integration
# --------------------------------------------------------------------------- #

class TestBootstrapInjection(unittest.TestCase):
    """_bootstrap() must produce an *active* routing section in all cases."""

    def setUp(self):
        _reset_bootstrap_state()
        self._saved_cfg = os.environ.pop("MEMCHORUS_CONFIG", None)
        self._saved_enabled = os.environ.pop("MEMCHORUS_AUTO_ENABLED", None)

    def tearDown(self):
        for key, val in (("MEMCHORUS_CONFIG", self._saved_cfg),
                         ("MEMCHORUS_AUTO_ENABLED", self._saved_enabled)):
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        _reset_bootstrap_state()

    def _routing_section(self):
        result = _bootstrap()
        assert result is not None, (
            "bootstrap returned None — check MEMCHORUS_AUTO_ENABLED")
        mp_cfg = result.config.get("mempalace_config")
        assert isinstance(mp_cfg, dict), (
            "orchestrator config missing mempalace_config")
        routing = mp_cfg.get("mempalace_routing")
        assert isinstance(routing, dict), (
            "orchestrator config missing mempalace_routing")
        return routing

    def test_legacy_yaml_without_marker_gets_injected(self):
        """Pre-fix profile config: mempalace_routing present, no routing.source.

        This is exactly the cthugha/bubo state that produced the silent
        no-op wing routing — the marker must be injected at bootstrap time.
        """
        os.environ["MEMCHORUS_CONFIG"] = json.dumps({
            "mempalace_config": {
                "mempalace_routing": {"wing_map": {"DECISION": ["decisions"]}}
            }
        })
        routing = self._routing_section()
        self.assertEqual(routing.get("routing.source"), "chroma")
        # Existing wing_map must survive the injection.
        self.assertEqual(routing.get("wing_map", {}).get("DECISION"),
                         ["decisions"])

    def test_default_routing_applies_when_section_absent(self):
        routing = self._routing_section()
        self.assertEqual(routing.get("routing.source"), "chroma")
        self.assertIn("wing_map", routing)

    def test_existing_marker_preserved(self):
        """nyarlathotep/grok shape: marker already present — never clobber."""
        os.environ["MEMCHORUS_CONFIG"] = json.dumps({
            "mempalace_config": {
                "mempalace_routing": {
                    "routing.source": "chroma",
                    "wing_map": {"DECISION": ["decisions"]},
                }
            }
        })
        routing = self._routing_section()
        self.assertEqual(routing.get("routing.source"), "chroma")


# --------------------------------------------------------------------------- #
# auto_init.generate_config
# --------------------------------------------------------------------------- #

class TestAutoInitGenerateConfig(unittest.TestCase):
    def _parsed(self, profile=None):
        import yaml
        text = generate_config(profile=profile)
        data = yaml.safe_load(text)
        return data

    def test_named_profile_includes_routing_source(self):
        data = self._parsed("testprof")
        routing = data["mempalace_config"]["mempalace_routing"]
        self.assertEqual(routing["routing.source"], "chroma")
        self.assertIn("wing_map", routing)

    def test_default_profile_includes_routing_source(self):
        data = self._parsed()
        routing = data["mempalace_config"]["mempalace_routing"]
        self.assertEqual(routing["routing.source"], "chroma")
        self.assertIn("wing_map", routing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
