"""
Natural-Language Integration Tests for MemChorus.

These tests validate the synthetic prompts used to exercise the
full behavioral trigger -> MCP save/recall pipeline. Because the
actual prompt execution requires a live LLM agent loop, pytest can't
drive that itself - but we CAN verify every required keyword trigger
is present so the prompts are provably complete.

Usage:
    cd /project && source venv/bin/activate
    pytest tests/test_natural_integration.py -v
"""
from natural_language_prompts import ALL_PROMPTS, REQUIRED_KEYWORDS


def test_every_prompt_hits_planning_trigger():
    """All prompts must contain at least one PLANNING keyword."""
    for name, prompt in ALL_PROMPTS:
        keywords = [kw.lower() for kw in REQUIRED_KEYWORDS["PLANNING"]]
        hits = sum(1 for kw in keywords if kw in prompt.lower())
        assert hits > 0, (
            f"Prompt '{name}' misses all PLANNING keywords. "
            f"Needs at least one of: {REQUIRED_KEYWORDS['PLANNING']}"
        )


def test_every_prompt_hits_tool_call_trigger():
    """All prompts must contain at least one TOOL_CALL keyword."""
    for name, prompt in ALL_PROMPTS:
        keywords = [kw.lower() for kw in REQUIRED_KEYWORDS["TOOL_CALL"]]
        hits = sum(1 for kw in keywords if kw in prompt.lower())
        assert hits > 0, (
            f"Prompt '{name}' misses all TOOL_CALL keywords. "
            f"Needs at least one of: {REQUIRED_KEYWORDS['TOOL_CALL']}"
        )


def test_every_prompt_hits_save_trigger():
    """All prompts must contain at least one SAVE keyword."""
    for name, prompt in ALL_PROMPTS:
        keywords = [kw.lower() for kw in REQUIRED_KEYWORDS["SAVE"]]
        hits = sum(1 for kw in keywords if kw in prompt.lower())
        assert hits > 0, (
            f"Prompt '{name}' misses all SAVE keywords. "
            f"Needs at least one of: {REQUIRED_KEYWORDS['SAVE']}"
        )


def test_every_prompt_hits_recall_trigger():
    """All prompts must contain at least one RECALL keyword."""
    for name, prompt in ALL_PROMPTS:
        keywords = [kw.lower() for kw in REQUIRED_KEYWORDS["RECALL"]]
        hits = sum(1 for kw in keywords if kw in prompt.lower())
        assert hits > 0, (
            f"Prompt '{name}' misses all RECALL keywords. "
            f"Needs at least one of: {REQUIRED_KEYWORDS['RECALL']}"
        )


def test_error_recovery_prompt_hits_error_trigger():
    """The error recovery prompt specifically must contain ERROR keywords."""
    name, prompt = ALL_PROMPTS[2]  # error_recovery
    keywords = [kw.lower() for kw in REQUIRED_KEYWORDS["ERROR"]]
    hits = sum(1 for kw in keywords if kw in prompt.lower())
    assert hits > 0, (
        f"Prompt '{name}' misses all ERROR keywords. "
        f"Needs at least one of: {REQUIRED_KEYWORDS['ERROR']}"
    )


def test_prompt_lengths_are_reasonable():
    """Prompts should be long enough to be substantive but not absurdly long."""
    for name, prompt in ALL_PROMPTS:
        words = len(prompt.split())
        is_ok = 50 < words < 1000
        qualifier = "short" if words <= 50 else "long"
        assert is_ok, (
            f"Prompt '{name}' has {words} words - seems too {qualifier} "
            "for effective behavioral triggering."
        )


def test_prompts_reference_real_modules():
    """At least one prompt should reference actual MemChorus modules."""
    any_hit = False
    for _name, prompt in ALL_PROMPTS:
        if "memchorus" in prompt.lower() or "mempalace" in prompt.lower():
            any_hit = True
            break
    assert any_hit, "No prompt references real module names - tests would be hollow"


def test_prompts_no_empty_strings():
    """All prompts must contain actual content."""
    for name, prompt in ALL_PROMPTS:
        assert len(prompt.strip()) > 100, (
            f"Prompt '{name}' is too short to be a meaningful integration test"
        )
