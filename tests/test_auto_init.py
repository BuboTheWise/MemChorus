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

    def test_overwrites_existing_config_without_error(self, tmp_path, monkeypatch, capsys):
        """Regression (GH-146): ``enable_plugin`` must overwrite a pre-existing
        ``config.yaml`` rather than hit ``FileExistsError [WinError 183]``.

        On Windows, ``Path.rename(src, dst)`` over an *existing* target raises
        ``FileExistsError [WinError 183]``; ``os.replace`` overwrites it instead.
        The plugin-enablement path (``enable_plugin``) *always* renames into an
        already-existing file — the config file is a precondition (``enable_plugin``
        returns early if it is absent) — so every successful call walks this line.
        On POSIX the old code masked the bug (``rename`` over existing works
        silently); on Windows it ``SystemExit(1)``.

        To make this a *real* regression test on the Linux CI box (where a bare
        rename-over-existing would also succeed), we reproduce the WinError 183
        behaviour directly by patching ``pathlib.Path.rename`` to raise
        ``FileExistsError`` when the destination already exists — which is exactly
        what Windows does. The fix uses ``os.replace`` (the C-level API, NOT the
        ``Path.rename`` wrapper), which the patch does not affect, so the old
        code fails this test and the new code passes on every platform.
        """
        import memchorus.auto_init as ai  # noqa: F811

        existing = ("agents:\n"
                    "  max_context: 8\n"
                    "plugins:\n"
                    "  enabled: [existing_a, existing_b]\n")
        fake_cfg = self._write_fake_cfg(existing, tmp_path)

        # Reproduce WinError 183: Path.rename over an existing target raises.
        def win_error_rename(self, target):
            if Path(target).exists():
                raise FileExistsError(
                    183,
                    "The process cannot access the file because it is being "
                    "used by another process.",
                )
            return orig_rename(self, target)

        orig_rename = Path.rename
        monkeypatch.setattr(Path, "rename", win_error_rename)

        orig_res = ai._resolve_hermes_config
        ai._resolve_hermes_config = lambda _p: fake_cfg  # type: ignore

        try:
            # Old (Path.rename) would raise SystemExit here under the simulated
            # WinError 183; the portable os.replace path must succeed.
            result = ai.enable_plugin(profile="default")
            assert result is True, "enable_plugin must overwrite, not raise"
            updated = fake_cfg.read_text()
            # Overwrite in place: memchorus added AND prior entries preserved.
            assert "memchorus" in updated
            assert "existing_a" in updated
            assert "existing_b" in updated
        finally:
            ai._resolve_hermes_config = orig_res  # restore

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


# ---------------------------------------------------------------------------
# GH-#335 — plugins.enabled validation (boolean / number / None entries)
# ---------------------------------------------------------------------------


class TestValidateEnabledEntries:
    """validate_enabled_entries splits a raw list into valid names and the
    entries that are not (bare booleans, numbers, None, empty strings)."""

    def test_rejects_yaml_boolean_true(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries([True, "memchorus"])
        assert valid == ["memchorus"]
        assert invalid == [True]

    def test_rejects_yaml_boolean_false(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries([False])
        assert valid == []
        assert invalid == [False]

    def test_rejects_numbers(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries([42, 3.14, "mempalace-mcp"])
        assert valid == ["mempalace-mcp"]
        assert invalid == [42, 3.14]

    def test_rejects_none(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries([None, "memchorus"])
        assert valid == ["memchorus"]
        assert invalid == [None]

    def test_rejects_nested_collections(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries([["a", "b"], {"k": "v"}, "ok"])
        assert valid == ["ok"]
        assert invalid == [["a", "b"], {"k": "v"}]

    def test_rejects_empty_and_whitespace_strings(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries(["", "   ", "memchorus"])
        assert valid == ["memchorus"]
        assert invalid == ["", "   "]

    def test_accepts_valid_plugin_names(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries(
            ["memchorus", "mempalace-mcp", "my_plugin", "v2"]
        )
        assert valid == ["memchorus", "mempalace-mcp", "my_plugin", "v2"]
        assert invalid == []

    def test_strips_whitespace_from_valid_names(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries(["  memchorus  "])
        assert valid == ["memchorus"]
        assert invalid == []

    def test_rejects_name_starting_with_digit(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries(["2fast", "9lives"])
        assert valid == []
        assert invalid == ["2fast", "9lives"]

    def test_handles_none_and_empty_input(self):
        from memchorus.auto_init import validate_enabled_entries
        assert validate_enabled_entries(None) == ([], [])
        assert validate_enabled_entries([]) == ([], [])

    def test_true_is_not_accepted_as_a_string_name(self):
        # The core of GH-#335: the YAML boolean `true` must not be treated as
        # the string "true", and the boolean-like *strings* "true"/"false" are
        # rejected too (a plugin literally named that is the bug, not a name).
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries(["true", "false", "True", True])
        assert valid == []
        # All four offenders must be preserved in order in `invalid`.
        assert "true" in invalid
        assert "false" in invalid
        assert "True" in invalid
        assert True in invalid

    def test_boolean_like_strings_rejected_case_insensitive(self):
        from memchorus.auto_init import validate_enabled_entries
        valid, invalid = validate_enabled_entries(
            ["TRUE", "False", "memchorus"]
        )
        assert valid == ["memchorus"]
        assert "TRUE" in invalid
        assert "False" in invalid


class TestCoerceEnabledList:
    """_coerce_enabled_list normalises the three shapes the config can take."""

    def test_none_becomes_empty_list(self):
        from memchorus.auto_init import _coerce_enabled_list
        assert _coerce_enabled_list(None) == []

    def test_list_is_preserved(self):
        from memchorus.auto_init import _coerce_enabled_list
        assert _coerce_enabled_list(["a", "b"]) == ["a", "b"]

    def test_tuple_is_preserved(self):
        from memchorus.auto_init import _coerce_enabled_list
        assert _coerce_enabled_list(("a",)) == ["a"]

    def test_bare_scalar_is_wrapped(self):
        from memchorus.auto_init import _coerce_enabled_list
        assert _coerce_enabled_list(True) == [True]
        assert _coerce_enabled_list("memchorus") == ["memchorus"]
        assert _coerce_enabled_list(42) == [42]


class TestEnablePluginValidation335:
    """GH-#335: enable_plugin rejects / drops non-name entries and normalises
    bare booleans instead of leaving them as phantom plugin names."""

    def _write_fake_cfg(self, text, tmp_path):
        fake_cfg = tmp_path / "config.yaml"
        fake_cfg.write_text(text)
        return fake_cfg

    def _patch(self, fake_cfg):
        import memchorus.auto_init as ai
        orig_res = ai._resolve_hermes_config
        ai._resolve_hermes_config = lambda _p: fake_cfg  # type: ignore
        return orig_res

    def test_drops_true_keeps_memchorus(self, tmp_path, capsys):
        import memchorus.auto_init as ai
        fake_cfg = self._write_fake_cfg(
            "plugins:\n  enabled: [true, memchorus]\n", tmp_path
        )
        orig_res = self._patch(fake_cfg)
        try:
            result = ai.enable_plugin(profile="default")
            import yaml
            data = yaml.safe_load(fake_cfg.read_text())
            enabled = data["plugins"]["enabled"]
            assert result is True
            assert enabled == ["memchorus"], f"got {enabled!r}"
            assert True not in enabled and "true" not in enabled
            # A warning should have been emitted on stderr.
            err = capsys.readouterr().err
            assert "rejecting" in err and "True" in err
        finally:
            ai._resolve_hermes_config = orig_res

    def test_block_list_true_not_corrupted(self, tmp_path):
        # The old line-based parser orphaned block-list items and produced
        # invalid YAML.  It must now normalise cleanly.
        import memchorus.auto_init as ai
        fake_cfg = self._write_fake_cfg(
            "plugins:\n  enabled:\n    - true\n    - memchorus\n", tmp_path
        )
        orig_res = self._patch(fake_cfg)
        try:
            result = ai.enable_plugin(profile="default")
            import yaml
            data = yaml.safe_load(fake_cfg.read_text())  # must parse
            assert data["plugins"]["enabled"] == ["memchorus"]
            assert result is True
        finally:
            ai._resolve_hermes_config = orig_res

    def test_bare_boolean_true_normalised(self, tmp_path):
        import memchorus.auto_init as ai
        fake_cfg = self._write_fake_cfg("plugins:\n  enabled: true\n", tmp_path)
        orig_res = self._patch(fake_cfg)
        try:
            result = ai.enable_plugin(profile="default")
            import yaml
            data = yaml.safe_load(fake_cfg.read_text())
            assert data["plugins"]["enabled"] == ["memchorus"]
            assert result is True
        finally:
            ai._resolve_hermes_config = orig_res

    def test_bare_boolean_false_normalised(self, tmp_path):
        import memchorus.auto_init as ai
        fake_cfg = self._write_fake_cfg("plugins:\n  enabled: false\n", tmp_path)
        orig_res = self._patch(fake_cfg)
        try:
            result = ai.enable_plugin(profile="default")
            import yaml
            data = yaml.safe_load(fake_cfg.read_text())
            assert data["plugins"]["enabled"] == ["memchorus"]
            assert result is True
        finally:
            ai._resolve_hermes_config = orig_res

    def test_mixed_null_number_string_cleaned(self, tmp_path):
        import memchorus.auto_init as ai
        fake_cfg = self._write_fake_cfg(
            "plugins:\n  enabled: [null, 42, memchorus, other-plug]\n", tmp_path
        )
        orig_res = self._patch(fake_cfg)
        try:
            result = ai.enable_plugin(profile="default")
            import yaml
            data = yaml.safe_load(fake_cfg.read_text())
            assert data["plugins"]["enabled"] == ["memchorus", "other-plug"]
            assert result is True
        finally:
            ai._resolve_hermes_config = orig_res

    def test_noop_when_memchorus_present_and_clean(self, tmp_path, capsys):
        import memchorus.auto_init as ai
        fake_cfg = self._write_fake_cfg(
            "plugins:\n  enabled: [memchorus, other-plug]\n", tmp_path
        )
        orig_res = self._patch(fake_cfg)
        before = fake_cfg.read_text()
        try:
            result = ai.enable_plugin(profile="default")
            assert result is False, "clean + already present => no-op"
            # File must be left untouched (idempotent).
            assert fake_cfg.read_text() == before
        finally:
            ai._resolve_hermes_config = orig_res

    def test_repairs_dirty_list_even_when_memchorus_present(self, tmp_path):
        # memchorus is present but a `true` entry lingers — the list is dirty,
        # so enable_plugin must clean it and report a change.
        import memchorus.auto_init as ai
        fake_cfg = self._write_fake_cfg(
            "plugins:\n  enabled: [memchorus, true]\n", tmp_path
        )
        orig_res = self._patch(fake_cfg)
        try:
            result = ai.enable_plugin(profile="default")
            import yaml
            data = yaml.safe_load(fake_cfg.read_text())
            assert data["plugins"]["enabled"] == ["memchorus"]
            assert result is True
        finally:
            ai._resolve_hermes_config = orig_res
