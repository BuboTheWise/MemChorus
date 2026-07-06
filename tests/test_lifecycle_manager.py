"""Tests for memchorus.lifecycle_manager — Phase 1 Foundation.

Covers:
  - _resolve_lifecycle_config          defaults / overrides (§6.2)
  - AuditEntry                         JSONL serialisation (§6.4)
  - AuditLogger                        write, rotation, disabled path (§6.4)
  - LifecycleManager                   skeleton, is_enabled, sweep stub
  - SweepScheduler                     start/stop lifecycle
"""

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestResolveLifecycleConfig(unittest.TestCase):
    """Defaults & overrides for the config resolver (§6.2)."""

    def test_none_input_returns_full_defaults(self):
        from memchorus.lifecycle_manager import _resolve_lifecycle_config

        cfg = _resolve_lifecycle_config(None)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["sweep_interval_hours"], 8)
        self.assertIn("ephemeral", cfg["retention_days"])
        self.assertEqual(cfg["retention_days"]["ephemeral"], 7)
        self.assertIsNone(cfg["retention_days"]["user_preference"])

    def test_empty_dict_returns_defaults(self):
        from memchorus.lifecycle_manager import _resolve_lifecycle_config

        cfg = _resolve_lifecycle_config({})
        self.assertFalse(cfg["enabled"])

    def test_enabled_override(self):
        from memchorus.lifecycle_manager import _resolve_lifecycle_config

        cfg = _resolve_lifecycle_config({"enabled": True})
        self.assertTrue(cfg["enabled"])

    def test_sweep_interval_override(self):
        from memchorus.lifecycle_manager import _resolve_lifecycle_config

        cfg = _resolve_lifecycle_config({"sweep_interval_hours": 3})
        self.assertEqual(cfg["sweep_interval_hours"], 3)

    def test_retention_days_partial_override(self):
        from memchorus.lifecycle_manager import _resolve_lifecycle_config

        cfg = _resolve_lifecycle_config({"retention_days": {"ephemeral": 3}})
        self.assertEqual(cfg["retention_days"]["ephemeral"], 3)
        # Other profiles retain their defaults.
        self.assertEqual(cfg["retention_days"]["long_lived_knowledge"], 180)

    def test_eviction_defaults(self):
        from memchorus.lifecycle_manager import _resolve_lifecycle_config

        cfg = _resolve_lifecycle_config({})
        self.assertAlmostEqual(cfg["eviction"]["importance_min"], 0.15)
        self.assertEqual(cfg["eviction"]["duplicate_cluster_max"], 3)
        self.assertAlmostEqual(cfg["eviction"]["similarity_min"], 0.75)

    def test_audit_defaults(self):
        from memchorus.lifecycle_manager import _resolve_lifecycle_config

        cfg = _resolve_lifecycle_config({})
        self.assertTrue(cfg["audit"]["enabled"])
        self.assertIn("hermes", os.path.expanduser(cfg["audit"]["log_path"]))
        self.assertEqual(cfg["audit"]["max_entries"], 10_000)


class TestAuditEntry(unittest.TestCase):
    """AuditEntry JSON serialisation (§6.4)."""

    def test_to_json(self):
        from memchorus.lifecycle_manager import AuditEntry

        entry = AuditEntry(
            ts="2026-06-29T12:00:00+00:00", action="archive",
            memory_id="drawer_abc123", source="mempalace",
            reason="AGE_AND_IMPORTANCE", prev_score=0.08,
            profile="ephemeral", drawer="wing_project/room_code",
        )
        line = entry.to_json()
        obj = json.loads(line)
        self.assertEqual(obj["ts"], "2026-06-29T12:00:00+00:00")
        self.assertEqual(obj["action"], "archive")
        self.assertEqual(obj["memory_id"], "drawer_abc123")

    def test_serialisation_excludes_empty_strings(self):
        from memchorus.lifecycle_manager import AuditEntry

        entry = AuditEntry(
            ts="2026-06-29T12:00:00+00:00", action="purge",
        )
        obj = json.loads(entry.to_json())
        self.assertNotIn("memory_id", obj)
        self.assertEqual(obj["action"], "purge")

    def test_optional_prev_score_none_not_included(self):
        from memchorus.lifecycle_manager import AuditEntry

        entry = AuditEntry(
            ts="2026-06-29T12:00:00+00:00", action="archive",
        )
        obj = json.loads(entry.to_json())
        self.assertNotIn("prev_score", obj)


class TestAuditLogger(unittest.TestCase):
    """NDJSON writer + rotation (§6.4)."""

    def test_write_single_line(self):
        from memchorus.lifecycle_manager import AuditLogger, AuditEntry

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            al = AuditLogger(log_path=tmp_path, max_entries=1000)
            al.log(AuditEntry(ts="2026-01-01T00:00:00+00:00", action="archive"))
            with open(tmp_path) as fh:
                lines = fh.readlines()
            self.assertEqual(len(lines), 1)
            obj = json.loads(lines[0])
            self.assertEqual(obj["action"], "archive")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_disabled_does_not_write(self):
        from memchorus.lifecycle_manager import AuditLogger, AuditEntry

        with tempfile.TemporaryDirectory() as tmpdir:
            log_p = os.path.join(tmpdir, "audit.jsonl")
            al = AuditLogger(log_path=log_p, max_entries=1000, enabled=False)
            al.log(AuditEntry(ts="2026-01-01T00:00:00+00:00", action="purge"))
            self.assertFalse(os.path.exists(log_p))

    def test_rotation_keeps_max_entries(self):
        from memchorus.lifecycle_manager import AuditLogger, AuditEntry

        with tempfile.TemporaryDirectory() as tmpdir:
            log_p = os.path.join(tmpdir, "audit.jsonl")
            al = AuditLogger(log_path=log_p, max_entries=5)
            for i in range(8):
                ts = f"2026-01-01T0{i}:00:00+00:00"
                al.log(AuditEntry(ts=ts, action="archive", memory_id=f"id_{i}"))
            with open(log_p) as fh:
                lines = fh.readlines()
            # After writes past max_entries, file is capped (with small buffer).
            self.assertLessEqual(len(lines), 6)

    def test_record_convenience(self):
        from memchorus.lifecycle_manager import AuditLogger

        with tempfile.TemporaryDirectory() as tmpdir:
            log_p = os.path.join(tmpdir, "audit.jsonl")
            al = AuditLogger(log_path=log_p)
            al.record(action="purge", memory_id="m1", reason="AGE")
            with open(log_p) as fh:
                lines = fh.readlines()
            self.assertEqual(len(lines), 1)
            obj = json.loads(lines[0])
            self.assertEqual(obj["memory_id"], "m1")

    def test_enabled_property(self):
        from memchorus.lifecycle_manager import AuditLogger

        self.assertTrue(AuditLogger(enabled=True).enabled)
        self.assertFalse(AuditLogger(enabled=False).enabled)


class TestLifecycleManager(unittest.TestCase):
    """Skeleton lifecycle manager (§7 / §8 Phase 1)."""

    def setUp(self):
        from memchorus.lifecycle_manager import _resolve_lifecycle_config, LifecycleManager

        self.enabled_cfg = LifecycleManager(
            config=_resolve_lifecycle_config({"enabled": True}), orchestrator=None,
        )
        self.disabled_cfg = LifecycleManager(
            config=_resolve_lifecycle_config({}), orchestrator=None,
        )

    def test_is_enabled_true(self):
        self.assertTrue(self.enabled_cfg.is_enabled)

    def test_is_enabled_default_false(self):
        self.assertFalse(self.disabled_cfg.is_enabled)

    def test_audit_accessible(self):
        self.assertIsNotNone(self.enabled_cfg.audit_logger)

    def test_audit_and_alias_same_instance(self):
        self.assertIs(self.enabled_cfg.audit, self.enabled_cfg.audit_logger)

    def test_sweep_returns_summary(self):
        result = self.enabled_cfg.sweep()
        self.assertIn("sweep_time", result)
        self.assertIn("memories_reviewed", result)


class TestSweepScheduler(unittest.TestCase):
    """Timed execution driver (§6.1)."""

    def test_start_stop(self):
        from memchorus.lifecycle_manager import (
            _resolve_lifecycle_config, LifecycleManager, SweepScheduler,
        )

        mgr = LifecycleManager(_resolve_lifecycle_config({}), orchestrator=None)
        sched = SweepScheduler(mgr)
        self.assertFalse(sched.is_running)
        sched.start()
        self.assertTrue(sched.is_running)
        sched.stop()
        self.assertFalse(sched.is_running)


if __name__ == "__main__":
    unittest.main()
