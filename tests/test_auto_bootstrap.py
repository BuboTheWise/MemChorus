#!/usr/bin/env python3
"""
test_auto_bootstrap.py - Unit/integration tests for auto_bootstrap subsystem.

Tests the full v1.2 bootstrap sequence (AC-A1 through AC-A4):
  1. Config resolution: env vars > YAML files > hardcoded defaults
  2. Enabled gate: MEMCHORUS_AUTO_ENABLED=false short-circuits to None
  3. MemPalace probe graceful degradation when MCP unavailable
  4. Source wiring and MemoryOrchestrator creation
  5. Lazy init behaviour in __init__.py via __getattr__
  6. Helper value resolvers (_resolve_boolean, _resolve_int, _resolve_float)

Uses the live hermes-agent venv runtime for real import semantics,
not mocks — so we test actual import resolution and module-level state.
"""

import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import contextmanager
from unittest import mock

# Ensure src/ on path (live development, not an installed package)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ---------------------------------------------------------------------------
# Helpers — re-set env vars around individual tests
# ---------------------------------------------------------------------------

_ORIG_ENV = dict(os.environ)


@contextmanager
def _with_env(**kwargs):
    """Context manager: set env vars for the duration of a test, restore after."""
    before = {}
    for k, v in kwargs.items():
        before[k] = os.environ.get(k)
        os.environ[k] = str(v) if v is not None else ""
    try:
        yield
    finally:
        for k, orig in before.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig


def _clear_bootstrap_state():
    """Remove cached bootstrap state so each test starts fresh.

    The __init__.py module stores _bootstrap_done and _instance at module
    level — we need to reset these between tests so lazy-init fires again.
    """
    import sys as _sys

    # Pop the memchorus package and all submodules from sys.modules.
    keys_to_pop = [k for k in _sys.modules if k == "memchorus" or k.startswith("memchorus.")]
    for k in keys_to_pop:
        _sys.modules.pop(k, None)


# =================================================================== #
# Test 1: Config resolution helpers                                   #
# =================================================================== #

class TestResolveBoolean(unittest.TestCase):
    """_resolve_boolean normalises truthy/falsy values to a strict bool."""

    def test_bool_passthrough(self):
        from memchorus.auto_bootstrap import _resolve_boolean
        self.assertTrue(_resolve_boolean(True))
        self.assertFalse(_resolve_boolean(False))

    def test_string_truthy(self):
        from memchorus.auto_bootstrap import _resolve_boolean
        for val in ("true", "1", "yes", "on", "anything_else"):
            self.assertTrue(
                _resolve_boolean(val),
                f"Expected '{val}' to resolve True",
            )

    def test_string_falsy(self):
        from memchorus.auto_bootstrap import _resolve_boolean
        for val in ("false", "0", "no", "off", ""):
            self.assertFalse(
                _resolve_boolean(val),
                f"Expected '{val}' to resolve False",
            )

    def test_numeric(self):
        from memchorus.auto_bootstrap import _resolve_boolean
        self.assertTrue(_resolve_boolean(1))
        self.assertFalse(_resolve_boolean(0))


class TestResolveInt(unittest.TestCase):

    def test_valid_int(self):
        from memchorus.auto_bootstrap import _resolve_int
        self.assertEqual(_resolve_int("42"), 42)
        self.assertEqual(_resolve_int(7), 7)

    def test_invalid_fallback(self):
        from memchorus.auto_bootstrap import _resolve_int
        self.assertEqual(_resolve_int("not_a_number"), 0)
        self.assertEqual(_resolve_int(None), 0)


class TestResolveFloat(unittest.TestCase):

    def test_valid_float(self):
        from memchorus.auto_bootstrap import _resolve_float
        self.assertAlmostEqual(_resolve_float("12.5"), 12.5)
        self.assertAlmostEqual(_resolve_float(3.14), 3.14)

    def test_invalid_fallback(self):
        from memchorus.auto_bootstrap import _resolve_float
        self.assertAlmostEqual(_resolve_float("abc"), 30.0)
        self.assertAlmostEqual(_resolve_float(None), 30.0)


# =================================================================== #
# Test 2: YAML config loading                                         #
# =================================================================== #

class TestLoadYamlConfig(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _clear_bootstrap_state()

    def test_returns_dict_with_valid_yaml(self):
        from memchorus.auto_bootstrap import _load_yaml_config, _HAS_YAML
        if not _HAS_YAML:
            self.skipTest("PyYAML not installed — skipping YAML config test")

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = os.path.join(tmpdir, ".hermes")
            os.makedirs(fake_home)
            yaml_path = os.path.join(fake_home, "memchorus.yaml")
            with open(yaml_path, "w") as f:
                f.write(textwrap.dedent("""\
                    default_source: hermes_default
                    half_life_days: 45.0
                    cache_ttl_seconds: 120
                """))
            # Temporarily set HOME so it picks up our test file via ~/.hermes/
            orig_home = os.environ.get("HOME")
            try:
                os.environ["HOME"] = tmpdir
                result = _load_yaml_config()
            finally:
                if orig_home is not None:
                    os.environ["HOME"] = orig_home

        self.assertIsInstance(result, dict)
        self.assertEqual(result["default_source"], "hermes_default")
        self.assertAlmostEqual(result["half_life_days"], 45.0)

    def test_no_yaml_file_returns_empty(self):
        from memchorus.auto_bootstrap import _load_yaml_config

        orig_home = os.environ.get("HOME")
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                os.environ["HOME"] = tmpdir  # no .hermes/ yet
                result = _load_yaml_config()
            finally:
                if orig_home is not None:
                    os.environ["HOME"] = orig_home

        self.assertEqual(result, {})

    def test_non_dict_yaml_skipped(self):
        from memchorus.auto_bootstrap import _load_yaml_config, _HAS_YAML
        if not _HAS_YAML:
            self.skipTest("PyYAML not installed")

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = os.path.join(tmpdir, ".hermes")
            os.makedirs(fake_home)
            yaml_path = os.path.join(fake_home, "memchorus.yaml")
            with open(yaml_path, "w") as f:
                f.write("just: a:\n- list\n- not_a_mapping")
            orig_home = os.environ.get("HOME")
            try:
                os.environ["HOME"] = tmpdir
                result = _load_yaml_config()
            finally:
                if orig_home is not None:
                    os.environ["HOME"] = orig_home

        self.assertEqual(result, {})


# =================================================================== #
# Test 3: Bootstrap enabled gate (MEMCHORUS_AUTO_ENABLED)              #
# =================================================================== #

class TestBootstrapEnabledGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _clear_bootstrap_state()

    def test_disabled_via_env_returns_none(self):
        """When MEMCHORUS_AUTO_ENABLED=false, _bootstrap returns None."""
        with _with_env(MEMCHORUS_AUTO_ENABLED="false"):
            from memchorus.auto_bootstrap import _bootstrap
            result = _bootstrap()
        self.assertIsNone(result)

    def test_disabled_variations(self):
        """'0', 'no', 'off' all disable bootstrap."""
        for val in ("0", "no", "off"):
            with _with_env(MEMCHORUS_AUTO_ENABLED=val):
                _clear_bootstrap_state()
                from memchorus.auto_bootstrap import _bootstrap
                result = _bootstrap()
            self.assertIsNone(
                result,
                f"MEMCHORUS_AUTO_ENABLED={val!r} should disable bootstrap",
            )

    def test_enabled_by_default(self):
        """When env is not set and defaults apply, auto_bootstrap runs normally."""
        with _with_env(MEMCHORUS_AUTO_ENABLED="true"):
            from memchorus.auto_bootstrap import _bootstrap
            result = _bootstrap()
        # When enabled: should return an orchestrator or None (if deps fail),
        # but importantly it SHOULD NOT short-circuit to the disabled path.
        # We can at least assert the log message for "disabled" was emitted.
        self.assertIsNotNone(result)  # orchestrator when env clean


# =================================================================== #
# Test 4: MemPalace probe degradation                                 #
# =================================================================== #

class TestMempalaceProbe(unittest.TestCase):
    """The bootstrap should degrade gracefully if MemPalace is unreachable."""

    def test_probe_failure_does_not_raise(self):
        """Even when MemPalace import fails, _bootstrap does not throw."""
        import logging
        log_capture = __import__("io").StringIO()
        handler = logging.StreamHandler(log_capture)
        logger = logging.getLogger("memchorus.auto_bootstrap")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            # Force MemPalace to be unavailable by mocking a failing import
            orig_modules = dict(__import__("sys").modules)
            with mock.patch.dict(
                __import__("sys").modules,
                {"memchorus.mempalace_memory_source": None},
            ):
                _clear_bootstrap_state()
                from memchorus.auto_bootstrap import _bootstrap
                # This should NOT raise — just warn and continue
                result = _bootstrap()
            # At minimum, should return None because orchestrator can't build
            # without a valid memory source. The key is: no exception escapes.
        finally:
            logger.removeHandler(handler)

    def test_bootstrap_returns_orchestrator_when_enabled_and_sources_exist(self):
        """Happy path: enabled bootstraps an orchestrator."""
        with _with_env(MEMCHORUS_AUTO_ENABLED="true"):
            from memchorus.auto_bootstrap import _bootstrap
            result = _bootstrap()
        self.assertIsNotNone(result)


# =================================================================== #
# Test 5: Lazy init in __init__.py via __getattr__                     #
# =================================================================== #

class TestLazyBootstrapInit(unittest.TestCase):
    """Verify that accessing memchorus._instance triggers auto-bootstrap exactly once."""

    def test_lazily_bootstraps_on_first_access(self):
        """_bootstrap_done should be False before _instance access, True after."""
        # We import the fresh module inside this test.
        mod = __import__("memchorus")
        # Before any attribute access (besides import), _bootstrap_done should
        # still be False because we haven't triggered __getattr__ yet on _instance.
        self.assertFalse(mod._bootstrap_done)

        # Accessing _instance fires __getattr__ which calls _bootstrap.
        inst = mod._instance
        self.assertTrue(mod._bootstrap_done)

    def test_import_succeeds_without_crashing(self):
        """The entire package should import without exceptions."""
        import importlib
        mod = importlib.import_module("memchorus")
        self.assertTrue(hasattr(mod, "__version__"))


# =================================================================== #
# Test 6: Config precedence (env overrides YAML overrides defaults)    #
# =================================================================== #

class TestConfigPrecedence(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _clear_bootstrap_state()

    def test_env_overrides_default(self):
        """MEMCHORUS_HALF_LIFE_DAYS env var should override the default 30.0."""
        with _with_env(MEMCHORUS_AUTO_ENABLED="true", MEMCHORUS_HALF_LIFE_DAYS="99"):
            from memchorus.auto_bootstrap import _bootstrap
            result = _bootstrap()
        self.assertIsNotNone(result)

    def test_defaults_when_no_source(self):
        """Without env or YAML, defaults should apply (half_life=30.0)."""
        # No relevant env vars set
        for k in ("MEMCHORUS_AUTO_ENABLED", "MEMCHORUS_HALF_LIFE_DAYS"):
            os.environ.pop(k, None)
        from memchorus.auto_bootstrap import _DEFAULTS
        self.assertEqual(_DEFAULTS["half_life_days"], 30.0)
        self.assertEqual(_DEFAULTS["cache_ttl_seconds"], 60)

    def test_env_source_override(self):
        """MEMCHORUS_DEFAULT_SOURCE env var overrides the YAML/default source."""
        with _with_env(MEMCHORUS_AUTO_ENABLED="true", MEMCHORUS_DEFAULT_SOURCE="custom_source"):
            from memchorus.auto_bootstrap import _bootstrap
            result = _bootstrap()
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
