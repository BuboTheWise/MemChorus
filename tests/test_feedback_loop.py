"""TDD: Test-driven development for GH#101 feedback loop module.

Red phase tests before implementation exists.

Acceptance criteria from issue:
- _try_feedback_loop() returns actual feedback blocks when conditions are met
- Corrections survive restart via persisted state file (JSON, one entry per unique error fingerprint)  
- Exhaust counter decrements on each successful correction injection; entries with counter reaching 0 archived to permanent memory and removed from feedback queue
- Unit tests for: correct matching, exhaust-and-archive cycle, self-referential-artifact rejection in pipeline
"""

import pytest
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Expected module path per GH#101 spec  
try:
    from memchorus.feedback_loop import (
        FeedbackCorrection,
        FeedbackLoopManager,
        FeedbackPersistenceStore,
    )
except ImportError:
    pass  # Tests should fail in red phase if module doesn't exist yet


class TestFeedbackPersistenceStore:
    """Test persistence layer for feedback corrections."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.store_path = str(self.tmpdir / "feedback_corrections.json")
        self.store = FeedbackPersistenceStore(self.store_path)

    def teardown_method(self):
        if Path(self.store_path).exists():
            os.unlink(self.store_path)

    def test_load_empty_returns_empty_list(self):
        """Empty/nonexistent file returns empty list of corrections."""
        corrections = self.store.load()
        assert isinstance(corrections, list)
        assert len(corrections) == 0

    def test_save_and_reload_persists_corrections(self):
        """Saved corrections survive file round-trip."""
        correction = FeedbackCorrection(
            fingerprint="test_bug_001",
            context="pip install failed with ModuleNotFoundError",
            correction_text="Always use --force-reinstall flag, path shifts break editable installs",
            category="dependency",
            exhaust_ttl=3,
            current_ttl=3,
        )
        self.store.save(correction)
        
        corrections = self.store.load()
        assert len(corrections) == 1
        assert corrections[0].fingerprint == "test_bug_001"

    def test_save_updates_existing_corrections(self):
        """Saving existing fingerprint updates rather than duplicates."""
        correction = FeedbackCorrection(
            fingerprint="dep_error",
            context="pip failed",
            correction_text="use force-reinstall",
            category="dependency",
            exhaust_ttl=2,
            current_ttl=2,
        )
        self.store.save(correction)
        self.store.save(correction)  # save again
        
        corrections = self.store.load()
        assert len(corrections) == 1

    def test_save_with_different_fingerprints_allows_multiple(self):
        """Multiple corrections with different fingerprints coexist."""
        c1 = FeedbackCorrection("pip_issue", "context", "fix1", "dep", 3, 3)
        c2 = FeedbackCorrection("git_error", "context", "fix2", "vcs", 2, 2)
        self.store.save(c1)
        self.store.save(c2)
        
        corrections = self.store.load()
        assert len(corrections) == 2

    def test_archive_removal_deletes_entry(self):
        """Archiving a correction removes it from the feedback store."""
        c1 = FeedbackCorrection("old_bug", "context", "fix", "dep", 1, 1)
        self.store.save(c1)
        
        self.store.archive("old_bug")
        corrections = self.store.load()
        assert len(corrections) == 0

    def test_decrement_ttl_successfully(self):
        """Decrementing TTL updates the countdown counter."""
        c1 = FeedbackCorrection("ttl_test", "context", "fix", "", 5, 5)
        self.store.save(c1)
        
        remaining = self.store.decrement_ttl("ttl_test")
        assert remaining == 4
        
        corrections = self.store.load()
        assert corrections[0].current_ttl == 4

    def test_decrement_returns_zero_on_exhaust(self):
        """TTL reaches zero when exhausted."""
        c1 = FeedbackCorrection("zero_ttl", "context", "fix", "", 1, 1)
        self.store.save(c1)
        
        remaining = self.store.decrement_ttl("zero_ttl")
        assert remaining == 0


class TestFeedbackLoopManager:
    """Test the main feedback loop manager that powers _try_feedback_loop."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.store_path = str(self.tmpdir / "feedback.json")
        
        # Use real config with feedback_loop section (per spec)
        config = {
            "feedback_loop": {
                "enabled": True,
                "store_path": self.store_path,
            }
        }
        self.manager = FeedbackLoopManager(config=config)

    def teardown_method(self):
        if Path(self.store_path).exists():
            os.unlink(self.store_path)

    def test_disabled_config_returns_empty_blocks(self):
        """When feedback_loop disabled, skip processing entirely."""
        config = {"feedback_loop": {"enabled": False}}
        mgr = FeedbackLoopManager(config=config)
        
        blocks = mgr.process_feedback("test input query text", {})
        assert blocks == []

    def test_no_matching_correction_returns_empty_blocks(self):
        """No relevant corrections in store produces empty result."""
        kwargs = {"context": "normal_query"}
        # Empty feedback store — no matching corrections
        blocks = self.manager.process_feedback("some query text here", kwargs)
        assert blocks == []

    def test_matching_decision_point_returns_feedback_block(self):
        """Input matching a stored correction returns [[FEEDBACK CORRECTION]] block."""
        from memchorus.feedback_loop import FeedbackCorrection
        
        # Store a correction with fingerprint and category
        correction = FeedbackCorrection(
            fingerprint="module_not_found_dep_issue",
            context="pip install editable package failed with ModuleNotFoundError after path shift",
            correction_text="Always use --force-reinstall flag when reinstalling MemChorus to avoid stale wheel cache",
            category="dependency",
            exhaust_ttl=3,
            current_ttl=3,
        )
        self.manager._store.save(correction)
        
        kwargs = {"trigger_category": "dependency"}
        blocks = self.manager.process_feedback(
            "fix pip install ModuleNotFoundError hermes-agent site-packages broken path editable",
            kwargs
        )
        
        assert len(blocks) > 0
        assert "[[FEEDBACK CORRECTION]]" in blocks[0]

    def test_query_echo_guard_rejects_internal_artifacts(self):
        """Query echo detection filters out raw tool output / internal artifacts."""
        from memchorus.feedback_loop import FeedbackCorrection

        # Placeholder pattern 1: 'session context <token> current task'
        # Pattern 2: 'tool output for t_<hex>' or 'execution context t_<hex>'
        bad = FeedbackCorrection(
            fingerprint="bad_artifact",
            context="not real feedback tool output for t_8a3f2b",
            correction_text="some other stuff here",
            category="",
            exhaust_ttl=3,
            current_ttl=3,
        )

        # The save should validate content and reject artifacts
        assert self.manager._store.save(bad) is False
        
    def test_injection_decrements_exhaust_counter(self):
        """Each successful injection decrements the TTL counter."""
        from memchorus.feedback_loop import FeedbackCorrection
        
        correction = FeedbackCorrection(
            fingerprint="dep_ttl_check",
            context="pip install editable package failed broken site-packages path",
            correction_text="Use force-reinstall pip install to avoid broken site-packages cache issues",
            category="dependency",
            exhaust_ttl=2,
            current_ttl=2,
        )
        self.manager._store.save(correction)
        
        # First injection decrements from 2 to 1
        blocks = self.manager.process_feedback(
            "fix dependency installation failure pip install broken site-packages path",
            {"trigger_category": "dependency"}
        )
        assert len(blocks) > 0
        
        corrections = self.manager._store.load()
        assert corrections[0].current_ttl == 1

    def test_exhausted_entry_archived_to_permanent_memory(self):
        """When TTL reaches 0, correction is archived (removed from feedback queue)."""
        from memchorus.feedback_loop import FeedbackCorrection
        
        # TTL of 1 — will be exhausted after first injection
        correction = FeedbackCorrection(
            fingerprint="single_use",
            context="pip install dependency installation broken path issue fix",
            correction_text="Temporary workaround for pip dependency installation known issue",
            category="dependency",
            exhaust_ttl=1,
            current_ttl=1,
        )
        self.manager._store.save(correction)
        
        # First injection uses the last TTL point
        blocks = self.manager.process_feedback(
            "fix dependency installation pip problem",
            {"trigger_category": "dependency"}
        )
        assert len(blocks) > 0
        
        # Entry should now be removed from feedback queue
        corrections = self.manager._store.load()
        exhaust_entries = [c for c in corrections if c.current_ttl > 0]
        assert len(exhaust_entries) == 0




# ──────────────────────────────────────────────
# Integration tests (end-to-end with hooks.py)
# ──────────────────────────────────────────────
class TestFeedbackIntegrationWithHooks:
    """Test the integration with _try_feedback_loop in hooks.py."""

    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.store_path = str(self.tmpdir / "fb.json")

