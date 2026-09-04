"""Legacy compatibility shim.

The authoritative packaging metadata (name, version, dependencies, extras,
entry points, package discovery) now lives exclusively in ``pyproject.toml``
at the repository root. This module exists only so that legacy
``python setup.py`` invocations and older tooling continue to work; it
deliberately declares *no* packaging fields of its own so it cannot diverge
from the canonical ``[project]`` table.

Version is a single source of truth: it is derived from
``src/memchorus/__init__.py`` at build time (see the
``[tool.setuptools.dynamic]`` table in ``pyproject.toml``).
"""

from setuptools import setup

setup()
