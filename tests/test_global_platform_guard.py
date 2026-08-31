"""Guard: no test may write to the *global* ``os.name`` or ``sys.platform``.

Patching ``os.name`` (or the sibling ``sys.platform``) at the module level
flips ``pathlib.Path`` dispatch to a foreign ``PosixPath`` / ``WindowsPath``
class.  On a CI host of a different OS the resulting
``NotImplementedError: cannot instantiate '…Path' on your system`` escapes
past the test body into pytest's own report machinery (``_pytest/nodes.py``
builds ``Path(os.getcwd())``), so an entire ``test-*`` job dies with a pytest
``INTERNALERROR`` instead of a clean, readable failure.

That is exactly what took down both ``test-windows`` jobs (3.11 + 3.12) on the
v2.0.24 release: eight ``posix``-tier cases in ``test_hermes_home.py`` set the
*global* ``os.name = "posix"``.  The v2.0.25 fix routed those cases through
module-scoped fixtures that hand the *module under test* its own ``os`` view
(``monkeypatch.setattr(hermes_home_mod, "os", SimpleNamespace(name="posix"))``)
so the global dispatch constant is never touched.

This file makes that isolation mechanical.  It statically scans every
``tests/*.py`` module and fails the suite if any test *writes* to the global
``os.name`` / ``sys.platform`` — while leaving the sanctioned module-scoped
pattern, ordinary reads/comparisons (``os.name == "nt"``), and ``os.environ``
mutations untouched.  A self-test proves the detector actually catches the bad
forms, so this guard can never pass vacuously.

Wired into the existing ``pytest tests/`` invocation — no new runner step.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

# (module attribute written) -> the constant that is dangerous when global
_DANGEROUS = {
    "os": {"name"},
    "sys": {"platform"},
}


def _is_name(node: ast.AST, *names: str) -> bool:
    return isinstance(node, ast.Name) and node.id in names


def _is_const_str(node: ast.AST, *values: str) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in values
    )


def detect_global_platform_writes(tree: ast.AST) -> List[Tuple[int, str]]:
    """Return a list of ``(lineno, description)`` for global platform writes.

    Flags the *global* forms only:

      * ``os.name = …``            (direct / augmented / annotated assignment)
      * ``setattr(os, "name", …)``
      * ``monkeypatch.setattr(os, "name", …)``
      * ``patch.object(os, "name", …)``
      * ``patch("os.name")`` / ``patch("sys.platform")``
      * …and the analogous ``sys.platform`` forms.

    Does **not** flag:
      * reads / comparisons:  ``os.name == "nt"``
      * the sanctioned module-scoped form:
        ``monkeypatch.setattr(some_mod, "os", …)``   (first arg != bare os/sys)
      * ``os.environ[...] = …``   (env dict, not a dispatch constant)
      * ``types.SimpleNamespace(name=...)``   (a keyword, not an attribute)
    """
    hits: List[Tuple[int, str]] = []

    for node in ast.walk(tree):
        # ----------------------------------------------------------------
        # 1) Direct assignment to os.name / sys.platform
        # ----------------------------------------------------------------
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)):
                    continue
                mod = target.value.id
                attr = target.attr
                if mod in _DANGEROUS and attr in _DANGEROUS[mod]:
                    hits.append(
                        (node.lineno, f"direct assignment to global {mod}.{attr}")
                    )

        # ----------------------------------------------------------------
        # 2) Call-based setattr / patch / patch.object on the global module
        # ----------------------------------------------------------------
        if isinstance(node, ast.Call):
            func = node.func
            kind = None
            if isinstance(func, ast.Attribute) and func.attr == "setattr":
                kind = "setattr"
            elif isinstance(func, ast.Name) and func.id == "setattr":
                kind = "setattr"
            elif isinstance(func, ast.Name) and func.id == "patch":
                kind = "patch"
            elif isinstance(func, ast.Attribute) and func.attr == "object":
                kind = "patch.object"

            if kind in ("setattr", "patch.object") and len(node.args) >= 2:
                first, second = node.args[0], node.args[1]
                if _is_name(first, "os", "sys") and _is_const_str(second, "name", "platform"):
                    mod = first.id
                    if second.value in _DANGEROUS[mod]:
                        hits.append(
                            (
                                node.lineno,
                                f"{kind} writes global {mod}.{second.value}",
                            )
                        )

            if kind == "patch" and node.args:
                first = node.args[0]
                if _is_const_str(first, "os.name", "sys.platform"):
                    hits.append((node.lineno, f'patch("{first.value}")'))

    return hits


# ---------------------------------------------------------------------------
# Self-tests: prove the detector is not vacuous
# ---------------------------------------------------------------------------

_BAD_DIRECT = "import os\ndef t():\n    os.name = 'posix'\n"
_BAD_SETATTR = (
    "import os\ndef t(monkeypatch):\n"
    "    monkeypatch.setattr(os, 'name', 'posix')\n"
)
_BAD_PATCH_ATTR = (
    "import os\ndef t(mockpatch):\n"
    "    mockpatch.object(os, 'name', 'posix')\n"
)
_BAD_PATCH_STR = "def t(patch):\n    patch('os.name', 'posix')\n"
_BAD_SYS = "import sys\ndef t():\n    sys.platform = 'win32'\n"

_GOOD = (
    "import os, types\n"
    "def t(monkeypatch):\n"
    "    # sanctioned module-scoped form\n"
    "    monkeypatch.setattr(some_mod, 'os', types.SimpleNamespace(name='posix', environ=os.environ))\n"
    "    assert os.name == 'posix'          # read / compare -> fine\n"
    "    os.environ['HERMES_HOME'] = 'x'   # env dict -> fine\n"
    "    monkeypatch.setattr(Path, 'home', lambda: '/tmp')  # other global class -> fine here\n"
)


def test_detector_flags_direct_os_name_assignment():
    tree = ast.parse(_BAD_DIRECT)
    assert detect_global_platform_writes(tree), "expected a hit on global os.name="


def test_detector_flags_monkeypatch_setattr_os():
    tree = ast.parse(_BAD_SETATTR)
    assert detect_global_platform_writes(tree), "expected a hit on monkeypatch.setattr(os, 'name')"


def test_detector_flags_patch_object_os():
    tree = ast.parse(_BAD_PATCH_ATTR)
    assert detect_global_platform_writes(tree), "expected a hit on patch.object(os, 'name')"


def test_detector_flags_patch_string_os_name():
    tree = ast.parse(_BAD_PATCH_STR)
    assert detect_global_platform_writes(tree), "expected a hit on patch('os.name')"


def test_detector_flags_sys_platform():
    tree = ast.parse(_BAD_SYS)
    assert detect_global_platform_writes(tree), "expected a hit on global sys.platform="


def test_detector_ignores_sanctioned_and_benign_forms():
    tree = ast.parse(_GOOD)
    assert detect_global_platform_writes(tree) == [], (
        "module-scoped patch / reads / os.environ must NOT be flagged"
    )


# ---------------------------------------------------------------------------
# The real guard: the test suite itself must contain zero global platform writes
# ---------------------------------------------------------------------------

def test_suite_has_no_global_platform_writes():
    tests_dir = Path(__file__).resolve().parent
    violations: List[str] = []
    for path in sorted(tests_dir.glob("*.py")):
        # Skip this guard module — its self-test strings are not parsed, only
        # the live detector is; and it documents the bad pattern by name.
        if path.name == Path(__file__).name:
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:  # pragma: no cover - defensive
            violations.append(f"{path.name}:<0>: SyntaxError: {exc}")
            continue
        for lineno, desc in detect_global_platform_writes(tree):
            violations.append(f"{path.name}:<{lineno}>: {desc}")

    assert not violations, (
        "Global os.name / sys.platform writes detected in the suite "
        "(they flip pathlib.Path dispatch and kill foreign-OS CI runners):\n"
        + "\n".join(violations)
    )
