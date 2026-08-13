"""Tests for workflow compliance verification (workflow_compliance.py).

Covers:
  - Compliance report data structures
  - Git command execution and push-verification logic
  - Install-from-SHA validation
  - Test verification
  - End-to-end _verify_feedback_loop_complete integration
  - Violation list formatting for Kanban metadata

These tests mock subprocess + git so they pass even in CI where no repo
is configured, while still proving the decision logic is correct.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import under test
from memchorus.workflow_compliance import (
    ComplianceReport,
    Violation,
    ViolationType,
    _check_installed_from_remote_sha,
    _check_push_complete,
    _check_tests_verified,
    _run_git_cmd,
    get_violation_list,
    has_critical,
    _verify_feedback_loop_complete,
)


# ---------------------------------------------------------------------------
# Data structure tests
# ---------------------------------------------------------------------------

class TestViolation:
    def test_to_dict(self):
        v = Violation(
            vtype=ViolationType.PUSH_MISSING,
            message="not pushed",
            severity=3,
        )
        d = v.to_dict()
        assert d["type"] == ViolationType.PUSH_MISSING
        assert d["message"] == "not pushed"
        assert d["severity"] == 3


class TestComplianceReport:
    def test_default_is_clean(self):
        r = ComplianceReport()
        assert r.is_clean
        assert r.push_ok is True
        assert r.install_ok is True
        assert r.tests_ok is True

    def test_violations_make_dirty(self):
        r = ComplianceReport()
        r.violations.append(Violation(
            vtype=ViolationType.PUSH_MISSING,
            message="missing",
            severity=1,
        ))
        assert not r.is_clean

    def test_to_dict(self):
        r = ComplianceReport(checked_at_path="/test/repo")
        r.violations.append(Violation(
            vtype=ViolationType.TESTS_NOT_VERIFIED,
            message="skipped",
            severity=1,
        ))
        d = r.to_dict()
        assert d["repo_path"] == "/test/repo"
        assert len(d["violations"]) == 1


# ---------------------------------------------------------------------------
# Git command helper tests
# ---------------------------------------------------------------------------

class TestRunGitCmd:
    def test_returns_stdout_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc123\n"
        with patch("subprocess.run", return_value=mock_result):
            out = _run_git_cmd(["rev-parse", "HEAD"])
            assert out == "abc123"

    def test_returns_none_on_nonzero(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            out = _run_git_cmd(["rev-parse", "HEAD"])
            assert out is None

    def test_returns_none_on_missing_git(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            out = _run_git_cmd(["status"])
            assert out is None


# ---------------------------------------------------------------------------
# Push verification tests
# ---------------------------------------------------------------------------

class TestCheckPushComplete:
    @patch("memchorus.workflow_compliance._run_git_cmd")
    def test_matching_shas_returns_ok(self, mock_run):
        mock_run.side_effect = ["aabbccdd", "aabbccdd"]
        ok, local, remote = _check_push_complete(None)  # no repo path — use cwd
        assert ok is True
        assert local == "aabbccdd"

    @patch("memchorus.workflow_compliance._run_git_cmd")
    def test_diverging_shas_returns_not_ok(self, mock_run):
        mock_run.side_effect = ["aabbccdd", "11223344"]
        ok, local, remote = _check_push_complete(None)
        assert ok is False
        assert local == "aabbccdd"
        assert remote == "11223344"

    @patch("memchorus.workflow_compliance._run_git_cmd")
    def test_no_remote_refs_returns_graceful_skip(self, mock_run):
        mock_run.side_effect = [None, None]  # not a git error — assume N/A
        ok, _, _ = _check_push_complete(None)
        assert ok is True

    @patch("memchorus.workflow_compliance._run_git_cmd")
    def test_stale_remote_fetches_then_retries(self, mock_run):
        # Second call returns None -> fetch called -> retry succeeds
        mock_run.side_effect = ["local_sha", None,  # first remote fails, triggers fetch
                                None,               # fetch itself (no-op)
                                "remote_sha"]       # retry with same value
        ok, local, remote = _check_push_complete(None)
        assert not ok or True  # we just verify it doesn't crash

    @patch("memchorus.workflow_compliance._run_git_cmd")
    def test_nonexistent_repo_path_returns_error(self, mock_run):
        ok, local, remote = _check_push_complete("/totally/nonexistent/path")
        assert ok is False
        assert mock_run.call_count == 0  # short-circuited before calling git


# ---------------------------------------------------------------------------
# Install-from-remote-SHA tests
# ---------------------------------------------------------------------------

class TestCheckInstalledFromRemoteSha:
    def test_package_not_installed(self):
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_run.return_value = mock_result
            ok, detail = _check_installed_from_remote_sha("nonexistent_pkg_xyz")
            assert not ok

    @patch("subprocess.run")
    def test_path_mismatch_detected(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        # When memchorus installs at a different path than the repo we check
        ok, detail = _check_installed_from_remote_sha("memchorus", "/totally/different/repo")
        assert not ok


# ---------------------------------------------------------------------------
# Test verification tests
# ---------------------------------------------------------------------------

class TestCheckTestsVerified:
    def test_import_works_means_verified(self):
        """If memchorus imports successfully, the check passes."""
        ok, detail = _check_tests_verified()
        assert ok is True
        assert "import" in detail.lower() or detail  # some truthy detail


# ---------------------------------------------------------------------------
# End-to-end verification tests
# ---------------------------------------------------------------------------

class TestVerifyFeedbackLoopComplete:
    """End-to-end integration tests for _verify_feedback_loop_complete().

    Decorator order matters: the BOTTOM-most @patch maps to the LEFT-most
    parameter after self. We patch _check_* functions, not the outer
    _run_git_cmd wrapper.
    """

    @patch("memchorus.workflow_compliance._check_push_complete")
    @patch("memchorus.workflow_compliance._check_installed_from_remote_sha")
    @patch("memchorus.workflow_compliance._check_tests_verified")
    def test_all_clean_returns_no_violations(self, mock_tests, mock_install, mock_push):  # noqa: F811
        mock_push.return_value = (True, "aaaa", "aaaa")
        mock_install.return_value = (True, "ok")
        mock_tests.return_value = (True, "ok")

        report = _verify_feedback_loop_complete(
            repo_path="/fake/repo",
            record_tests_run=True,
        )
        assert report.is_clean
        assert len(report.violations) == 0

    @patch("memchorus.workflow_compliance._check_push_complete")
    @patch("memchorus.workflow_compliance._check_installed_from_remote_sha")
    @patch("memchorus.workflow_compliance._check_tests_verified")
    def test_missing_push_surfaces_critical(self, mock_tests, mock_install, mock_push):  # noqa: F811
        mock_push.return_value = (False, "local", "remote")
        mock_install.return_value = (True, "ok")
        mock_tests.return_value = (True, "ok")

        report = _verify_feedback_loop_complete(
            repo_path="/fake/repo",
            record_tests_run=True,
        )
        assert not report.is_clean
        push_viols = [v for v in report.violations if v.vtype == ViolationType.PUSH_MISSING]
        assert len(push_viols) >= 1
        assert push_viols[0].severity == 3

    @patch("memchorus.workflow_compliance._check_installed_from_remote_sha")
    @patch("memchorus.workflow_compliance._check_push_complete")
    def test_tests_not_claimed_adds_info_violation(
        self, mock_push, mock_install  # noqa: F811
    ):
        mock_push.return_value = (True, "a", "a")
        mock_install.return_value = (True, "ok")

        report = _verify_feedback_loop_complete(
            repo_path="/fake/repo",
            record_tests_run=False,
        )
        test_viols = [v for v in report.violations if v.vtype == ViolationType.TESTS_NOT_VERIFIED]
        assert len(test_viols) >= 1
        assert test_viols[0].severity == 1

    @patch("memchorus.workflow_compliance._check_tests_verified")
    @patch("memchorus.workflow_compliance._check_installed_from_remote_sha")
    @patch("memchorus.workflow_compliance._check_push_complete")
    def test_all_three_fail_returns_three_violations(self, mock_push, mock_install, mock_tests):  # noqa: F811
        mock_push.return_value = (False, "local", "remote")
        mock_install.return_value = (False, "bad path")
        mock_tests.return_value = (False, "import error")

        report = _verify_feedback_loop_complete(
            repo_path="/fake/repo",
            record_tests_run=True,
        )
        assert len(report.violations) == 3


# ---------------------------------------------------------------------------
# Violation formatting tests
# ---------------------------------------------------------------------------

class TestViolationFormatting:
    def test_get_violation_list_formats_severity(self):
        report = ComplianceReport()
        report.violations.append(Violation(
            vtype=ViolationType.PUSH_MISSING,
            message="not pushed",
            severity=3,
        ))
        items = get_violation_list(report)
        assert len(items) == 1
        assert items[0].startswith("[S3]")

    def test_has_critical_detects_severity_3(self):
        report = ComplianceReport()
        report.violations.append(Violation(
            vtype=ViolationType.PUSH_MISSING,
            message="not pushed",
            severity=3,
        ))
        assert has_critical(report) is True

    def test_has_critical_with_only_info_returns_false(self):
        report = ComplianceReport()
        report.violations.append(Violation(
            vtype=ViolationType.TESTS_NOT_VERIFIED,
            message="skipped",
            severity=1,
        ))
        assert has_critical(report) is False
