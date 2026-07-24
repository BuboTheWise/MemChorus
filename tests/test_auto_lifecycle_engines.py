"""Tests for memchorus.auto_lifecycle_engines — lifecycle auto-wiring module.

Covers:
  - auto_init_lifecycle(env-gated disabled by default)
  - auto_init_lifecycle(enabled=True via env var)
  - auto_init_lifecycle(enabled=True via config_override)
  - AutoLifecycleState immutability and defaults
  - get_lifecycle_state read-only probe
  - create_merge_engine_on convenience helper
  - Graceful degradation when orchestrator has no memory_sources
"""

import os
import unittest
from unittest import mock


class TestModuleImport(unittest.TestCase):
    """Basic import smoke test."""

    def test_import(self):
        from memchorus import auto_lifecycle_engines as ale
        self.assertTrue(hasattr(ale, "auto_init_lifecycle"))
        self.assertTrue(hasattr(ale, "AutoLifecycleState"))
        self.assertTrue(hasattr(ale, "get_lifecycle_state"))
        self.assertTrue(hasattr(ale, "create_merge_engine_on"))


class TestAutoLifecycleState(unittest.TestCase):
    """AutoLifecycleState dataclass behavior."""

    def setUp(self):
        from memchorus.auto_lifecycle_engines import AutoLifecycleState
        self.ALS = AutoLifecycleState

    def test_defaults_all_inactive(self):
        s = self.ALS()
        self.assertFalse(s.retention_active)
        self.assertFalse(s.eviction_active)
        self.assertFalse(s.merge_active)
        self.assertFalse(s.manager_active)
        self.assertFalse(s.scheduler_active)
        self.assertIsNone(s.orchestrator_id)
        self.assertEqual(s.sweep_interval_hours, 0.0)
        self.assertEqual(s.backend_sources, [])

    def test_frozen(self):
        s = self.ALS()
        with self.assertRaises(Exception):  # FrozenInstanceError
            s.retention_active = True  # type: ignore

    def test_str_returns_readable(self):
        s = self.ALS(retention_active=True, merge_active=True)
        t = str(s)
        self.assertIn("Retention", t)

    def test_custom_fields(self):
        s = self.ALS(
            orchestrator_id="O@0xdeadbeef",
            backend_sources=["hermes_default"],
        )
        self.assertEqual(s.orchestrator_id, "O@0xdeadbeef")
        self.assertIn("hermes_default", s.backend_sources)


class TestAutoInitDefaultDisabled(unittest.TestCase):
    """Lifecycle is off by default — opt-in only (backward compat §9)."""

    def test_disabled_when_env_unset(self):
        from memchorus.auto_lifecycle_engines import auto_init_lifecycle, AutoLifecycleState

        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("MEMCHORUS_LIFECYCLE_ENABLED", None)

        fake = object()
        state = auto_init_lifecycle(fake)

        self.assertIsInstance(state, AutoLifecycleState)
        self.assertFalse(state.manager_active)
        self.assertFalse(state.retention_active)

    def test_disabled_when_env_false(self):
        from memchorus.auto_lifecycle_engines import auto_init_lifecycle

        with mock.patch.dict(os.environ, {"MEMCHORUS_LIFECYCLE_ENABLED": "false"}):
            state = auto_init_lifecycle(object())

        self.assertFalse(state.manager_active)

    def test_enabled_via_env_true(self):
        from memchorus.auto_lifecycle_engines import auto_init_lifecycle

        with mock.patch.dict(os.environ, {"MEMCHORUS_LIFECYCLE_ENABLED": "true"}), \
             mock.patch("memchorus.auto_lifecycle_engines._lazy_lifecycle_manager") as lm, \
             mock.patch("memchorus.auto_lifecycle_engines._lazy_merge_engine") as me:

            # Mock manager creation — no failures
            LM = unittest.mock.MagicMock()
            SS = unittest.mock.MagicMock()
            resolve_cfg = lambda c: {"sweep_interval_seconds": 8 * 3600, **c}  # noqa: E731
            lm.return_value = (LM, SS, resolve_cfg)

            # Mock merge engine creation
            mg_cls = unittest.mock.MagicMock()
            mg_factory = lambda o, c: unittest.mock.MagicMock()  # noqa: E731
            me.return_value = (mg_cls, mg_factory)

            state = auto_init_lifecycle(object())

        self.assertTrue(state.manager_active)
        lm.assert_called_once()


class TestAutoInitConfigOverride(unittest.TestCase):
    """config_override.enabled=True activates lifecycle even without env var."""

    def test_config_enable(self):
        from memchorus.auto_lifecycle_engines import auto_init_lifecycle

        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("MEMCHORUS_LIFECYCLE_ENABLED", None)

        with mock.patch("memchorus.auto_lifecycle_engines._lazy_lifecycle_manager") as lm, \
             mock.patch("memchorus.auto_lifecycle_engines._lazy_merge_engine") as me:

            LM = unittest.mock.MagicMock()
            LM.return_value.config = {"sweep_interval_seconds": 0}
            SS = unittest.mock.MagicMock()
            resolve_cfg = lambda c: {"enabled": True, **c} if c else {"enabled": True}  # noqa: E731
            lm.return_value = (LM, SS, resolve_cfg)

            mg_cls, mg_factory = unittest.mock.MagicMock(), unittest.mock.MagicMock()
            me.return_value = (mg_cls, mg_factory)

            fake_orch = type("O", (), {"memory_sources": {"hermes_default": "x"}})()
            state = auto_init_lifecycle(fake_orch, config_override={"enabled": True})

        self.assertTrue(state.manager_active)
        self.assertIn("hermes_default", state.backend_sources)

    def test_explicit_disable_in_config(self):
        from memchorus.auto_lifecycle_engines import auto_init_lifecycle

        with mock.patch.dict(os.environ, {"MEMCHORUS_LIFECYCLE_ENABLED": "true"}):
            state = auto_init_lifecycle(object(), config_override={"enabled": False})

        self.assertFalse(state.manager_active)


class TestGetLifecycleState(unittest.TestCase):
    """Read-only status probe."""

    def test_no_lifecycle_returns_inactive(self):
        from memchorus.auto_lifecycle_engines import get_lifecycle_state

        bare = object()
        state = get_lifecycle_state(bare)

        self.assertFalse(state.manager_active)
        self.assertFalse(state.retention_active)
        self.assertIn("object", state.orchestrator_id)

    def test_with_manager_returns_active(self):
        from memchorus.auto_lifecycle_engines import (
            get_lifecycle_state,
            AutoLifecycleState,
        )

        mgr = unittest.mock.MagicMock()
        mgr.config = {"sweep_interval_seconds": 28800}
        mgr._get_retention_engine.return_value = unittest.mock.MagicMock()
        mgr._get_eviction_engine.return_value = unittest.mock.MagicMock()

        orch = unittest.mock.MagicMock()
        orch._lifecycle_manager = mgr
        orch._merge_engine = unittest.mock.MagicMock()
        orch.memory_sources = {"src1": "a", "src2": "b"}

        state = get_lifecycle_state(orch)

        self.assertTrue(state.manager_active)
        self.assertTrue(state.retention_active)
        self.assertTrue(state.eviction_active)
        self.assertTrue(state.merge_active)
        self.assertAlmostEqual(state.sweep_interval_hours, 8.0, delta=0.1)
        self.assertEqual(len(state.backend_sources), 2)


class TestCreateMergeEngineOn(unittest.TestCase):
    """Convenience merge-only helper."""

    def test_success_returns_engine(self):
        from memchorus.auto_lifecycle_engines import create_merge_engine_on

        with mock.patch(
            "memchorus.auto_lifecycle_engines._lazy_merge_engine"
        ) as me:
            mg_cls = unittest.mock.MagicMock()
            engine_instance = unittest.mock.MagicMock()
            factory = lambda o, c: engine_instance  # noqa: E731
            me.return_value = (mg_cls, factory)

            result = create_merge_engine_on(object())

        self.assertIsNotNone(result)

    def test_failure_returns_none(self):
        from memchorus.auto_lifecycle_engines import create_merge_engine_on

        with mock.patch(
            "memchorus.auto_lifecycle_engines._lazy_merge_engine"
        ) as me:
            me.side_effect = ImportError("nope")

        self.assertIsNone(create_merge_engine_on(object()))


class TestGracefulDegradation(unittest.TestCase):
    """Partial init still returns state with active engines."""

    def test_manager_fails_retention_still_probes(self):
        from memchorus.auto_lifecycle_engines import auto_init_lifecycle

        with mock.patch.dict(os.environ, {"MEMCHORUS_LIFECYCLE_ENABLED": "true"}), \
             mock.patch(
                 "memchorus.auto_lifecycle_engines._lazy_lifecycle_manager"
             ) as lm, \
             mock.patch(
                 "memchorus.auto_lifecycle_engines._lazy_merge_engine"
             ) as me:

            # LifecycleManager raises during init
            lm.return_value = (
                lambda *a, **k: (_ for _ in ()).throw(ImportError("broken")),
                None,
                lambda c: {"sweep_interval_seconds": 0},
            )
            mg_cls, mg_factory = unittest.mock.MagicMock(), unittest.mock.MagicMock()
            me.return_value = (mg_cls, mg_factory)

            state = auto_init_lifecycle(object())

        # Manager failed — but function returns without crashing
        self.assertFalse(state.manager_active)
        # Merge should still have been attempted
        me.assert_called_once()


if __name__ == "__main__":
    unittest.main()
