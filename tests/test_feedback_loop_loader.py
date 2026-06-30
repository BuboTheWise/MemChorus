"""Tests for MemChorus feedback_loop.loader module.

Covers:
- Valid YAML parsing with all fields populated correctly
- Invalid/rejected files (wrong types, missing required fields, etc.)
- Edge cases (empty directory, no yaml files, duplicate names)
"""

import copy
import logging
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import TestCase


# ---------------------------------------------------------------------------
# Helpers to avoid importing from the source repo during test bootstrap
# ---------------------------------------------------------------------------

def _tmp_yaml_file(root: Path, name: str, content: str) -> Path:
    """Write *content* to ``root/<name>``.  Returns the path written."""
    fpath = root / name
    # YAML in strings is often indented by doc authors -- strip first line indent
    fpath.write_text(textwrap.dedent(content), encoding="utf-8")
    return fpath


def _minimal_loop(**overrides: Any) -> Dict[str, Any]:
    """Return a minimal valid loop-def dict with sensible defaults."""
    base: Dict[str, Any] = {
        "schema": "schema_v1",
        "name": "test_loop",
        "trigger_event": "pre_llm_call",
        "conditions": {},
        "correction_prompt": "do something helpful",
        "cooldown_interval": 60,
        "priority": 5,
        "enabled": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Import under test (will be lazy inside each test to handle missing deps)
# ---------------------------------------------------------------------------

def _import_loader():
    """Import loader; raise ImportError when PyYAML is absent."""
    import yaml  # forces fail-fast if pyyaml truly not installed
    from memchorus.feedback_loop.loader import load_feedback_loops, DEFAULT_DIRECTORY
    return load_feedback_loops, DEFAULT_DIRECTORY


# ---------------------------------------------------------------------------
# Test helpers that create temp directories and call the loader
# ---------------------------------------------------------------------------

def _load_in_tmp(yaml_files: Dict[str, str]) -> List[Any]:
    """Create a temp dir, write *yaml_files*, load via ``load_feedback_loops``.
    
    Returns the list of FeedbackLoopDefinition instances.
    yaml_files maps basename → YAML content string.
    """
    load_fn, _ = _import_loader()
    tmp = Path(tempfile.mkdtemp())
    for fname, content in yaml_files.items():
        _tmp_yaml_file(tmp, fname, content)
    result = load_fn(directory=str(tmp))
    return result


def _load_raw_in_tmp(yaml_files: Dict[str, str]) -> object:
    """Like ``_load_in_tmp`` but returns the raw loaded list (may include None)."""
    return _load_in_tmp(yaml_files)


# ===========================================================================
# VALID YAML PARSING TESTS
# ===========================================================================

class TestValidYamlParsing(TestCase):
    """When a file is valid, it loads correctly."""

    def test_minimal_valid_loop(self):
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: minimal_loop
            trigger_event: pre_llm_call
            conditions: {}
            cooldown_interval: 60
        """)
        loops = _load_in_tmp({"minimal.yaml": yaml_def})
        self.assertEqual(len(loops), 1)
        loop = loops[0]
        self.assertEqual(loop.name, "minimal_loop")
        self.assertEqual(loop.trigger_event, "pre_llm_call")
        self.assertIsInstance(loop.conditions, dict)

    def test_all_fields_populated(self):
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: full_loop
            trigger_event: post_tool_call
            conditions:
              conversation_length: {type: gt, value: 40}
              repetition_entropy: {type: threshold, value: 0.7}
            correction_prompt: "Watch for loops"
            cooldown_interval: 120
            priority: 8
            enabled: true
        """)
        loops = _load_in_tmp({"full.yaml": yaml_def})
        self.assertEqual(len(loops), 1)
        loop = loops[0]
        self.assertEqual(loop.name, "full_loop")
        self.assertEqual(loop.trigger_event, "post_tool_call")
        self.assertEqual(loop.cooldown_interval, 120)
        self.assertEqual(loop.priority, 8)
        self.assertTrue(loop.enabled)

    def test_correct_field_types(self):
        """All fields should have correct Python types after loading."""
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: type_check_loop
            trigger_event: pre_llm_call
            conditions: {}
            correction_prompt: "check types"
            cooldown_interval: 90
            priority: -5
            enabled: false
        """)
        loops = _load_in_tmp({"types.yaml": yaml_def})
        loop = loops[0]
        # Type checks
        self.assertIsInstance(loop.name, str)
        self.assertTrue(hasattr(loop.trigger_event, "value"))  # enum
        self.assertIsInstance(loop.conditions, dict)
        self.assertIsInstance(loop.cooldown_interval, int)
        self.assertIsInstance(loop.priority, int)
        self.assertIsInstance(loop.enabled, bool)

    def test_valid_yml_extension(self):
        ".yml files should load identically to .yaml."
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: yml_loop
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """)
        loops = _load_in_tmp({"config.yml": yaml_def})
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0].name, "yml_loop")

    def test_trigger_event_string_maps_to_enum(self):
        """trigger_event as a plain string should map to TriggerEvent enum."""
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: trigger_test
            trigger_event: post_tool_call
            cooldown_interval: 60
        """)
        loops = _load_in_tmp({"trigger.yaml": yaml_def})
        loop = loops[0]
        # Pydantic should have created a TriggerEvent enum instance
        self.assertTrue(hasattr(loop.trigger_event, "value"))

# ===========================================================================
# INVALID / REJECTED FILES TESTS
# ===========================================================================

class TestInvalidRejectedFiles(TestCase):
    """Malformed or schema-violating files should be skipped with warnings."""

    def test_missing_required_schema_field(self):
        yaml_def = textwrap.dedent("""\
            name: no_schema_loop
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """)
        loops = _load_in_tmp({"missing_schema.yaml": yaml_def})
        # Should be empty -- invalid entry skipped
        self.assertEqual(len(loops), 0)

    def test_missing_required_name_field(self):
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """)
        loops = _load_in_tmp({"no_name.yaml": yaml_def})
        self.assertEqual(len(loops), 0)

    def test_wrong_cooldown_type(self):
        """cooldown_interval as a string should fail validation."""
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: cooldown_str_loop
            trigger_event: pre_llm_call
            cooldown_interval: "not_a_number"
        """)
        loops = _load_in_tmp({"bad_cooldown.yaml": yaml_def})
        self.assertEqual(len(loops), 0)

    def test_malformed_yaml_syntax(self):
        """Broken YAML should be silently skipped."""
        bad_yaml = textwrap.dedent("""\
            schema: schema_v1
            name: broken: yaml: [{not} valid
              - unclosed bracket
        """)
        loops = _load_in_tmp({"broken.yaml": bad_yaml})
        self.assertEqual(len(loops), 0)

    def test_non_dict_top_level(self):
        """Top-level scalar/list instead of mapping should be skipped."""
        list_yaml = "- item1\n- item2\n"
        scalar_yaml = "just a string\n"
        loops_list = _load_in_tmp({"list.yaml": list_yaml})
        loops_scalar = _load_in_tmp({"scalar.yaml": scalar_yaml})
        self.assertEqual(len(loops_list), 0)
        self.assertEqual(len(loops_scalar), 0)

    def test_unsupported_schema_version(self):
        yaml_def = textwrap.dedent("""\
            schema: schema_v2
            name: v2_loop
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """)
        loops = _load_in_tmp({"v2.yaml": yaml_def})
        self.assertEqual(len(loops), 0)

    def test_invalid_trigger_event(self):
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: bad_trigger
            trigger_event: invalid_trigger_value
            cooldown_interval: 60
        """)
        loops = _load_in_tmp({"bad_trigger.yaml": yaml_def})
        self.assertEqual(len(loops), 0)

# ===========================================================================
# EDGE CASES TESTS
# ===========================================================================

class TestEdgeCases(TestCase):
    """Handle edge cases gracefully without crashing."""

    def test_empty_directory(self):
        tmp = Path(tempfile.mkdtemp())
        load_fn, _ = _import_loader()
        # Pass an empty temp dir
        result = load_fn(directory=str(tmp))
        self.assertEqual(result, [])

    def test_no_yaml_files_present(self):
        """Directory with only non-yaml files should return empty."""
        import sys
        if sys.version_info >= (3, 11):
            from contextlib import chdir as chdir_ctx
        else:
            class _chdir:
                def __init__(self, path): self.path = path
                def __enter__(self): self._old = Path.cwd()
                def __exit__(self, *a): None
            chdir_ctx = lambda x: _chdir(x)  # type: ignore[assignment]
        tmp = Path(tempfile.mkdtemp())
        load_fn, _ = _import_loader()
        (tmp / "readme.md").write_text("hello")
        (tmp / "data.txt").write_text("world")
        result = load_fn(directory=str(tmp))
        self.assertEqual(result, [])

    def test_missing_directory(self):
        """If the specified path doesn't exist, return empty list."""
        load_fn, _ = _import_loader()
        result = load_fn(directory="/nonexistent/path/that/should/not/exist")
        self.assertEqual(result, [])

    def test_duplicate_names_across_files_first_wins(self):
        """When two files define the same name, first file (by sort order) takes precedence."""
        yaml_a = textwrap.dedent("""\
            schema: schema_v1
            name: dup_loop
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """)
        yaml_b = textwrap.dedent("""\
            schema: schema_v1
            name: dup_loop
            trigger_event: post_tool_call
            cooldown_interval: 120
        """)
        loops = _load_in_tmp({
            "aa_first.yaml": yaml_a,  # sorts earlier
            "zz_second.yaml": yaml_b,  # sorts later
        })
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0].name, "dup_loop")
        # First file wins -- should be aa_first with cooldown=60
        self.assertEqual(loops[0].cooldown_interval, 60)

    def test_duplicate_names_across_different_files_warning_logged(self):
        """Duplicates should produce a warning log."""
        yaml_a = textwrap.dedent("""\
            schema: schema_v1
            name: dup_loop
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """)
        yaml_b = textwrap.dedent("""\
            schema: schema_v1
            name: dup_loop
            trigger_event: post_tool_call
            cooldown_interval: 120
        """)
        # Capture the logging output
        import io
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        loader_logger = logging.getLogger("memchorus.feedback_loop.loader")
        original_level = loader_logger.level
        loader_logger.addHandler(handler)
        try:
            load_fn, _ = _import_loader()
            tmp = Path(tempfile.mkdtemp())
            _tmp_yaml_file(tmp, "aa.yaml", yaml_a)
            _tmp_yaml_file(tmp, "zz.yaml", yaml_b)
            loops = load_fn(directory=str(tmp))
            stream.seek(0)
            log_output = stream.read()
            self.assertIn("duplicate", log_output.lower())
        finally:
            loader_logger.removeHandler(handler)
            loader_logger.setLevel(original_level)

    def test_empty_yaml_file(self):
        """Empty file should be skipped."""
        loops = _load_in_tmp({"empty.yaml": ""})
        self.assertEqual(len(loops), 0)

    def test_whitespace_only_file(self):
        """Whitespace-only file should be skipped."""
        import sys
        if sys.version_info >= (3, 11):
            from contextlib import chdir as chdir_ctx
        else:
            class _chdir:
                def __init__(self, path): self.path = path
                def __enter__(self): self._old = Path.cwd()
                def __exit__(self, *a): None
            chdir_ctx = lambda x: _chdir(x)  # type: ignore[assignment]
        tmp = Path(tempfile.mkdtemp())
        load_fn, _ = _import_loader()
        (tmp / "whitespace.yaml").write_text("   \n  \n  ")
        result = load_fn(directory=str(tmp))
        self.assertEqual(len(result), 0)

    def test_multiple_valid_loops(self):
        """Multiple valid YAML files should all load."""
        yaml_a = textwrap.dedent("""\
            schema: schema_v1
            name: loop_a
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """)
        yaml_b = textwrap.dedent("""\
            schema: schema_v1
            name: loop_b
            trigger_event: post_tool_call
            cooldown_interval: 120
        """)
        loops = _load_in_tmp({
            "first.yaml": yaml_a,
            "second.yaml": yaml_b,
        })
        self.assertEqual(len(loops), 2)
        names = {lo.name for lo in loops}
        self.assertIn("loop_a", names)
        self.assertIn("loop_b", names)

    def test_enabled_false_loop(self):
        """enabled: false should still parse correctly, just be disabled."""
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: disabled_loop
            trigger_event: pre_llm_call
            cooldown_interval: 60
            enabled: false
        """)
        loops = _load_in_tmp({"disabled.yaml": yaml_def})
        self.assertEqual(len(loops), 1)
        self.assertFalse(loops[0].enabled)

# ===========================================================================
# EXISTING TEST BACKWARDS-COMPATIBILITY (test_feedback_loop.py references)
# ===========================================================================

class TestBackwardsCompatibility(TestCase):
    """Verify the code matches what existing tests expect."""

    def test_load_feedback_loops_importable(self):
        load_fn, _ = _import_loader()
        self.assertTrue(callable(load_fn))

    def test_trigger_event_values(self):
        """The two allowed trigger event values."""
        loops = _load_in_tmp({
            "pre.yaml": textwrap.dedent("""\
                schema: schema_v1
                name: pre_test
                trigger_event: pre_llm_call
                cooldown_interval: 60
            """),
            "post.yaml": textwrap.dedent("""\
                schema: schema_v1
                name: post_test
                trigger_event: post_tool_call
                cooldown_interval: 90
            """),
        })
        triggers = {lo.trigger_event.value for lo in loops}
        self.assertEqual(triggers, {"pre_llm_call", "post_tool_call"})

    def test_conditions_parsing(self):
        """Conditions should parse to a dict."""
        yaml_def = textwrap.dedent("""\
            schema: schema_v1
            name: conditions_test
            trigger_event: pre_llm_call
            cooldown_interval: 60
            conditions:
              conversation_length: {type: gt, value: 40}
              repetition_entropy: {type: threshold, value: 0.7}
        """)
        loops = _load_in_tmp({"conditions.yaml": yaml_def})
        loop = loops[0]
        self.assertIsInstance(loop.conditions, dict)

if __name__ == "__main__":  # pragma: no cover
    import unittest
    unittest.main()
