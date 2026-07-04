#!/usr/bin/env python3
"""
test_auto_bootstrap.py — MemChorus v1.2 auto-bootstrap tests (P0).

Covers lazy singleton creation (AC-A4), env disable master switch (AC-A1),
and graceful degradation when MemPalace probe fails.
"""

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def _clear():
    """Remove cached memchorus modules so bootstrap can re-fire."""
    for k in list(sys.modules):
        if k.startswith('memchorus'):
            del sys.modules[k]


# ── 1. Lazy singleton creation (AC-A4) ───────────────────────────────

def test_bootstrap_created_once():
    """First attribute access triggers bootstrap; subsequent accesses return same instance."""
    _clear()
    fake_orch = MagicMock(spec=['search', 'retrieve', 'save'])

    with patch('memchorus.auto_bootstrap.MemoryOrchestrator', return_value=fake_orch), \
         patch('memchorus.auto_bootstrap._load_yaml_config', return_value={}):
        import memchorus as mc

        first = mc._instance
        second = mc._instance

        assert first is not None, "Expected singleton on first access"
        assert first is second, "Expected cached instance on second access"


# ── 2. Env disable master switch (AC-A1) ────────────────────────────

def test_env_disable_prevents_bootstrap():
    """MEMCHORUS_AUTO_ENABLED=false -> import succeeds with no orchestrator created."""
    _clear()

    with patch.dict(os.environ, {"MEMCHORUS_AUTO_ENABLED": "false"}, clear=True), \
         patch('memchorus.auto_bootstrap.MemoryOrchestrator', side_effect=AssertionError("should not be called")):
        import memchorus as mc  # noqa: F811

        assert mc._instance is None, "Expected no orchestrator when auto-bootstrap disabled"


# ── 3. Graceful degradation on MemPalace failure (AC-A3) ─────────────

def test_mempalace_probe_failure_degrades_gracefully():
    """When MemPalace probe fails -> hermes_default only, warning logged, no crash."""
    _clear()

    # Simulate MemPalace constructor failing during the bootstrap probe step
    with patch('memchorus.mempalace_memory_source.MemPalaceMemorySource.__init__',
               side_effect=ConnectionError("MCP unreachable")), \
         patch.object(MagicMock, '__bool__', return_value=False):
        try:
            import memchorus as mc  # noqa: F811

            # Bootstrap should complete without propagating the exception up
            assert True, "Import succeeded despite MemPalace unreachability"
        except ConnectionError:
            assert False, "ConnectionError should NOT propagate past _bootstrap() boundary"


# ── 4. Config precedence: env var overrides YAML ─────────────────────

def test_env_var_overrides_yaml_config():
    """Env vars take precedence over YAML config values."""
    _clear()
    fake_orch = MagicMock(spec=['search', 'retrieve', 'save'])

    with patch('memchorus.auto_bootstrap.MemoryOrchestrator', return_value=fake_orch):
        # YAML says disabled, env says enabled — env wins
        with patch('memchorus.auto_bootstrap._load_yaml_config') as mock_yaml:
            mock_yaml.return_value = {"auto_enabled": False}

            with patch.dict(os.environ, {"MEMCHORUS_AUTO_ENABLED": "true"}, clear=True):
                import memchorus as mc  # noqa: F811

                assert mc._instance is not None, \
                    "Env should override YAML — bootstrap should proceed"


# ── 5. Hardcoded defaults used when no config present ────────────────

def test_hardcoded_defaults_when_no_config():
    """When no YAML file and no env vars set, hardcoded defaults from _DEFAULTS apply."""
    _clear()
    fake_orch = MagicMock(spec=['search', 'retrieve', 'save'])

    with patch('memchorus.auto_bootstrap.MemoryOrchestrator') as mock_cls:
        with patch('memchorus.auto_bootstrap._load_yaml_config', return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                import memchorus as mc  # noqa: F811

                assert mock_cls.call_count > 0, \
                    "Orchestrator should be created using hardcoded defaults"


# ── 6. Bootstrap returns None when orchestrator instantiation fails ───

def test_bootstrap_returns_none_on_orchestrator_failure():
    """If MemoryOrchestrator init raises an exception, _bootstrap returns None gracefully."""
    _clear()

    with patch('memchorus.auto_bootstrap.MemoryOrchestrator',
               side_effect=RuntimeError("orchestration unavailable")), \
         patch('memchorus.auto_bootstrap._load_yaml_config', return_value={}):
        import memchorus as mc  # noqa: F811

        assert mc._instance is None, "Expected graceful fallback when orchestrator fails"