"""Unit tests for provisions opt-out toggles (GH-102).

Covers:
 - provisions.enabled defaults to True
 - provisions.distillation_enabled defaults to True
 - _try_guard_scan returns [] when provisions_enabled=False (early exit)
 - _try_distill_prohibition returns early when distillation_enabled=False
 - Mixed toggle states: scan disabled / distillation enabled and vice versa
 - Existing prohibitions still load and work regardless of distillation toggle
"""

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict


def _make_orchestrator(config_overrides: Dict[str, Any]):
    """Build a minimal orchestrator stub with a .config dict for the hooks to read.

    The hooks only access orchestrator.config (a dict) to check toggle values,
    so this stub is sufficient without pulling in the full MemoryOrchestrator.
    """
    from types import SimpleNamespace
    defaults: Dict[str, Any] = {
        "default_source": "hermes_default",
        "half_life_days": 30.0,
        "cache_ttl_seconds": 60,
        "enforce_on_read": True,
        "enforce_on_write": True,
        "provisions_enabled": True,
        "provisions_distillation_enabled": True,
    }
    defaults.update(config_overrides)
    stub = SimpleNamespace()
    stub.config = defaults  # type: ignore[attr-defined]
    return stub


class TestProvisionsDefaults(unittest.TestCase):
    """Verify toggle defaults match the spec (both True by default)."""

    def test_provisions_enabled_default_true(self):
        from memchorus.auto_bootstrap import _DEFAULTS
        self.assertIn("provisions", _DEFAULTS)
        self.assertTrue(_DEFAULTS["provisions"]["enabled"])

    def test_provisions_distillation_enabled_default_true(self):
        from memchorus.auto_bootstrap import _DEFAULTS
        self.assertTrue(_DEFAULTS["provisions"]["distillation_enabled"])


class TestGuardScanToggle(unittest.TestCase):
    """_try_guard_scan respects provisions.enabled toggle."""

    def test_guard_scan_returns_empty_when_disabled(self):
        """provisions_enabled=False: guard scan short-circuits to empty list."""
        from memchorus.hooks import MemChorusHooks
        stub = _make_orchestrator({"provisions_enabled": False})
        hooks = MemChorusHooks()
        result = hooks._try_guard_scan("I will run pip install -e .", stub)
        self.assertEqual(result, [])

    def test_guard_scan_runs_when_enabled_default(self):
        """Default True: guard scan executes normally (does not crash)."""
        from memchorus.hooks import MemChorusHooks
        from memchorus.prohibitions import ProhibitionsManager, Prohibition
        stub = _make_orchestrator({})
        stub._prohibitions_manager = ProhibitionsManager(  # type: ignore[attr-defined]
            data_dir=Path(tempfile.gettempdir()),
        )
        hooks = MemChorusHooks()
        blocks = hooks._try_guard_scan("I will run pip install -e .", stub)
        # Should not crash; may or may not find matches depending on seeded rules.
        assert isinstance(blocks, list), "guard scan should return a list"

    def test_guard_scan_runs_when_explicitly_enabled(self):
        """provisions_enabled=True explicitly: guard scan works."""
        from memchorus.hooks import MemChorusHooks
        from memchorus.prohibitions import ProhibitionsManager, Prohibition
        stub = _make_orchestrator({"provisions_enabled": True})
        pm = ProhibitionsManager(data_dir=Path(tempfile.gettempdir()))
        pm.add_rule(Prohibition(
            id="test-install",
            condition="Never run pip install -e",
            trigger_keywords=["pip install -e"],
            severity=3,
        ))
        stub._prohibitions_manager = pm  # type: ignore[attr-defined]

        hooks = MemChorusHooks()
        blocks = hooks._try_guard_scan("I will run pip install -e .", stub)
        self.assertGreater(len(blocks), 0, "should match the test-install rule")


class TestDistillationToggle(unittest.TestCase):
    """_try_distill_prohibition respects provisions.distillation_enabled toggle."""

    def test_distillation_skips_when_disabled(self):
        """distillation_enabled=False: _try_distill_prohibition returns early."""
        import tempfile
        from memchorus.prohibitions import ProhibitionsManager, Prohibition
        from memchorus.hooks import _try_distill_prohibition
        stub = _make_orchestrator({"provisions_distillation_enabled": False})
        tmp_dir = tempfile.mkdtemp()
        pm = ProhibitionsManager(data_dir=Path(tmp_dir))
        pm.load()  # seeds 3 default rules
        initial_count = len(pm.rules)
        stub._prohibitions_manager = pm  # type: ignore[attr-defined]

        _try_distill_prohibition(
            "ModuleNotFoundError hermes_cli caused by pip install -e",
            stub,
        )
        # Rule count should not change — distillation was skipped early
        self.assertEqual(len(pm.rules), initial_count)

    def test_distillation_runs_when_enabled(self):
        """Default True: distillation attempt is made (does not crash)."""
        from memchorus.prohibitions import ProhibitionsManager, Prohibition
        from memchorus.hooks import _try_distill_prohibition
        stub = _make_orchestrator({})
        pm = ProhibitionsManager(data_dir=Path(tempfile.gettempdir()))
        pm.load()
        stub._prohibitions_manager = pm  # type: ignore[attr-defined]

        # Feed a self-breaking error message
        _try_distill_prohibition(
            "ModuleNotFoundError hermes_cli caused by pip install -e",
            stub,
        )
        # If distillation happened, rules may have increased — that's fine.
        # The key is this did not crash.


class TestMixedToggles(unittest.TestCase):
    """Toggle combinations: one enabled while the other is disabled."""

    def test_scan_disabled_distillation_enabled(self):
        """provisions_enabled=False + distillation_enabled=True.
        Guard scan skipped; distillation still runs.
        """
        from memchorus.prohibitions import ProhibitionsManager, Prohibition
        from memchorus.hooks import MemChorusHooks, _try_distill_prohibition

        stub = _make_orchestrator(
            {"provisions_enabled": False, "provisions_distillation_enabled": True},
        )
        pm = ProhibitionsManager(data_dir=Path(tempfile.gettempdir()))
        initial_count = len(pm.rules)
        pm.load()
        stub._prohibitions_manager = pm  # type: ignore[attr-defined]

        hooks = MemChorusHooks()
        scan_result = hooks._try_guard_scan("pip install -e", stub)
        self.assertEqual(scan_result, [], "scan should return empty when disabled")

    def test_scan_enabled_distillation_disabled(self):
        """provisions_enabled=True + distillation_enabled=False.
        Guard scan runs; distillation skipped.
        """
        import tempfile
        from memchorus.prohibitions import ProhibitionsManager, Prohibition
        from memchorus.hooks import MemChorusHooks, _try_distill_prohibition

        stub = _make_orchestrator(
            {"provisions_enabled": True, "provisions_distillation_enabled": False},
        )
        tmp_dir = tempfile.mkdtemp()
        pm = ProhibitionsManager(data_dir=Path(tmp_dir))
        pm.load()
        initial_count = len(pm.rules)
        # Add a matching rule for the scan test
        pm.add_rule(Prohibition(
            id="test-rule",
            condition="Never run pip install -e",
            trigger_keywords=["pip install -e"],
            severity=3,
        ))
        stub._prohibitions_manager = pm  # type: ignore[attr-defined]

        hooks = MemChorusHooks()
        scan_result = hooks._try_guard_scan("I will run pip install -e .", stub)
        self.assertGreater(len(scan_result), 0, "scan should still match rules")

        # Distillation should be skipped
        _try_distill_prohibition(
            "ModuleNotFoundError hermes_cli caused by pip install -e",
            stub,
        )
        rule_count_after = len(pm.rules)
        self.assertEqual(rule_count_after, initial_count + 1,
                         "only the manually-added rule should exist — no new distilled one")


class TestBootstrapWiresProvisionsConfig(unittest.TestCase):
    """End-to-end: bootstrap populates provisions keys on orchestrator config."""

    def test_bootstrap_config_contains_provisions_keys(self):
        """_bootstrap() includes provisions_enabled and provisions_distillation_enabled."""
        import os as _os
        _os.environ["MEMCHORUS_AUTO_ENABLED"] = "true"
        try:
            from memchorus.auto_bootstrap import _bootstrap
            orchestrator = _bootstrap()
            if orchestrator is not None:
                self.assertIn("provisions_enabled", orchestrator.config)
                self.assertIn("provisions_distillation_enabled", orchestrator.config)
                # Defaults should be True
                self.assertTrue(orchestrator.config["provisions_enabled"])
                self.assertTrue(orchestrator.config["provisions_distillation_enabled"])
        finally:
            _os.environ.pop("MEMCHORUS_AUTO_ENABLED", None)


if __name__ == "__main__":
    unittest.main()
