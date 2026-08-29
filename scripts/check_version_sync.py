#!/usr/bin/env python3
"""Four-way version-sync gate — setup.py vs README vs __init__.py vs pyproject.toml.

Purpose
-------
Fail fast (CI or pre-commit) when the *declared* package version drifts from
the canonical version, including the case where a *separate* packaging file
carries a stale hard-coded version that shadows the canonical one.

Why four, not three
-------------------
The 2026-08-29 repo audit found `src/pyproject.toml` declaring `version = "1.5.4"`
while the rest of the repo (setup.py via get_version(), README, __init__.py) was
at 2.0.15. If pip ever resolves the build from that pyproject.toml, the published
package would be 1.5.4 even though the runtime reports 2.0.15. That is exactly
the class of bug this gate exists to catch, so the pyproject is in scope.

The four sources that MUST agree:
  1. `src/memchorus/__init__.py`          ->  `__version__`          (runtime source of truth)
  2. `setup.py`                           ->  `version=` kwarg       (packaging)
  3. `README.md`                          ->  top changelog banner   (what users read)
  4. `src/pyproject.toml` / `pyproject.toml` -> `[project] version=` (PEP 621)

setup.py note: this repo's setup.py computes its version via a `get_version()`
helper that itself reads `__version__` from __init__.py. A `version=get_version()`
form is *structurally* equal to __init__.py by construction; we resolve that
indirection explicitly so a future hard-coded version in setup.py that diverges
still gets caught.

Exit codes
----------
  0  all present sources agree
  1  drift detected, or a required source could not be read
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT_PATH = os.path.join(REPO_ROOT, "src", "memchorus", "__init__.py")
SETUP_PATH = os.path.join(REPO_ROOT, "setup.py")
README_PATH = os.path.join(REPO_ROOT, "README.md")
PYPROJECT_CANDIDATES = (
    os.path.join(REPO_ROOT, "src", "pyproject.toml"),
    os.path.join(REPO_ROOT, "pyproject.toml"),
)


def _read(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _pick_first_path(candidates):
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def get_init_version():
    """(`__version__` value or None, path). Canonical runtime value."""
    text = _read(INIT_PATH)
    if not text:
        return None, INIT_PATH
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return (m.group(1).strip(), INIT_PATH) if m else (None, INIT_PATH)


def get_setup_version():
    """Version that setup.py will actually declare.

    - hard-coded  version="X.Y.Z"           -> X.Y.Z
    - delegated   version=get_version()     -> resolves to __init__.py value
    - delegated   version=<other_helper>()  -> resolves to __init__.py value
    - no version= keyword at all            -> None (caller fails loudly)
    """
    text = _read(SETUP_PATH)
    if not text:
        return None, SETUP_PATH
    m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', text)
    if m:
        return (m.group(1).strip(), SETUP_PATH)
    if re.search(r'version\s*=\s*[\w.]+\s*\(', text):
        v, _ = get_init_version()
        return (v, SETUP_PATH)
    return (None, SETUP_PATH)


def get_readme_version():
    """Top changelog banner version from README.md (first `### vX.Y.Z` header)."""
    text = _read(README_PATH)
    if not text:
        return None, README_PATH
    versions = re.findall(r'^\s*#{1,6}\s+v?(\d+\.\d+\.\d+)', text, re.MULTILINE)
    return (versions[0], README_PATH) if versions else (None, README_PATH)


def get_pyproject_version():
    """`[project] version = "X.Y.Z"` from the first pyproject.toml that exists."""
    path = _pick_first_path(PYPROJECT_CANDIDATES)
    if path is None:
        return None, None
    text = _read(path)
    if not text:
        return None, path
    m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return (m.group(1).strip(), path) if m else (None, path)


def main():
    init_v, init_p = get_init_version()
    setup_v, setup_p = get_setup_version()
    readme_v, readme_p = get_readme_version()
    pyproj_v, pyproj_p = get_pyproject_version()

    print(f"__init__.py   : {init_v or '-'}   ({init_p or '<missing>'})")
    print(f"setup.py      : {setup_v or '-'}   ({setup_p or '<missing>'})")
    print(f"README.md     : {readme_v or '-'}   ({readme_p or '<missing>'})")
    print(f"pyproject.toml: {pyproj_v or '-'}   ({pyproj_p or '<absent>'})")

    # The mandatory three (a repo without a pyproject.toml still ships).
    for name, val in (("__init__.py", init_v), ("setup.py", setup_v), ("README.md", readme_v)):
        if not val:
            print(f"FAIL: could not resolve version in required source: {name}", file=sys.stderr)
            return 1

    def pair_report(sources):
        for i in range(len(sources)):
            for j in range(i + 1, len(sources)):
                a_name, a_v = sources[i]
                b_name, b_v = sources[j]
                if a_v != b_v:
                    print(f"  {a_name}={a_v}  {b_name}={b_v}", file=sys.stderr)

    # If pyproject is absent, the three-way agreement is sufficient.
    if pyproj_v is None:
        if init_v == setup_v == readme_v:
            print(f"OK: version aligned at {init_v} (pyproject.toml absent — 3-way check)")
            return 0
        print("DRIFT: mandatory three sources disagree", file=sys.stderr)
        pair_report([("__init__.py", init_v), ("setup.py", setup_v), ("README.md", readme_v)])
        return 1

    # All four present: all must agree.
    pyproj_label = os.path.basename(pyproj_p) if pyproj_p else "pyproject.toml"
    sources = [
        ("__init__.py", init_v),
        ("setup.py", setup_v),
        ("README.md", readme_v),
        (pyproj_label, pyproj_v),
    ]
    distinct = list(dict.fromkeys(v for _, v in sources))
    if len(distinct) == 1:
        print(f"OK: version aligned at {init_v} across all {len(sources)} sources")
        return 0
    print(f"DRIFT: {len(sources)} sources present and disagree ({len(distinct)} distinct values):",
          file=sys.stderr)
    if len(distinct) == 2:
        lo, hi = sorted(distinct)
        for name, v in sources:
            tag = "LOW " if v == lo else "HIGH"
            print(f"  {tag}: {name}={v}", file=sys.stderr)
        print("  (they must be equal)", file=sys.stderr)
    else:
        pair_report(sources)
    return 1


if __name__ == "__main__":
    sys.exit(main())
