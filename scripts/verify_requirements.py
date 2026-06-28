#!/usr/bin/env python3
"""Ad-hoc doc-sync verification for MemChorus Requirements.md.

Checks every claim in the requirements checklist against ground truth
by inspecting the actual source tree, test suite, and plugin wiring.

Run from repo root:  python scripts/verify_requirements.py
Also callable from any working dir by passing --repo <path>.
Exit 0 = docs match reality. Exit 1 = drift detected.
"""

import argparse
import ast
import os
import sys


def module_exists(repo_root, name):
    """Return True if src/memchorus/<name>.py exists."""
    p = os.path.join(repo_root, "src", "memchorus", f"{name}.py")
    return os.path.isfile(p)


def method_exists_in_file(repo_root, basename, method_name):
    """Parse the module and return True if a def *method_name* is present."""
    p = os.path.join(repo_root, "src", "memchorus", f"{basename}.py")
    try:
        with open(p) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == method_name:
                return True
    except Exception:
        pass
    return False


def keyword_in_file(repo_root, basename, keyword):
    """Return True if *keyword* appears somewhere in the file."""
    p = os.path.join(repo_root, "src", "memchorus", f"{basename}.py")
    try:
        with open(p) as f:
            return keyword in f.read()
    except Exception:
        return False


def run_checks(repo_root):
    errors = []
    warns = []

    # --- Core Interface ---
    for m in ['memory_source', 'hermes_memory_source', 'mempalace_memory_source',
              'orchestrator', 'behavioral_trigger', 'enforcement_manager',
              'auto_storage_engine', 'relevance_engine']:
        if not module_exists(repo_root, m):
            errors.append(f"Missing module: src/memchorus/{m}.py")

    abc_methods = {'save', 'retrieve', 'search', 'is_available', 'get_source_info'}
    mem_source_p = os.path.join(repo_root, "src", "memchorus", "memory_source.py")
    found_abc = set()
    try:
        with open(mem_source_p) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == 'MemorySource':
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        found_abc.add(item.name)
    except Exception:
        pass
    missing = abc_methods - found_abc
    if missing:
        errors.append(f"MemorySource ABC missing methods: {missing}")

    # --- B-2 recommended_sources on Orchestrator ---
    if not method_exists_in_file(repo_root, "orchestrator", "recommended_sources"):
        errors.append("Orchestrator missing recommended_sources() — B-2 claim is false")

    # --- B-2 capture_outcome wires through recommended_sources ---
    if not keyword_in_file(repo_root, "auto_storage_engine", "recommended_sources"):
        errors.append("auto_storage_engine does NOT call recommended_sources() — B-2 wiring missing")

    # --- Plugin hooks wired ---
    plugin_init = os.path.join(os.environ.get("HOME", "~"), ".hermes", "plugins",
                               "hermes-memchorus", "__init__.py")
    if not os.path.isfile(plugin_init):
        errors.append("Plugin __init__.py not found — hooks cannot be wired")
    else:
        with open(plugin_init) as f:
            pi = f.read()
        for hook in ['pre_llm_call', 'post_tool_call']:
            if hook not in pi:
                errors.append(f"Plugin missing '{hook}' hook registration")

    # --- DecisionPoint types (B-1) ---
    if not method_exists_in_file(repo_root, "behavioral_trigger", "DecisionPoint"):
        warns.append("No DecisionPoint enum/class in behavioral_trigger.py — pre-decision recall may be incomplete")

    # --- setup.py deps ---
    setup_p = os.path.join(repo_root, "setup.py")
    if os.path.isfile(setup_p):
        with open(setup_p) as f:
            setup_text = f.read()
        has_deps = "install_requires=" in setup_text
        non_empty = '["' in setup_text + "['" in setup_text  # rough heuristic
        if has_deps and non_empty:
            warns.append("setup.py lists external dependencies — doc claim of zero deps may be stale")

    # --- Test suite sanity ---
    tests_dir = os.path.join(repo_root, "tests")
    test_files = [f for f in os.listdir(tests_dir) if f.endswith('.py')] if os.path.isdir(tests_dir) else []
    if len(test_files) < 5:
        warns.append(f"Only {len(test_files)} test files found — comprehensive coverage claim is shaky")

    return errors, warns


def main():
    parser = argparse.ArgumentParser(description="Verify Requirements.md matches codebase.")
    parser.add_argument("--repo", default=None, help="Path to MemChorus repo root.")
    args = parser.parse_args()

    repo_root = args.repo if args.repo else os.getcwd()
    errors, warns = run_checks(repo_root)

    for w in warns:
        print(f"WARN: {w}")
    for e in errors:
        print(f"FAIL: {e}")

    if errors:
        print(f"\n{len(errors)} FAILURE(S) — requirements doc does NOT match codebase.")
        sys.exit(1)
    else:
        print(f"\nDRIFT CHECK PASS — no discrepancies between requirements claims and source tree (warnings: {len(warns)}).")
        sys.exit(0)


if __name__ == "__main__":
    main()
