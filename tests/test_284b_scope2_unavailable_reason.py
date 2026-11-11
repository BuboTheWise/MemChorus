"""GH-284 Issue B, Scope 2 — unavailable_reason() acceptance tests.

Each test covers a distinct failure class that the setup wizard or
dashboard must surface instead of a generic "check credentials" hint.

Scope-2 acceptance criteria (from the kanban card):
  * unavailable_reason() returns a non-None string naming the
    exact cause (missing-package, version-mismatch, stale-plugin-dir,
    store-path) when the provider is unavailable.
  * The returned string contains a concrete Fix: remediation.
  * No home-directory absolute paths (/home/...) or usernames
    appear in any returned diagnostic.
  * When the provider IS available, unavailable_reason()
    returns None.
  * is_available() and unavailable_reason() agree: if
    is_available() is True, unavailable_reason() must be None.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# --- path setup (mirror conftest.py) ----------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from integrations.provider import (  # noqa: E402
    EXPECTED_SDK_VERSION,
    MemChorusMemoryProvider,
    unavailable_reason,
)


def _make_hermes_home(tmp_path: Path) -> Path:
    """Create a minimal Hermes home directory structure."""
    plugins_dir = tmp_path / "plugins" / "memchorus"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "__init__.py").write_text("")
    return tmp_path


@pytest.fixture
def blocked_import():
    """Context fixture: temporarily make ``import memchorus`` fail.

    Setting sys.modules["memchorus"] = None makes the import machinery
    raise ImportError before any finder runs; mock.patch.dict restores
    the original module on exit so no source paths are touched.
    """
    import memchorus  # noqa: F401  ensure the real module is loaded first

    with mock.patch.dict(sys.modules, {"memchorus": None}):
        yield


class TestAvailableReturnsNone:
    """When memchorus is importable, unavailable_reason must be None."""

    def test_none_when_importable(self, tmp_path):
        home = _make_hermes_home(tmp_path)
        assert unavailable_reason(hermes_home=str(home)) is None

    def test_provider_method_none_when_importable(self, tmp_path):
        home = _make_hermes_home(tmp_path)
        provider = MemChorusMemoryProvider()
        assert provider.is_available() is True
        assert provider.unavailable_reason(hermes_home=str(home)) is None

    def test_is_available_and_reason_agree(self, tmp_path):
        """If is_available() is True, unavailable_reason() must be None."""
        home = _make_hermes_home(tmp_path)
        provider = MemChorusMemoryProvider()
        if provider.is_available():
            assert provider.unavailable_reason(hermes_home=str(home)) is None


class TestMissingPackage:
    """When the SDK cannot be imported, the diagnostic must name the cause."""

    def test_names_cause(self, tmp_path, blocked_import):
        home = _make_hermes_home(tmp_path)
        result = unavailable_reason(hermes_home=str(home))
        assert result is not None
        assert result.startswith("missing-package"), result
        assert "Fix:" in result
        assert "pip" in result and "install" in result

    def test_no_home_path_leak(self, tmp_path, blocked_import):
        home = _make_hermes_home(tmp_path)
        result = unavailable_reason(hermes_home=str(home))
        assert result is not None
        assert "/home/" not in result, f"home path leaked: {result}"

    def test_has_canonical_url(self, tmp_path, blocked_import):
        home = _make_hermes_home(tmp_path)
        result = unavailable_reason(hermes_home=str(home))
        assert result is not None
        assert "BuboTheWise" in result

    def test_provider_method(self, tmp_path, blocked_import):
        home = _make_hermes_home(tmp_path)
        provider = MemChorusMemoryProvider()
        assert provider.is_available() is False
        result = provider.unavailable_reason(hermes_home=str(home))
        assert result is not None
        assert result.startswith("missing-package")
        assert "Fix:" in result


class TestVersionMismatch:
    """When installed version does not equal expected, name both."""

    def test_names_both_versions(self, tmp_path):
        import memchorus
        home = _make_hermes_home(tmp_path)
        with mock.patch.object(memchorus, "__version__", "0.9.0"):
            result = unavailable_reason(
                hermes_home=str(home), expected_version="2.0.80"
            )

        assert result is not None
        assert result.startswith("version-mismatch"), result
        assert "0.9.0" in result
        assert "2.0.80" in result
        assert "Fix:" in result

    def test_version_match_returns_none(self, tmp_path):
        import memchorus
        home = _make_hermes_home(tmp_path)
        result = unavailable_reason(
            hermes_home=str(home),
            expected_version=memchorus.__version__,
        )
        assert result is None

    def test_no_version_attr(self, tmp_path):
        import memchorus
        home = _make_hermes_home(tmp_path)
        with mock.patch.dict(vars(memchorus), {"__version__": None}):
            result = unavailable_reason(
                hermes_home=str(home), expected_version="2.0.80"
            )

        assert result is not None
        assert "__version__" in result


class TestStalePluginDir:
    """A broken symlink at plugins/memchorus/ must be named."""

    def test_broken_symlink_named(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        os.symlink("/nonexistent/target/xyz", str(plugins_dir / "memchorus"))

        result = unavailable_reason(hermes_home=str(tmp_path))
        assert result is not None
        assert result.startswith("stale-plugin-dir"), result
        assert "Fix:" in result

    def test_broken_symlink_no_home_leak(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        os.symlink("/home/someuser/old-copy", str(plugins_dir / "memchorus"))

        result = unavailable_reason(hermes_home=str(tmp_path))
        assert result is not None
        assert result.startswith("stale-plugin-dir")
        assert "/home/someuser" not in result, f"home path leaked: {result}"


class TestStorePath:
    """A data_path pointing at a non-existent dir must be named."""

    def test_missing_store_path_named(self, tmp_path):
        import yaml
        plugins_dir = tmp_path / "plugins" / "memchorus"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        (plugins_dir / "__init__.py").write_text("")

        with open(str(tmp_path / "config.yaml"), "w") as f:
            yaml.safe_dump(
                {"memory": {"memchorus": {"data_path": "/no/such/store"}}}, f
            )

        result = unavailable_reason(hermes_home=str(tmp_path))
        assert result is not None
        assert result.startswith("store-path"), result
        assert "Fix:" in result
        assert "config.yaml" in result


class TestNoHomeLeak:
    """No diagnostic string may contain an absolute /home/ path."""

    @staticmethod
    def _check_no_home(result):
        if result is None:
            return
        assert "/home/" not in result, f"home path leaked: {result}"
        assert "bubo@" not in result, f"username leaked: {result}"

    def test_available_case(self, tmp_path):
        home = _make_hermes_home(tmp_path)
        self._check_no_home(unavailable_reason(hermes_home=str(home)))

    def test_version_mismatch_case(self, tmp_path):
        import memchorus
        home = _make_hermes_home(tmp_path)
        with mock.patch.object(memchorus, "__version__", "99.99.99"):
            self._check_no_home(
                unavailable_reason(
                    hermes_home=str(home), expected_version="1.0.0"
                )
            )

    def test_stale_plugin_case(self, tmp_path):
        tmp2 = tmp_path / "stale"
        (tmp2 / "plugins").mkdir(parents=True, exist_ok=True)
        os.symlink("/home/someuser/old", str(tmp2 / "plugins" / "memchorus"))
        self._check_no_home(unavailable_reason(hermes_home=str(tmp2)))


class TestExpectedVersion:
    def test_matches_sdk(self):
        import memchorus
        assert EXPECTED_SDK_VERSION == memchorus.__version__
