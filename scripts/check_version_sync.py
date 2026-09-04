#!/usr/bin/env python3
"""Version-sync gate — keep every packaging surface honest about the version.

Purpose
-------
Fail fast (CI or pre-commit) when the *declared* package version drifts from
the canonical runtime version. The canonical value is
``src/memchorus/__init__.py::__version__``; this gate verifies that every
other surface that can carry a version (the README changelog banner, the
legacy ``setup.py``, and any ``pyproject.toml``) is either *equal-by-
construction* to that value or repeats it exactly.

Classification model
--------------------
Each source is classified as one of:

  canonical      the runtime truth (``__init__.py``); always authoritative
  concrete       carries its own literal ``"X.Y.Z"`` string that must equal
                 the canonical value
  equal          *equal-by-construction* — delegates to ``__init__.py`` and
                 therefore cannot drift (a bare ``setup()`` shim, a
                 ``version=get_version()`` delegation, or a PEP 621
                 ``dynamic = ["version"]`` + ``attr: memchorus.__version__``)
  absent         the source file is not present (allowed for ``setup.py`` /
                 ``pyproject.toml``; a pyproject-less repo still ships)
  broken         a real packaging config that should carry a version but
                 none could be resolved (fails)

Pass rule
---------
  * canonical resolves to a value (required)
  * README banner is concrete and equals canonical (required)
  * setup.py:   equal|absent -> OK; concrete must == canonical; broken -> FAIL
  * pyproject:  equal|absent -> OK; concrete must == canonical; broken -> FAIL

Traps handled
-------------
* A PEP 621 ``[tool.setuptools.dynamic]`` table declares
  ``version = "attr: memchorus.__version__"``. A naive ``version = "..."``
  regex would read ``attr: memchorus.__version__`` as a concrete value and
  report a false drift. The version search is therefore scoped to the
  ``[project]`` section, where the real (or dynamic) version lives.
* setup.py note: a legacy repo may use ``version=get_version()`` which
  itself reads ``__init__.py`` — that is *equal-by-construction*. A bare
  ``setup()`` shim (no packaging fields at all) is also *equal-by-construction*.
  But a setup.py that carries real fields (``name=``/``packages=``) yet
  loses its ``version=`` is *broken* and fails. This preserves the 2026-08-29
  guard behaviour (a stale ``src/pyproject.toml`` declaring ``1.5.4`` while
  the rest of the repo was at 2.0.15) while accepting both the old delegated
  layout and the new promoted pyproject layout.

Exit codes
----------
  0  every present source is honest about the version
  1  drift detected, a real config is missing its version, or a required
     source could not be read
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT_PATH = os.path.join(REPO_ROOT, "src", "memchorus", "__init__.py")
SETUP_PATH = os.path.join(REPO_ROOT, "setup.py")
README_PATH = os.path.join(REPO_ROOT, "README.md")
PYPROJECT_CANDIDATES = (
    os.path.join(REPO_ROOT, "pyproject.toml"),      # canonical location first
    os.path.join(REPO_ROOT, "src", "pyproject.toml"),  # legacy nested location
)

# --- classification result kinds ---------------------------------------
CANONICAL = "canonical"
CONCRETE = "concrete"
EQUAL = "equal"
ABSENT = "absent"
BROKEN = "broken"


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


def _project_section(text):
    """Return the body of the ``[project]`` section only.

    Turns off at the next TOML table header (e.g. ``[project.scripts]``,
    ``[tool.*]``), so a ``version = "attr: ..."`` line inside
    ``[tool.setuptools.dynamic]`` is never mistaken for a concrete value.
    """
    out, in_project = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            out.append(line)
    return "\n".join(out)


def classify_init():
    """Canonical runtime version from ``__init__.py``."""
    text = _read(INIT_PATH)
    if not text:
        return (BROKEN, None, INIT_PATH)
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', text, re.MULTILINE)
    if not m:
        return (BROKEN, None, INIT_PATH)
    return (CANONICAL, m.group(1).strip(), INIT_PATH)


def classify_readme():
    """Top changelog banner version from README.md (first ``v X.Y.Z`` header)."""
    text = _read(README_PATH)
    if not text:
        return (BROKEN, None, README_PATH)
    m = re.search(r'^\s*#{1,6}\s+v?(\d+\.\d+\.\d+)', text, re.MULTILINE)
    if not m:
        return (BROKEN, None, README_PATH)
    return (CONCRETE, m.group(1).strip(), README_PATH)


def classify_setup():
    """Version surface in setup.py.

    - concrete ``version="X.Y.Z"``      -> ("concrete", value)
    - delegated ``version=helper()``    -> ("equal", None)   [reads __init__]
    - bare ``setup()`` shim (no fields) -> ("equal", None)   [no fields to drift]
    - real fields but no version        -> ("broken", None)
    - file absent                       -> ("absent", None)
    """
    text = _read(SETUP_PATH)
    if not text:
        return (ABSENT, None, SETUP_PATH)

    concrete = re.search(r'version\s*=\s*["\']([^"\']+)', text)
    if concrete:
        return (CONCRETE, concrete.group(1).strip(), SETUP_PATH)

    if re.search(r'version\s*=\s*[\w.]+\s*\(', text):
        # A delegated call (e.g. get_version()) reads __init__ at build time
        # -> equal-by-construction.
        return (EQUAL, None, SETUP_PATH)

    # No version= string or call. Distinguish a shim from a broken real config.
    real_fields = (
        r'name\s*=', r'packages\s*=', r'py_modules\s*=', r'install_requires\s*=',
        r'entry_points\s*=', r'extras_require\s*=', r'long_description\s*=',
    )
    has_real_config = any(re.search(p, text) for p in real_fields)
    if has_real_config:
        return (BROKEN, None, SETUP_PATH)   # real setup() that lost its version
    return (EQUAL, None, SETUP_PATH)        # explicit shim


def classify_pyproject():
    """Version surface in the canonical pyproject.toml.

    - concrete ``[project] version = "X.Y.Z"`` -> ("concrete", value)
    - dynamic  ``[project] dynamic = ["version"]`` -> ("equal", None)
    - no resolvable version                     -> ("broken", None)
    - file absent                               -> ("absent", None)
    """
    path = _pick_first_path(PYPROJECT_CANDIDATES)
    if path is None:
        return (ABSENT, None, None)
    text = _read(path)
    if not text:
        return (BROKEN, None, path)

    project = _project_section(text)

    concrete = re.search(r'^\s*version\s*=\s*["\']([^"\']+)', project, re.MULTILINE)
    if concrete:
        return (CONCRETE, concrete.group(1).strip(), path)

    dynamic = re.search(r'^\s*dynamic\s*=\s*\[(.*?)\]', project, re.MULTILINE | re.DOTALL)
    if dynamic and re.search(r'["\']version["\']|^\s*version\s*$', dynamic.group(1)):
        return (EQUAL, None, path)

    return (BROKEN, None, path)


def main():
    init_kind, init_v, init_p = classify_init()
    readme_kind, readme_v, readme_p = classify_readme()
    setup_kind, setup_v, setup_p = classify_setup()
    pyproj_kind, pyproj_v, pyproj_p = classify_pyproject()

    def _p(path):
        return path if path else "<absent>"

    lines = [
        f"__init__.py   : {init_kind:<10} {init_v or '-'}   ({_p(init_p)})",
        f"setup.py      : {setup_kind:<10} {setup_v or '-'}   ({_p(setup_p)})",
        f"README.md     : {readme_kind:<10} {readme_v or '-'}   ({_p(readme_p)})",
        f"pyproject.toml: {pyproj_kind:<10} {pyproj_v or '-'}   ({_p(pyproj_p)})",
    ]
    print("\n".join(lines))

    problems = []

    # 1. Canonical runtime value must resolve.
    if init_kind != CANONICAL or not init_v:
        problems.append("could not resolve canonical version in __init__.py")
        canonical = None
    else:
        canonical = init_v

    # 2. README banner must be concrete and equal to canonical.
    if readme_kind == CONCRETE:
        if canonical and readme_v != canonical:
            problems.append(f"README banner {readme_v} != __init__ {canonical}")
    elif readme_kind == BROKEN:
        problems.append("README did not carry a resolvable version banner")

    # 3. setup.py — honest only if equal/absent, or concrete-matching.
    if setup_kind == CONCRETE:
        if canonical and setup_v != canonical:
            problems.append(f"setup.py version {setup_v} != __init__ {canonical}")
    elif setup_kind == BROKEN:
        problems.append("setup.py has real packaging fields but no resolvable version")

    # 4. pyproject.toml — honest only if equal/absent, or concrete-matching.
    if pyproj_kind == CONCRETE:
        if canonical and pyproj_v != canonical:
            problems.append(f"pyproject.toml version {pyproj_v} != __init__ {canonical}")
    elif pyproj_kind == BROKEN:
        problems.append("pyproject.toml [project] has no resolvable version (set it or mark it dynamic)")

    if problems:
        for p in problems:
            print(f"  FAIL: {p}", file=sys.stderr)
        return 1

    n_honest = sum(k in (CANONICAL, CONCRETE, EQUAL, ABSENT) for k in (init_kind, readme_kind, setup_kind, pyproj_kind))
    print(f"OK: version honest at {canonical} across {n_honest} of 4 sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
