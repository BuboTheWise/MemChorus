"""Natural-language integration test prompts for MemChorus end-to-end verification."""


NATURAL_TEST_PROMPT = """\
Investigate the test coverage of three Python modules in the project at /tmp/mc_test_tree/src/memchorus/: adaptive_threshold, behavioral_trigger, and lifecycle_eviction.

Do this step by step:
1. Plan your approach for checking whether these modules have sufficient test coverage and what gaps remain
2. Execute tool calls to actually inspect test files and run pytest with --cov-report on those specific modules
3. Save your concrete findings (coverage percentages, which lines are missing) so they persist between sessions
4. Recall any prior analysis of these same modules from earlier sessions to compare whether things improved or regressed

Important: use real tool calls — terminal commands, file reads, Python imports. Do not fabricate data. If anything errors out, document what failed and why before continuing. After finishing, produce a summary synthesis worth remembering for future reference.
"""


PLAN_VERIFICATION_PROMPT = """\
Review your current approach to handling the task you're assigned. Before doing any more work, write down in memory a concise plan describing what you've verified so far and what remains to confirm production readiness of MemChorus. Execute tool calls to inspect the current state. After saving it, recall it back within this same session to prove roundtrip persistence is working. Report whether the saved content matches what you just wrote.
"""


ERROR_RECOVERY_PROMPT = """\
Plan how you'll intentionally trigger and then diagnose a failure. Execute a command that will fail (e.g., try to import a module that doesn't exist), then immediately attempt to recover from the error. Document both the error state and your recovery steps in memory so they serve as a debugging reference later. After handling it, verify you can recall what went wrong by searching for 'error' or 'failure' keywords.
"""


FULL_PIPELINE_PROMPT = """\
You have four tasks to complete:

A) PLAN: Outline how you'll audit the memchorus.mempalace_persistent_session module for test coverage gaps and what new tests could close those gaps.

B) EXECUTE: Run pytest with coverage on that specific module. Actually execute the command — don't just describe it.

C) SAVE: Store your coverage findings as a durable note including specific percentages and line numbers remaining uncovered. Use explicit memory persistence so this survives session boundaries.

D) RECALL: Immediately after saving, search for what you just stored to verify roundtrip recall works. Confirm the content is findable.

After all four steps, report each step's outcome separately so I can tell whether the save and recall actually fired.
"""


ALL_PROMPTS = [
    ("natural_test", NATURAL_TEST_PROMPT),
    ("plan_verification", PLAN_VERIFICATION_PROMPT),
    ("error_recovery", ERROR_RECOVERY_PROMPT),
    ("full_pipeline", FULL_PIPELINE_PROMPT),
]


REQUIRED_KEYWORDS = {
    "PLANNING": ["plan", "approach", "strategy", "outline"],
    "TOOL_CALL": ["execute", "tool call", "command", "run"],
    "SAVE": ["save", "persist", "memory", "store"],
    "RECALL": ["recall", "search", "prior", "earlier"],
    "ERROR": ["error", "fail", "diagnose", "recover"],
}


def get_prompt(name: str) -> str | None:
    """Look up a prompt by name."""
    for n, text in ALL_PROMPTS:
        if n == name:
            return text
    return None