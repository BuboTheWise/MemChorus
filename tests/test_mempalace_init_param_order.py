"""Regression tests: MemPalaceMemorySource.__init__ parameter order.

The deterministic production bug: ``MemPalaceMemorySource(source_cfg)`` bound
``source_cfg`` to ``name`` (which was the first parameter), leaving ``config``
as None — so ``_profile_name`` came out empty and cache_dir fell back to
``hermes_home()``, cross-contaminating profiles' caches.

The order is now ``config`` first, ``name`` second: a single positional
argument is always the config dict, and ``name`` keeps its default.
"""

from pathlib import Path

import pytest

from memchorus.hermes_home import profile_home
from memchorus.mempalace_memory_source import MemPalaceMemorySource


@pytest.fixture
def hermes_root(tmp_path, monkeypatch):
    """Pin HERMES_HOME to a neutral base dir (no ``profiles`` segment).

    Makes ``hermes_home()`` / ``profile_home()`` resolve deterministically
    regardless of the test runner's own profile env.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    return tmp_path


class TestInitParamOrder:
    def test_single_positional_arg_is_config(self, hermes_root):
        """The bug case: one positional arg must bind to *config*, not *name*."""
        cfg = {"profile_name": "cthugha", "skip_mcp": True}
        src = MemPalaceMemorySource(cfg)
        assert src.config == cfg
        assert src._name == "mempalace"  # default name still applies
        assert src._profile_name == "cthugha"
        assert Path(src._cache_dir) == profile_home("cthugha") / "mempalace_cache"

    def test_single_positional_not_bound_to_name(self, hermes_root):
        """A dict passed positionally must never leak into ``_name``."""
        cfg = {"profile_name": "bubo", "skip_mcp": True}
        src = MemPalaceMemorySource(cfg)
        assert isinstance(src._name, str)
        assert src._name == "mempalace"
        assert cfg not in (src._name,)

    def test_all_keyword_callers_unchanged(self, hermes_root):
        """Existing keyword callers (orchestrator/recall_cli style) keep working."""
        cfg = {"profile_name": "bubo", "skip_mcp": True}
        src = MemPalaceMemorySource(name="mempalace", config=cfg)
        assert src._name == "mempalace"
        assert src.config == cfg
        assert src._profile_name == "bubo"
        assert Path(src._cache_dir) == profile_home("bubo") / "mempalace_cache"

    def test_zero_arg_defaults(self, hermes_root):
        """Default constructor stays safe: default name, empty config, no profile."""
        src = MemPalaceMemorySource()
        assert src._name == "mempalace"
        assert src.config == {}
        assert src._profile_name == ""

    def test_two_positional_args_new_order(self, hermes_root):
        """New (config, name) positional order binds each to the right slot."""
        cfg = {"profile_name": "nyarlathotep", "skip_mcp": True}
        src = MemPalaceMemorySource(cfg, "custom")
        assert src._name == "custom"
        assert src.config == cfg
        assert src._profile_name == "nyarlathotep"
        assert Path(src._cache_dir) == profile_home("nyarlathotep") / "mempalace_cache"

    def test_explicit_cache_dir_still_wins(self, hermes_root, tmp_path):
        """An explicit ``cache_dir`` in config outranks profile scoping."""
        explicit = tmp_path / "explicit_cache"
        src = MemPalaceMemorySource({"profile_name": "cthugha", "cache_dir": str(explicit)})
        assert Path(src._cache_dir) == explicit
