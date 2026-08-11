"""Unit tests for the canonical _sanitize_profile implementation.

This file exercises every branch of ``memchorus._sanitize_profile`` which lives
in ``__init__.py`` as a compiled-regex whitelist validator that prevents
OSError 36 (File name too long) when HERMES_PROFILE contains corrupted content.

See Kanban task t_98852cbd for the DRY consolidation context.
"""

import logging
import pytest
from memchorus import _sanitize_profile


class TestSanitizeProfile:
    """Comprehensive tests for _sanitize_profile shared implementation."""

    # --- Valid inputs ---------------------------------------------------

    def test_valid_simple_alpha(self):
        assert _sanitize_profile("default") == "default"

    def test_valid_with_numbers(self):
        assert _sanitize_profile("cthugha-9") == "cthugha-9"

    def test_valid_uppercase(self):
        assert _sanitize_profile("Agent123") == "Agent123"

    def test_valid_with_underscore(self):
        assert _sanitize_profile("bubo_test") == "bubo_test"

    def test_valid_with_hyphen(self):
        assert _sanitize_profile("my-profile-name") == "my-profile-name"

    @pytest.mark.parametrize(
        "profile",
        ["a_z-A1", "X1", "test-profile-123_abc"],
    )
    def test_valid_various_combinations(self, profile):
        assert _sanitize_profile(profile) == profile

    # --- Empty / None inputs -------------------------------------------

    def test_empty_string_returns_default(self):
        assert _sanitize_profile("") == "default"

    def test_none_returns_default(self):
        assert _sanitize_profile(None) == "default"

    # --- Boundary lengths (max 48 chars) ---------------------------------

    def test_exactly_48_chars_is_valid(self):
        profile = "a" * 48
        assert _sanitize_profile(profile) == profile

    def test_49_chars_falls_back_to_default(self):
        # Just over the limit - regex rejects, logs warning, returns default
        profile = "a" * 49
        assert _sanitize_profile(profile) == "default"

    def test_too_long_falls_back(self):
        huge = "x" * 200
        result = _sanitize_profile(huge)
        assert result == "default"

    # --- Path traversal attempts -----------------------------------------

    def test_dot_dot_slash_rejected(self):
        assert _sanitize_profile("../etc/passwd") == "default"

    def test_absolute_path_rejected(self):
        assert _sanitize_profile("/home/bubo/.hermes") == "default"

    def test_backslash_traversal_rejected(self):
        assert _sanitize_profile("..\\windows\\system32") == "default"

    # --- Dots in profile names (not valid chars for our regex) -----------

    def test_dot_in_name_rejected(self):
        assert _sanitize_profile("bubo.config") == "default"

    def test_trailing_dot_rejected(self):
        assert _sanitize_profile("profile.") == "default"

    def test_leading_dot_rejected(self):
        assert _sanitize_profile(".hidden") == "default"

    # --- Unicode characters (not in our ASCII whitelist) -----------------

    @pytest.mark.parametrize(
        "value",
        [
            "cafe\u0301",       # unicode combining char
            "\u6d4b\u8bd5",      # CJK chars
            "\u0645\u0631\u062d\u0628\u0627",  # Arabic
        ],
    )
    def test_unicode_rejected(self, value):
        assert _sanitize_profile(value) == "default"

    # --- Special chars rejected -----------------------------------------

    @pytest.mark.parametrize(
        "value",
        [
            "name with spaces",
            "profile@domain",
            "user#123",
            "test\nnewline",
        ],
    )
    def test_special_chars_rejected(self, value):
        assert _sanitize_profile(value) == "default"

    # --- Warning logging on invalid input -------------------------------

    def test_warning_logged_on_invalid_input(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = _sanitize_profile("INVALID SPACE!")
        assert result == "default"
        assert "HERMES_PROFILE contained invalid value" in caplog.text
        assert "falling back to 'default'" in caplog.text
