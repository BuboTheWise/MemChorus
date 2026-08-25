"""Unit tests for src/memchorus/prohibitions.py — Behavioral guard system core."""

import json
import tempfile
from pathlib import Path
from typing import List

import pytest


@pytest.fixture
def manager(tmp_path: Path):
    from memchorus.prohibitions import ProhibitionsManager
    mgr = ProhibitionsManager(data_dir=tmp_path)
    mgr.load()
    return mgr


# ---------------------------------------------------------------------------
# GuardVerdict + GuardResult
# ---------------------------------------------------------------------------

class TestGuardVerdict:
    def test_enum_values(self):
        from memchorus.prohibitions import GuardVerdict
        assert GuardVerdict.OK.value == "ok"
        assert GuardVerdict.WARNING.value == "warning"
        assert GuardVerdict.BLOCK.value == "block"


class TestGuardResult:
    def _make_result(self, verdict=None, rules=None):
        from memchorus.prohibitions import GuardResult
        kw = {}
        if verdict is not None:
            kw["verdict"] = verdict
        if rules is not None:
            kw["matched_rules"] = rules
        return GuardResult(**kw)

    def test_triggered_default_is_false(self):
        result = self._make_result()
        assert not result.triggered

    def test_triggered_warning(self):
        from memchorus.prohibitions import GuardVerdict
        result = self._make_result(verdict=GuardVerdict.WARNING)
        assert result.triggered

    def test_triggered_block(self):
        from memchorus.prohibitions import GuardVerdict
        result = self._make_result(verdict=GuardVerdict.BLOCK)
        assert result.triggered

    def test_inject_blocks_empty_when_ok(self):
        blocks = self._make_result().inject_blocks()
        assert blocks == []

    def test_inject_blocks_returns_double_bracket_text(self):
        from memchorus.prohibitions import GuardVerdict, Prohibition
        rule = Prohibition(
            id="test-rule-1",
            condition="Never delete the venv directory",
            trigger_keywords=["rm -rf"],
            severity=3,
            block_action="Deleting site-packages",
            rationale="Breaks the agent runtime",
        )
        rule._compile_patterns()
        result = self._make_result(verdict=GuardVerdict.BLOCK, rules=[rule])
        blocks: List[str] = result.inject_blocks()
        assert len(blocks) == 1
        assert "[[BEHAVIORAL GUARD:" in blocks[0]
        assert "test-rule-1" in blocks[0]
        assert "**WHY:**" in blocks[0]

    def test_inject_blocks_caps_at_three_rules(self):
        from memchorus.prohibitions import GuardVerdict, Prohibition
        rules = []
        for i in range(5):
            r = Prohibition(id=f"r{i}", condition=f"Rule {i}", trigger_keywords=[f"kw{i}"])
            r._compile_patterns()
            rules.append(r)
        result = self._make_result(verdict=GuardVerdict.BLOCK, rules=rules)
        blocks = result.inject_blocks()
        assert len(blocks) == 3


# ---------------------------------------------------------------------------
# Prohibition model
# ---------------------------------------------------------------------------

class TestProhibition:
    def test_roundtrip_serialization(self):
        from memchorus.prohibitions import Prohibition
        raw = {
            "id": "guard-A",
            "condition": "Never run rm -rf /important",
            "trigger_keywords": ["rm -rf", "delete important"],
            "tool_call_check": r"rm\s+-rf\s+/i",
            "severity": 3,
            "block_action": "Deleting critical paths",
            "rationale": "System stability",
            "source": "system",
            "created": "2026-08-18T12:00:00+00:00",
            "type": "infrastructure",
        }
        rule = Prohibition.from_dict(raw)
        assert rule.id == raw["id"]
        assert rule.condition == raw["condition"]
        assert rule.trigger_keywords == raw["trigger_keywords"]
        out = rule.to_dict()
        for key in raw:
            if key == "created":
                continue
            assert out[key] == raw[key], f"Mismatch on {key}: {out[key]!r} != {raw[key]!r}"

    def test_matches_text_after_compile(self):
        from memchorus.prohibitions import Prohibition
        rule = Prohibition(id="t1", condition="No editable install", trigger_keywords=["pip install -e"])
        rule._compile_patterns()
        assert rule.matches_text("I will run pip install -e . to fix this")
        assert not rule.matches_text("I will write a Python script")

    def test_matches_text_case_insensitive(self):
        from memchorus.prohibitions import Prohibition
        rule = Prohibition(id="t2", condition="No venv deletion", trigger_keywords=["delete site-packages"])
        rule._compile_patterns()
        assert rule.matches_text("Please DELETE SITE-PACKAGES now")

    def test_matches_text_no_compiled_patterns_returns_false(self):
        from memchorus.prohibitions import Prohibition
        rule = Prohibition(id="t3", condition="empty", trigger_keywords=[])
        assert not rule.matches_text("anything at all")

    def test_tool_call_check_compiles_to_regex(self):
        from memchorus.prohibitions import Prohibition as R  # noqa: N806
        rule = R(id="t4", condition="test", tool_call_check=r"pip\s+install\s+\-e")
        rule._compile_patterns()
        assert rule.matches_text("pip install -e /foo")
        assert not rule.matches_text("pip show requests")

    def test_invalid_regex_is_handled_gracefully(self):
        from memchorus.prohibitions import Prohibition as R2  # noqa: N806
        rule = R2(id="t5", condition="test", tool_call_check="[invalid(regex")
        rule._compile_patterns()
        assert not rule.matches_text("anything")

    def test_guard001_tool_call_check_matches_editable_install_spacings(self):
        import re
        from memchorus.prohibitions import _DEFAULT_SEED_RULES
        rule = next(
            r for r in _DEFAULT_SEED_RULES
            if r["id"] == "guard-001-no-editable-install"
        )
        pattern = re.compile(rule["tool_call_check"], re.IGNORECASE)
        assert pattern.search("pip install -e ./hermes-agent")
        assert pattern.search("pip install  -e  ./hermes-agent")

    def test_default_values(self):
        from memchorus.prohibitions import Prohibition as R3  # noqa: N806
        rule = R3(id="t6", condition="minimal")
        assert rule.severity == 3
        assert rule.block_action == ""
        assert rule.rationale == ""
        assert rule.source == "system"
        assert rule.type_ == "infrastructure"


# ---------------------------------------------------------------------------
# ProhibitionsManager
# ---------------------------------------------------------------------------

class TestProhibitionsManager:
    def test_load_seeds_three_rules(self, manager):
        rules = manager.rules
        assert len(rules) >= 3
        for r in rules:
            assert r.source == "system"

    def test_save_and_reload_idempotent(self, manager, tmp_path):
        count_before = len(manager.rules)
        manager.save()
        from memchorus.prohibitions import ProhibitionsManager
        mgr2 = ProhibitionsManager(data_dir=tmp_path)
        count_after = mgr2.load()
        assert count_after == count_before

    def test_scan_text_no_match_is_ok(self, manager):
        from memchorus.prohibitions import GuardVerdict
        result = manager.scan_text("just a normal greeting")
        assert result.verdict == GuardVerdict.OK
        assert not result.triggered

    def test_scan_text_matches_seed_venv_guard(self, manager):
        from memchorus.prohibitions import GuardVerdict
        text = "I should run pip install -e to fix the editable install issue"
        result = manager.scan_text(text)
        assert result.verdict == GuardVerdict.BLOCK
        assert result.triggered
        assert len(result.matched_rules) >= 1

    def test_scan_text_matches_seed_scratch_delete(self, manager):
        from memchorus.prohibitions import GuardVerdict
        text = "cleanup by delete site-packages now"
        result = manager.scan_text(text)
        assert result.verdict in (GuardVerdict.BLOCK, GuardVerdict.WARNING)
        assert result.triggered

    def test_scan_tool_call_wraps_command_and_args(self, manager):
        from memchorus.prohibitions import GuardVerdict
        result = manager.scan_tool_call("pip", "install -e ~/.hermes/")
        assert result.triggered
        assert result.verdict == GuardVerdict.BLOCK

    def test_add_rule_appends(self, manager):
        from memchorus.prohibitions import Prohibition
        new_rule = Prohibition(id="custom-abc", condition="Never touch the database directly", trigger_keywords=["drop table"])
        n0 = len(manager.rules)
        manager.add_rule(new_rule)
        assert len(manager.rules) == n0 + 1

    def test_add_rule_deduplicates_by_id(self, manager):
        from memchorus.prohibitions import Prohibition
        rule = Prohibition(id="custom-xyz", condition="no ping", trigger_keywords=["ping"])
        n0 = len(manager.rules)
        manager.add_rule(rule)
        manager.add_rule(rule)
        assert len(manager.rules) == n0 + 1

    def test_remove_rule_by_id(self, manager):
        seed_ids = [r.id for r in manager.rules]
        target = seed_ids[0]
        assert manager.remove_rule(target) is True
        remaining_ids = [r.id for r in manager.rules]
        assert target not in remaining_ids

    def test_remove_nonexistent_id(self, manager):
        assert manager.remove_rule("does-not-exist") is False

    def test_len_and_rules_property(self, manager):
        assert len(manager) >= 3
        n = len(manager.rules)
        manager.rules.clear()
        assert len(manager) == n

    def test_jsonl_file_created_with_valid_lines(self, manager, tmp_path):
        jsonl = tmp_path / "prohibitions.jsonl"
        assert jsonl.exists()
        content = jsonl.read_text(encoding="utf-8")
        for line in content.strip().splitlines():
            obj = json.loads(line)
            assert "id" in obj
            assert "condition" in obj
