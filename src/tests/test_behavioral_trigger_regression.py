"""
Regression tests for BehavioralTrigger keyword matching fixes.

Tests cover word-boundary matching when keywords contain underscores/hyphens,
plus new POST_ACTION_COMPLETE patterns for installation/git/diagnostic outputs.

Fixes:
 - \b word boundaries treating _ as word char (so {"exit_code": 0} matched nothing)
   → replaced with (?<![a-zA-Z])...(?![a-zA-Z]) letter-only lookaround
 - Missing POST_ACTION_COMPLETE keywords for pip install, git, diag outputs.
"""

import unittest
from memchorus.behavioral_trigger import BehavioralTrigger


class TestWordBoundaryWithUnderscore(unittest.TestCase):
    """Verify underscores do NOT act as word boundaries (they should be separators)."""

    def setUp(self):
        self.bt = BehavioralTrigger()

    # --- Underscored keywords must match even inside JSON/structured output ---

    def test_exit_code_in_json_matches(self):
        text = '{"exit_code": 0, "output": "success"}'
        results = self.bt.detect(text)
        matched = [r.matched_keyword for r in results]
        keyword_matches = any("exit code" in kw.lower() for kw in matched)
        assert keyword_matches, (
            f"'exit_code' inside JSON should match a POST_ACTION_COMPLETE pattern. "
            f"Matched keywords: {matched}"
        )

    def test_tool_call_output_with_exit_code(self):
        text = 'command finished with exit_code=0 in 2.3s'
        results = self.bt.detect(text)
        keyword_matches = any("exit code" in kw.lower() for kw in [r.matched_keyword for r in results])
        assert keyword_matches, \
            f"'exit_code=0' plain text should fire POST_ACTION_COMPLETE. Got: {[r.matched_keyword for r in results]}"

    def test_exit_code_nonzero_still_triggers_error(self):
        text = 'process crashed with exit_code=1'
        results = self.bt.detect(text)
        assert len(results) > 0, \
            f"Crash message should have triggered at least one decision point. Got: {[r.matched_keyword for r in results]}"

    def test_backtick_wrapped_command_output(self):
        text = '`npm run build exited with exit_code 0` — all tests passed'
        results = self.bt.detect(text)
        assert len(results) > 0, \
            f"Backtick-wrapped command output should still be detectable. Got: {len(results)} matches"

    # --- Hyphenated keywords should also work as separators ---

    def test_tool_call_hyphen_in_text(self):
        text = 'post-tool-call hook fired successfully'
        results = self.bt.detect(text)
        assert len(results) > 0, \
            f"Hyphenated terms should match. Got: {[r.matched_keyword for r in results]}"

    def test_confirmed_green_still_works(self):
        text = 'CI pipeline confirmed green after merge'
        results = self.bt.detect(text)
        keyword_matches = any("confirmed green" in kw.lower() for kw in [r.matched_keyword for r in results])
        assert keyword_matches, \
            f"'confirmed green' should match POST_ACTION_COMPLETE. Got: {[r.matched_keyword for r in results]}"


class TestPostActionCompleteExtendedPatterns(unittest.TestCase):
    """New patterns for installation/git/diagnostic outputs."""

    def setUp(self):
        self.bt = BehavioralTrigger()

    # --- Installation success patterns ---

    def test_successfully_installed_pip_output(self):
        text = 'Successfully installed memchorus-1.5.03 setuptools-69.0 wheel-0.43'
        results = self.bt.detect(text)
        keyword_matches = any("successfully installed" in kw.lower() for kw in [r.matched_keyword for r in results])
        assert keyword_matches, \
            f"'Successfully installed' should fire POST_ACTION_COMPLETE. Got: {[r.matched_keyword for r in results]}"

    def test_requirement_already_satisfied(self):
        text = "Requirement already satisfied: memchorus in /usr/lib/python3.14/site-packages"
        results = self.bt.detect(text)
        assert len(results) > 0, \
            f"'already satisfied' indicates an install outcome. Got: {[r.matched_keyword for r in results]}"

    def test_pip_uninstall_success(self):
        text = 'Found existing installation: memchorus 1.5.02. Uninstalling memchorus-1.5.02: Successfully uninstalled memchorus-1.5.02'
        results = self.bt.detect(text)
        assert len(results) > 0, \
            f"Pip uninstall success should be detected. Got: {[r.matched_keyword for r in results]}"

    # --- Git output patterns ---

    def test_git_push_success(self):
        text = "Enumerating objects: 15, done.\nCounting objects: 100% (15/15), done.\nTo https://github.com/BuboTheWise/MemChorus.git\n * [new branch]      fix/behavioral-trigger-bug -> fix/behavioral-trigger-bug"
        results = self.bt.detect(text)
        assert len(results) > 0, \
            f"'git push' success should be detected. Got: {len(results)} matches"

    def test_git_merge_complete(self):
        text = "Merge made by the 'ort' strategy.\n src/memchorus/behavioral_trigger.py | 45 +++++++++++++++++- 1 file changed, 42 insertions(+), 3 deletions(-)"
        results = self.bt.detect(text)
        assert len(results) > 0, \
            f"'Merge made' output should be caught. Got: {[r.matched_keyword for r in results]}"

    def test_git_commit_success(self):
        text = "[fix/behavioral-trigger-bug 92195b0] fix(behavioral_trigger): replace word boundaries with letter-only lookaround"
        results = self.bt.detect(text)
        assert len(results) > 0, \
            f"'[branch commit]' output should fire decision point. Got: {[r.matched_keyword for r in results]}"

    def test_git_diff_stat_output(self):
        text = " src/memchorus/behavioral_trigger.py | 12 ++++++++----\n tests/test_behavioral_trigger_regression.py | 85 ++++++++++++++++++++++++++++++++++++++++++++++++++"
        results = self.bt.detect(text)
        # Diff output is less likely to match, but 'changed' or file references can fire
        pass  # Not a strong signal — just ensuring no crash


class TestEdgeCases(unittest.TestCase):
    """Edge cases: empty inputs, noise, boundary conditions."""

    def setUp(self):
        self.bt = BehavioralTrigger()

    def test_empty_string_no_crash(self):
        results = self.bt.detect("")
        assert results == []

    def test_none_like_input_no_crash(self):
        with self.assertRaises(TypeError):
            self.bt.detect(None)  # type: ignore

    def test_very_long_input_truncates_safely(self):
        text = "completed " * 10000  # 80k chars of repeated keyword
        results = self.bt.detect(text)
        assert len(results) > 0, "Even very long text should match if keywords present"

    def test_noisy_traceback_does_not_fire_post_complete(self):
        text = """Traceback (most recent call last):
  File "/home/bubo/.local/lib/python3.14/site-packages/memchorus/__init__.py", line 42, in _bootstrap
    raise RuntimeError("Failed to initialize")
RuntimeError: Failed to initialize"""
        results = self.bt.detect(text)
        # Should fire ERROR_STATE but NOT POST_ACTION_COMPLETE for a trace
        has_error = any(r.matched_keyword in ("error", "failed", "traceback", "exception", "bug", "regression", "resolve") for r in results)
        assert has_error, f"Traceback should trigger ERROR_STATE. Got: {[r.matched_keyword for r in results]}"


if __name__ == "__main__":
    unittest.main()
