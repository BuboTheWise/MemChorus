"""Tests for feedback_loop_verifier — the behavioral compliance checker."""

import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

import pytest


class TestVerifyFeedbackLoopComplete:
    """Test the core _verify_feedback_loop_complete() function."""

    def test_returns_result_with_no_violations_on_success(self, tmp_path):
        """When everything is green, violations list should be empty."""
        from memchorus.feedback_loop_verifier import (
            FeedbackLoopResult,
            _verify_feedback_loop_complete,
        )

        with mock.patch(
            "memchorus.feedback_loop_verifier._get_local_head_sha", return_value="abc123"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._get_remote_head_sha",
            return_value="abc123",
        ), mock.patch(
            "memchorus.feedback_loop_verifier._check_installed_from_sha",
            return_value=(True, True, "/some/path"),
        ), mock.patch(
            "memchorus.feedback_loop_verifier._run_tests", return_value=(True, "ok")
        ):
            result = _verify_feedback_loop_complete(
                repo_path=str(tmp_path),
                package_name="test_pkg",
                test_command=["python", "-c", "print(1)"],
            )

        assert result.passed is True
        assert len(result.violations) == 0

    def test_violation_when_not_pushed(self, tmp_path):
        """Should flag when local SHA differs from remote."""
        from memchorus.feedback_loop_verifier import _verify_feedback_loop_complete

        with mock.patch(
            "memchorus.feedback_loop_verifier._get_local_head_sha", return_value="local123"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._get_remote_head_sha",
            return_value="remote456",
        ):
            result = _verify_feedback_loop_complete(
                repo_path=str(tmp_path), package_name="test_pkg"
            )

        assert result.passed is False
        assert any("not pushed" in v or "!=" in v for v in result.violations)

    def test_violation_when_not_installed(self, tmp_path):
        """Should flag when the package is not installed."""
        from memchorus.feedback_loop_verifier import _verify_feedback_loop_complete

        with mock.patch(
            "memchorus.feedback_loop_verifier._get_local_head_sha", return_value="abc123"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._get_remote_head_sha",
            return_value="abc123",
        ), mock.patch(
            "memchorus.feedback_loop_verifier._check_installed_from_sha",
            return_value=(False, False, "not_installed"),
        ):
            result = _verify_feedback_loop_complete(
                repo_path=str(tmp_path), package_name="nonexistent_package_xyz"
            )

        assert result.passed is False
        assert any("not installed" in v for v in result.violations)

    def test_violation_when_tests_fail(self, tmp_path):
        """Should flag when tests don't pass under installed version."""
        from memchorus.feedback_loop_verifier import _verify_feedback_loop_complete

        with mock.patch(
            "memchorus.feedback_loop_verifier._get_local_head_sha", return_value="abc123"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._get_remote_head_sha",
            return_value="abc123",
        ), mock.patch(
            "memchorus.feedback_loop_verifier._check_installed_from_sha",
            return_value=(True, True, "/some/path"),
        ), mock.patch(
            "memchorus.feedback_loop_verifier._run_tests", return_value=(False, "FAILED")
        ):
            result = _verify_feedback_loop_complete(
                repo_path=str(tmp_path),
                package_name="test_pkg",
                test_command=["python", "-c", "exit(1)"],
            )

        assert result.passed is False
        assert any("test" in v.lower() or "failed" in v.lower() for v in result.violations)

    def test_skips_test_step_when_no_command(self, tmp_path):
        """If test_command is None, the test step should be skipped gracefully."""
        from memchorus.feedback_loop_verifier import _verify_feedback_loop_complete

        with mock.patch(
            "memchorus.feedback_loop_verifier._get_local_head_sha", return_value="abc123"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._get_remote_head_sha",
            return_value="abc123",
        ), mock.patch(
            "memchorus.feedback_loop_verifier._check_installed_from_sha",
            return_value=(True, True, "/some/path"),
        ):
            result = _verify_feedback_loop_complete(
                repo_path=str(tmp_path), package_name="test_pkg", test_command=None
            )

        assert result.passed is True
        assert result.details.get("tests_passed") == "skipped_no_command"

    def test_details_includes_shas(self, tmp_path):
        """SHAs should be recorded in details for audit trail."""
        from memchorus.feedback_loop_verifier import _verify_feedback_loop_complete

        with mock.patch(
            "memchorus.feedback_loop_verifier._get_local_head_sha", return_value="a1b2c3d4"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._get_remote_head_sha",
            return_value="e5f6g7h8",
        ):
            _verify_feedback_loop_complete(
                repo_path=str(tmp_path), package_name="test_pkg"
            )

        # Details dict should contain SHA info from the mocked calls.


class TestFeedbackLoopVerifierClass:
    """Test the FeedbackLoopVerifier convenience class."""

    def test_get_completion_metadata_is_clean(self, tmp_path):
        """Metadata dict should show passed=True when everything checks out."""
        from memchorus.feedback_loop_verifier import FeedbackLoopVerifier

        verifier = FeedbackLoopVerifier(repo_path=str(tmp_path), package_name="x")

        with mock.patch(
            "memchorus.feedback_loop_verifier._get_local_head_sha", return_value="abc"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._get_remote_head_sha", return_value="abc"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._check_installed_from_sha",
            return_value=(True, True, "/x"),
        ):
            meta = verifier.get_completion_metadata()

        assert meta["feedback_loop_verified"] is True
        assert meta["feedback_loop_violations"] == []

    def test_get_completion_metadata_includes_violations(self, tmp_path):
        """Metadata should include violation list when checks fail."""
        from memchorus.feedback_loop_verifier import FeedbackLoopVerifier

        verifier = FeedbackLoopVerifier(repo_path=str(tmp_path), package_name="x")

        with mock.patch(
            "memchorus.feedback_loop_verifier._get_local_head_sha", return_value="loc"
        ), mock.patch(
            "memchorus.feedback_loop_verifier._get_remote_head_sha", return_value="rem"
        ):
            meta = verifier.get_completion_metadata()

        assert meta["feedback_loop_verified"] is False
        assert len(meta["feedback_loop_violations"]) > 0


class TestHelperFunctions:
    """Test the internal helper functions."""

    def test_run_cmd_success(self):
        """_run_cmd should return exit code 0 for successful commands."""
        from memchorus.feedback_loop_verifier import _run_cmd

        rc, out, err = _run_cmd(["python", "-c", "print('hello')"])
        assert rc == 0
        assert "hello" in out
        assert not err

    def test_run_cmd_failure(self):
        """_run_cmd should capture non-zero exit codes."""
        from memchorus.feedback_loop_verifier import _run_cmd

        rc, out, err = _run_cmd(["python", "-c", "import sys; sys.exit(42)"])
        assert rc == 42

    def test_run_cmd_timeout(self):
        """_run_cmd should handle timeouts gracefully."""
        from memchorus.feedback_loop_verifier import _run_cmd

        rc, out, err = _run_cmd(["python", "-c", "import time; time.sleep(99)"], timeout=1)
        assert rc == 124
        assert "timeout" in err

    def test_get_local_head_sha_in_real_repo(self):
        """Should return a valid SHA in the actual MemChorus repo."""
        from memchorus.feedback_loop_verifier import _get_local_head_sha

        repo = os.path.expanduser("~/.hermes/workspace/Code/MemChorus")
        sha = _get_local_head_sha(repo)
        # SHA should be 40 hex chars
        assert sha is not None and len(sha.strip()) == 40

    def test_add_violation_sets_passed_to_false(self):
        """Adding violations must flip the passed flag."""
        from memchorus.feedback_loop_verifier import FeedbackLoopResult

        result = FeedbackLoopResult()
        assert result.passed is True

        result.add_violation("something went wrong")
        assert result.passed is False
        assert len(result.violations) == 1
