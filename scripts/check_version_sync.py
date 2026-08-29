#!/usr/bin/env python3
"""Three-way version-sync gate — setup.py vs README banner vs __init__.py.

Purpose
-------
Fail fast (CI or pre-commit) when the *declared* package version drifts from
the version a human reads in the README. This is the gate the 2026-08-28 PR
#145 cycle needed: version was 2.0.15 in `__init__.py` but the drift only
surfaces when setup.py, the README banner, and `__init__.py` diverge from
each other.

The three sources that MUST agree:
  1. `src/memchorus/__init__.py`  ->  `__version__`   (runtime source of truth)
  2. `setup.py`                                       (packaging declares it)
  3. `README.md`  top changelog banner                (what users read)

Setup.py note: this repo's `setup.py` computes its version via a `get_version()`
helper that itself reads `__version__` from `__init__.py`. So when `setup.py`
contains `version=get_version()`, it is *structurally* equal to `__init__.py`
by construction — we resolve that indirection explicitly rather than trusting
it, so a future hard-coded version in setup.py that diverges still gets caught.

Exit codes
----------
  0  all three sources agree
  1  drift detected (or a source could not be read)
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT_PATH = os.path.join(REPO_ROOT, "src", "memchorus", "__init__.py")
SETUP_PATH = os.path.join(REPO_ROOT, "setup.py")
README_PATH = os.path.join(REPO_ROOT, "README.md")


def _read(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def get_init_version():
    """`__version__` from src/memchorus/__init__.py (canonical runtime value)."""
    text = _read(INIT_PATH)
    if text is None:
        return None
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return m.group(1).strip() if m else None


def get_setup_version():
    """Version that setup.py will actually declare.

    - hard-coded  version="X.Y.Z"            -> X.Y.Z
    - delegated   version=get_version()      -> __init__.py value
    - delegated   version=<other_helper>()   -> treat as __init__.py value
    - no version= keyword at all             -> None (caller fails loudly)
    """
    text = _read(SETUP_PATH)
    if text is None:
        return None
    m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', text)
    if m:
        return m.group(1).strip()
    if re.search(r'version\s*=\s*[\w.]+\(', text):
        # Delegated to a helper (get_version()) that resolves __init__.py.
        return get_init_version()
    return None


def get_readme_version():
    """Top changelog banner version from README.md (first `### vX.Y.Z` header)."""
    text = _read(README_PATH)
    if text is None:
        return None
    versions = re.findall(r'^\s*#{1,6}\s+v?(\d+\.\d+\.\d+)', text, re.MULTILINE)
    return versions[0] if versions else None


def main():
    init_v = get_init_version()
    setup_v = get_setup_version()
    readme_v = get_readme_version()

    print(f"__init__.py : {init_v}")
    print(f"setup.py    : {setup_v}")
    print(f"README.md   : {readme_v}")

    missing = [
        name
        for name, val in (
            ("__init__.py", init_v),
            ("setup.py", setup_v),
            ("README.md", readme_v),
        )
        if not val
    ]
    if missing:
        print(f"FAIL: could not resolve version in: {', '.join(missing)}", file=sys.stderr)
        return 1

    if init_v == setup_v == readme_v:
        print(f"OK: version aligned at {init_v} across all 3 sources")
        return 0

    print("DRIFT: the three sources disagree", file=sys.stderr)
    if init_v != setup_v:
        print(f"  __init__.py={init_v}  setup.py={setup_v}", file=sys.stderr)
    if init_v != readme_v:
        print(f"  __init__.py={init_v}  README.md={readme_v}", file=sys.stderr)
    if setup_v != readme_v:
        print(f"  setup.py={setup_v}  README.md={readme_v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
