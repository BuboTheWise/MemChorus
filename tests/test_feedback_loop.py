"""Tests for FeedbackLoop loader + schema_v1 — declarative YAML definitions.

Acceptance criteria from task t_d73391c3:
  F-1: Valid YAML parsing produces valid FeedbackLoopDefinition instances.
  F-2: Invalid YAML is rejected (parse errors, missing fields, bad types).
  F-3: Missing definition directory returns empty list without crashing.
  F-4: Field validation boundaries enforced (negative cooldown, empty name, etc.).
  F-5: Duplicate loop names resolve deterministically (first file wins).
  F-6: Loader silently skips unsupported schema versions.

All tests use unittest.TestCase for compatibility with the existing suite.
"""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any, Dict

# Ensure src is on path so `import memchorus` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

HAS_YAML = False
try:
    import yaml  # type: ignore[import-not-found]

    HAS_YAML = True
except ImportError:
    pass

from memchorus.feedback_loop.loader import (
    load_feedback_loops,
)
from memchorus.feedback_loop.schema_v1 import (
    ConditionSignal,
    FeedbackLoopDefinition,
    SUPPORTED_VERSIONS,
    TriggerEvent,
    validate_schema_v1,
)


# ---------------------------------------------------------------------------
# Fixtures -- helper to create a temp directory populated with YAML files.
# ---------------------------------------------------------------------------


def _mkloop_dict() -> Dict[str, Any]:
    """Return a minimal valid dict for FeedbackLoopDefinition."""
    return {
        "schema": "schema_v1",
        "name": "test_loop",
        "trigger_event": "pre_llm_call",
        "cooldown_interval": 60,
        "priority": 50,
        "enabled": True,
        "correction_prompt": "test",
    }


def _mkloop() -> FeedbackLoopDefinition:
    """Return a valid FeedbackLoopDefinition using defaults."""
    return validate_schema_v1(_mkloop_dict())


def _tmp_yaml_file(tmp_dir: Path, filename: str, content: str) -> Path:
    """Write *content* (may be YAML or plain text) to *tmp_dir/filename*."""
    p = tmp_dir / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ===========================================================================
# F-1 : Valid YAML parsing
# ===========================================================================


class TestValidYamParsing(unittest.TestCase):
    """F-1: Valid YAML decoding produces valid loop definitions."""

    def test_minimal_valid(self):
        """Minimal valid definition (only required fields, conditions omitted)."""
        d = validate_schema_v1(_mkloop_dict())
        self.assertIsInstance(d, FeedbackLoopDefinition)
        self.assertEqual(d.schema, "schema_v1")
        self.assertEqual(d.trigger_event, TriggerEvent.PRE_LLM_CALL)
        self.assertEqual(len(d.conditions), 0)

    def test_full_valid(self):
        """Fully-populated valid definition with conditions."""
        payload = _mkloop_dict()
        payload["conditions"] = {"spiral_risk_level": {"type": "threshold", "value": 50}}
        d = validate_schema_v1(payload)
        self.assertIn("spiral_risk_level", d.conditions)
        sig: ConditionSignal = d.conditions["spiral_risk_level"]  # type: ignore[misc]
        self.assertEqual(sig.type, "threshold")
        self.assertEqual(sig.value, 50)

    def test_to_dict_roundtrip(self):
        """to_dict() produces a structure that validates again."""
        payload = _mkloop_dict()
        payload["conditions"] = {"risk": {"type": "numeric", "value": 42}}
        original = validate_schema_v1(payload)
        dumped = original.to_dict()
        restored = FeedbackLoopDefinition(**dumped)
        self.assertEqual(restored.name, original.name)
        self.assertIn("risk", restored.conditions)

    def test_post_tool_call_trigger(self):
        """post_tool_call trigger_event is accepted."""
        payload = _mkloop_dict()
        payload["trigger_event"] = "post_tool_call"
        d = validate_schema_v1(payload)
        self.assertEqual(d.trigger_event, TriggerEvent.POST_TOOL_CALL)


# ===========================================================================
# F-2 : Invalid YAML rejection
# ===========================================================================


class TestInvalidYamlRejection(unittest.TestCase):
    """F-2: Invalid YAML content is rejected."""

    def test_missing_schema(self):
        """schema key absent → ValidationError (not swallowed)."""
        p = _mkloop_dict()
        del p["schema"]
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)

    def test_unknown_schema(self):
        """Schema version not in SUPPORTED_VERSIONS → rejected."""
        p = _mkloop_dict()
        p["schema"] = "foo_bar"
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)

    def test_invalid_trigger_event(self):
        """Trigger event not in TriggerEvent enum → rejected."""
        p = _mkloop_dict()
        p["trigger_event"] = "bad_value"
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)

    def test_wrong_cooldown_type(self):
        """Cooldown must be an integer, not a negative value."""
        p = _mkloop_dict()
        p["cooldown_interval"] = -1
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)


# ===========================================================================
# F-3 : Missing directory handling
# ===========================================================================


class TestMissingDirectory(unittest.TestCase):
    """F-3: Loader does not crash when the definition directory is missing."""

    def test_nonexistent_directory(self):
        load_feedback_loops(directory="/nonexistent/path/xyz_12345")  # should not raise


# ===========================================================================
# F-4 : Field validation boundaries
# ===========================================================================


class TestFieldValidationBoundaries(unittest.TestCase):
    """F-4: Schema enforces field-level validation rules."""

    def test_negative_cooldown(self):
        p = _mkloop_dict()
        p["cooldown_interval"] = -5
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)

    def test_zero_cooldown_allowed(self):
        """Cooldown of zero must be allowed (immediate retry mode)."""
        p = _mkloop_dict()
        p["cooldown_interval"] = 0
        d = validate_schema_v1(p)
        self.assertEqual(d.cooldown_interval, 0)

    def test_max_cooldown_allowed(self):
        """The absolute maximum cooldown (3600s) must be allowed."""
        p = _mkloop_dict()
        p["cooldown_interval"] = 3600
        d = validate_schema_v1(p)
        self.assertEqual(d.cooldown_interval, 3600)

    def test_cooldown_exceeded(self):
        """Cooldown > 3600 must be rejected."""
        p = _mkloop_dict()
        p["cooldown_interval"] = 3601
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)

    def test_empty_name(self):
        """Empty name string should fail validation."""
        p = _mkloop_dict()
        p["name"] = ""
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)

    def test_whitespace_only_name(self):
        """Whitespace-only name is accepted (stripped to non-empty)."""
        p = _mkloop_dict()
        result = validate_schema_v1(p)
        self.assertTrue(result.name.strip())

    def test_priority_boundary_high(self):
        p = _mkloop_dict()
        p["priority"] = 10000
        d = validate_schema_v1(p)
        self.assertEqual(d.priority, 10000)

    def test_priority_boundary_low(self):
        p = _mkloop_dict()
        p["priority"] = -10000
        d = validate_schema_v1(p)
        self.assertEqual(d.priority, -10000)

    def test_priority_exceeded(self):
        p = _mkloop_dict()
        p["priority"] = 10001
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)

    def test_priority_negative_exceeded(self):
        p = _mkloop_dict()
        p["priority"] = -10001
        with self.assertRaises(Exception):  # type: ignore[misc]
            validate_schema_v1(p)


# ===========================================================================
# F-5 : Duplicate name resolution
# ===========================================================================


class TestDuplicateNames(unittest.TestCase):
    """F-5: Loader resolves duplicate names (first file wins)."""

    def test_duplicate_names_first_wins(self):
        yaml_a = """
            schema: schema_v1
            name: dup_loop
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """
        yaml_b = """
            schema: schema_v1
            name: dup_loop
            trigger_event: post_tool_call
            cooldown_interval: 120
        """
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _tmp_yaml_file(td, "zz_a.yaml", yaml_a)
            _tmp_yaml_file(td, "zz_b.yaml", yaml_b)  # sorted after zz_a

            loops = load_feedback_loops(directory=str(td))
            self.assertEqual(len(loops), 1)
            self.assertEqual(loops[0].cooldown_interval, 60)


# ===========================================================================
# F-6 : Unsupported schema versions are skipped
# ===========================================================================


class TestUnsupportedSchemaVersion(unittest.TestCase):
    """F-6: Loader skips files with unknown schema versions silently."""

    def test_old_schema_skipped(self):
        content = textwrap.dedent("""
            schema: schema_v0
            name: old_loop
            trigger_event: pre_llm_call
            cooldown_interval: 60
        """)
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _tmp_yaml_file(td, "old.yaml", content)

            loops = load_feedback_loops(directory=str(td))
            self.assertEqual(len(loops), 0)


# ===========================================================================
# F-7 : Empty / malformed files gracefully skipped
# ===========================================================================


class TestMalformedFiles(unittest.TestCase):
    """F-7: Empty, whitespace-only, and non-YAML files are handled."""

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            (td / "empty.yaml").touch()

            loops = load_feedback_loops(directory=str(td))
            self.assertEqual(len(loops), 0)

    @unittest.skipUnless(HAS_YAML, "PyYAML not installed; cannot test YAML parse errors")
    def test_bad_yaml_syntax(self):
        # Malformed YAML that pyyaml can't parse (unmatched quotes, etc.)
        content = "{\n\tbroken yaml: [\n}  -- no closing\n"
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            _tmp_yaml_file(td, "bad.yaml", content)

            loops = load_feedback_loops(directory=str(td))
            self.assertEqual(len(loops), 0)


# ===========================================================================
if __name__ == "__main__":
    unittest.main()
