"""Tests for memchorus.auto_init — config generation + file writing."""

from __future__ import annotations

import os
import tempfile
import textwrap
from pathlib import Path

import pytest

from memchorus.auto_init import (
    _build_wing_map,
    generate_config,
    write_config,
    enable_plugin,
)


# ---------------------------------------------------------------------------
# Wing map tests
# ---------------------------------------------------------------------------

class TestBuildWingMap:
    def test_default_profile_unprefixed(self):
        wmap = _build_wing_map("default")
        assert "DECISION" in wmap
        assert "LEARNING" in wmap
        # No profile prefix on keys
        for key in wmap:
            assert not key.startswith("_"), f"Unexpected prefix on {key}"

    def test_custom_profile_prefixed(self):
        wmap = _build_wing_map("test_executor")
        assert "test_executor_DECISION" in wmap
        assert "test_executor_LEARNING" in wmap
        for key in wmap:
            assert key.startswith("test_executor_"), f"Missing prefix on {key}"

    def test_rooms_are_defensive_copies(self):
        wmap = _build_wing_map("x")
        original_len = len(wmap["x_DECISION"])
        wmap["x_DECISION"].append("mutated")
        # Second call should be unaffected (template never modified)
        w2 = _build_wing_map("x")
        assert len(w2["x_DECISION"]) == original_len


# ---------------------------------------------------------------------------
# YAML generation tests
# ---------------------------------------------------------------------------

class TestGenerateConfig:
    def test_returns_yaml_string(self):
        txt = generate_config(profile="test_agent")
        assert isinstance(txt, str)
        assert "skip_mcp" in txt  # core field present

    def test_default_profile_omits_profile_name_key(self):
        txt = generate_config(profile="default")
        assert "profile_name:" not in txt

    def test_custom_profile_includes_profile_name(self):
        txt = generate_config(profile="test_executor")
        assert "profile_name: test_executor" in txt

    def test_data_dir_embedded_when_supplied(self):
        custom = "/opt/mem palace/data"
        txt = generate_config(profile="p", data_dir=custom)
        # YAML dumps paths literally
        assert custom.replace("/", "/") in txt  # identity check

    def test_skip_mcp_is_false_by_default(self):
        txt = generate_config()
        assert "skip_mcp: false" in txt

    def test_has_mempalace_and_heredefault_sections(self):
        txt = generate_config(profile="a")
        assert "mempalace_config:" in txt
        assert "hermes_default_config:" in txt


# ---------------------------------------------------------------------------
# File writer tests (idempotency + atomic rename)
# ---------------------------------------------------------------------------

class TestWriteConfig:
    def test_creates_file_in_new_dir(self, tmp_path):
        """Even when the parent doesn't exist yet."""
        dest = str(tmp_path / "new_pro" / "memchorus.yaml")
        # Monkey-patch home temporarily via env trick is overkill — just
        # use the generate_config path directly on a custom profile.
        import memchorus.auto_init as ai

        orig_resolver = ai._resolve_config_path

        def fake_resolver(_p):
            return Path(dest)

        ai._resolve_config_path = fake_resolver  # type: ignore

        try:
            ai.write_config(profile="new_pro")
            assert Path(dest).exists()
        finally:
            ai._resolve_config_path = orig_resolver  # restore

    def test_idempotent_on_second_call(self, tmp_path, capsys):
        dest = str(tmp_path / "idem.yaml")
        import memchorus.auto_init as ai

        orig_resolver = ai._resolve_config_path
        ai._resolve_config_path = lambda _p: Path(dest)  # type: ignore

        try:
            ai.write_config(profile="idem_test")  # first — creates file
            captured_first = capsys.readouterr()

            ai.write_config(profile="idem_test")  # second — same profile = same content
            captured_second = capsys.readouterr()

            assert "[skip]" in captured_second.out, "Second write should skip"
        finally:
            ai._resolve_config_path = orig_resolver


# ---------------------------------------------------------------------------
# Plugin-enablement tests
# ---------------------------------------------------------------------------

class TestEnablePlugin:
    def _write_fake_cfg(self, content, tmp_dir):
        """Helper: write a config.yaml under tmp_dir."""
        cfg = Path(str(tmp_dir)) / "config.yaml"
        cfg.write_text(content)
        return cfg

    def test_adds_memchorus_when_absent(self, tmp_path, capsys):
        import memchorus.auto_init as ai  # noqa: F811
        fake_cfg = self._write_fake_cfg("plugins:\n  enabled: [existing]\n",
                                       tmp_path)

        orig_res = ai._resolve_hermes_config
        ai._resolve_hermes_config = lambda _p: fake_cfg  # type: ignore

        try:
            result = ai.enable_plugin(profile="default")
            assert result is True
            updated = fake_cfg.read_text()
            assert "memchorus" in updated
        finally:
            ai._resolve_hermes_config = orig_res

    def test_noop_when_already_enabled(self, tmp_path, capsys):
        import memchorus.auto_init as ai  # noqa: F811
        fake_cfg = self._write_fake_cfg("plugins:\n  enabled: [memchorus]\n", tmp_path)

        orig_res = ai._resolve_hermes_config
        ai._resolve_hermes_config = lambda _p: fake_cfg  # type: ignore

        try:
            result = ai.enable_plugin(profile="default")
            assert result is False, "Should return False when already present"
        finally:
            ai._resolve_hermes_config = orig_res

    def test_returns_false_when_config_missing(self, tmp_path, capsys):
        import memchorus.auto_init as ai  # noqa: F811
        fake_cfg = tmp_path / "nonexistent.yaml"

        orig_res = ai._resolve_hermes_config
        ai._resolve_hermes_config = lambda _p: fake_cfg  # type: ignore

        try:
            result = ai.enable_plugin(profile="default")
            assert result is False, "Missing config => graceful no-op"
        finally:
            ai._resolve_hermes_config = orig_res
