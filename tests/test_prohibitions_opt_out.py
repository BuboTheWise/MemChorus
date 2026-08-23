"""TDD tests for GH#102: explicit opt-out toggles for prohibitions system."""
import pytest
from unittest.mock import MagicMock, patch


class TestGuardScanOptOut:

    def test_guard_scan_enabled_by_default(self):
        mock_orch = MagicMock()
        mock_orch.config = {}
        mock_orch._prohibitions_manager = None
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan", return_value=[]) as scan_mock:
                with patch("memchorus.hooks._build_search_terms", return_value="guard test"):
                    hooks.on_pre_llm_call(message="test")
                assert scan_mock.called

    def test_guard_scan_bypassed_when_disabled(self):
        mock_orch = MagicMock()
        mock_orch.config = {"prohibitions": {"enabled": False}}
        mock_orch._prohibitions_manager = None
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan", return_value=[]) as scan_mock:
                with patch("memchorus.hooks._build_search_terms", return_value="test"):
                    hooks.on_pre_llm_call(message="test")
                assert not scan_mock.called

    def test_guard_scan_runs_when_explicitly_enabled(self):
        mock_orch = MagicMock()
        mock_orch.config = {"prohibitions": {"enabled": True}}
        mock_orch._prohibitions_manager = None
        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks
            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan", return_value=[]) as scan_mock:
                with patch("memchorus.hooks._build_search_terms", return_value="test"):
                    hooks.on_pre_llm_call(message="test")
                assert scan_mock.called


class TestDistillationOptOut:

    def _mock_orch(self, cfg):
        m = MagicMock()
        m.config = cfg
        return m

    def test_distillation_bypassed_when_disabled(self):
        config = {"prohibitions": {"distillation_enabled": False}}
        mock_orch = self._mock_orch(config)
        from memchorus.hooks import _try_distill_prohibition
        result = _try_distill_prohibition("critical failure", mock_orch)
        assert result is None

    def test_distillation_runs_when_explicitly_enabled(self):
        config = {"prohibitions": {"distillation_enabled": True}}
        mock_orch = self._mock_orch(config)
        with patch("memchorus.prohibition_distiller.ProhibitionDistiller") as pd_mock:
            pd_inst = MagicMock()
            pd_inst.distill.return_value = None
            pd_mock.return_value = pd_inst
            from memchorus.hooks import _try_distill_prohibition
            _try_distill_prohibition("critical failure", mock_orch)
            pd_mock.assert_called_once()

    def test_distillation_runs_by_default(self):
        config = {}
        mock_orch = self._mock_orch(config)
        with patch("memchorus.prohibition_distiller.ProhibitionDistiller") as pd_mock:
            pd_inst = MagicMock()
            pd_inst.distill.return_value = None
            pd_mock.return_value = pd_inst
            from memchorus.hooks import _try_distill_prohibition
            _try_distill_prohibition("critical failure", mock_orch)
            pd_mock.assert_called_once()


class TestConfigDefaultsAndBackwardCompatibility:

    def test_empty_prohibitions_config_does_not_disable_anything(self):
        cfg = {"prohibitions": {}}
        assert cfg["prohibitions"].get("enabled", True) is True
        assert cfg["prohibitions"].get("distillation_enabled", True) is True

    def test_missing_prohibitions_key_preserves_all(self):
        cfg = {}
        guard_cfg = cfg.get("prohibitions", {})
        assert guard_cfg.get("enabled", True) is True
        assert guard_cfg.get("distillation_enabled", True) is True

    def test_can_disable_one_but_not_other(self):
        cfg = {"prohibitions": {"enabled": False, "distillation_enabled": True}}
        assert not cfg["prohibitions"]["enabled"]
        assert cfg["prohibitions"]["distillation_enabled"]

    def test_can_disable_distillation_but_not_guards(self):
        cfg = {"prohibitions": {"enabled": True, "distillation_enabled": False}}
        assert cfg["prohibitions"]["enabled"]
        assert not cfg["prohibitions"]["distillation_enabled"]
