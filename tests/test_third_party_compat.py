#!/usr/bin/env python3
"""
test_third_party_compat.py - Third-party compatibility tests for MemChorus v1.2.

Covers:
- Import memchorus in subprocess with pyyaml absent -> graceful degradation
- Optional features fail silently when submodule missing
- No hard dependencies on non-standard packages block import
"""

import os
import subprocess
import sys
import tempfile
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src'))


def _run(script_lines):
    """Write script to temp file, run in subprocess, return CompletedProcess."""
    env = os.environ.copy()
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)
    tmp.write('\n'.join(script_lines))
    tmp.flush()
    try:
        return subprocess.run(
            [sys.executable, tmp.name],
            capture_output=True, text=True, timeout=30, env=env,
        )
    finally:
        os.unlink(tmp.name)


def _mk_script(*lines_before):
    """Return a list of script lines with path setup already prepended."""
    L = [
        'import sys',
        f'sys.path.insert(0, {SRC_DIR!r})',
    ]
    L.extend(lines_before)
    return L


def _yaml_blocker_lines():
    """Return script lines that clear cached yaml modules and install a Python 3.4+ blocker.

    Includes 'import sys' at the top so it can be concatenated directly as a script.
    """
    return [
        '# import sys is already included by caller',
        'import importlib.util',
        # Clear any pre-cached yaml-related modules
        'for mod in list(sys.modules):',
        '    if "yaml" in mod.lower(): del sys.modules[mod]',
        # Modern meta_path finder (Python 3.4+) that catches all yaml.* submodules
        'class YamlBlocker:',
        '    def find_spec(self, name, path=None, target=None):',
        '        import importlib.util',
        '        if name == "yaml" or name.startswith("yaml."):',
        '            return importlib.util.spec_from_loader(name, loader=None)',
        '        return None',
        'sys.meta_path.insert(0, YamlBlocker())',
    ]


class TestYamlMissingGraceful(unittest.TestCase):
    """memchorus imports OK even when pyyaml is absent."""

    def _block_yaml_script(self, postfix_lines):
        """Return subprocess lines that clear cached yaml modules, install a meta_path blocker, then execute POSTFIX_LINES.

        Uses _mk_script for proper sys.path + import sys setup, then appends the
        YAML blocker (which does NOT repeat 'import sys'), then the caller's
        postfix lines.
        """
        L = _mk_script()
        L.extend(_yaml_blocker_lines())
        L.extend(postfix_lines)
        return L

    def test_import_without_yaml(self):
        s = self._block_yaml_script([
            'import memchorus.auto_bootstrap as ab',
            'print("HAS_YAML=" + str(ab._HAS_YAML))',
        ])
        r = _run(s)
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        self.assertIn("HAS_YAML=False", r.stdout.strip())

    def test_bootstrap_when_yaml_missing(self):
        s = self._block_yaml_script([
            'import os; os.environ["MEMCHORUS_AUTO_ENABLED"] = "false"',
            'from memchorus.auto_bootstrap import _bootstrap',
            'print(_bootstrap() is None)',
        ])
        r = _run(s)
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")


class TestFeatureSilentDegradation(unittest.TestCase):

    def test_mcp_probe_failure_non_fatal(self):
        """Block mempalace MCP subprocesses so that the probe fails gracefully.

        We can't block imports of `mempalace_memory_source` or `orchestrator`
        because __init__.py imports them eagerly at module load time.
        Instead we block the actual MCP subprocess calls inside the probe by
        replacing subprocess.run so it always raises, then verify bootstrap
        completes without leaking an exception.
        """
        s = _mk_script(
            'import os; os.environ["MEMCHORUS_AUTO_ENABLED"] = "true"',
            # Clear any pre-loaded memchorus modules so we get a fresh import
            'for mod in list(sys.modules):',
            '    if "memchorus" in mod: del sys.modules[mod]',
            # Patch subprocess.run to simulate MCP server being down
            'import subprocess as _sp',
            '_orig_run = _sp.run',
            'def _bad_run(*a, **kw): raise ConnectionError("MCP unreachable")',
            '_sp.run = _bad_run',
            'from memchorus.auto_bootstrap import _bootstrap',
            'x = _bootstrap()',
            '_sp.run = _orig_run',
            # Bootstrap should return something or None without raising
            'print(type(x).__name__)',
        )
        r = _run(s)
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")

    def test_yaml_loader_missing_returns_empty(self):
        s = _mk_script(
            *_yaml_blocker_lines(),
            'from memchorus.auto_bootstrap import _load_yaml_config, _HAS_YAML',
            'v = _load_yaml_config()',
            'print(f"{_HAS_YAML}|{type(v).__name__}")',
        )
        r = _run(s)
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        parts = r.stdout.strip().split('|')
        self.assertEqual(parts[0], 'False')
        self.assertEqual(parts[1], 'dict')


class TestImportChainSanity(unittest.TestCase):

    def test_import_auto_bootstrap(self):
        s = _mk_script(
            'from memchorus.auto_bootstrap import _bootstrap, _DEFAULTS',
            "print('OK')",
        )
        r = _run(s)
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")

    def test_import_orientation(self):
        s = _mk_script(
            'from memchorus.orientation import orientation_search, _CacheRegistry',
            "print('OK')",
        )
        r = _run(s)
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")

    def test_defaults_dict_keys(self):
        s = _mk_script(
            'from memchorus.auto_bootstrap import _DEFAULTS',
            "assert 'auto_enabled' in _DEFAULTS",
            "assert 'default_source' in _DEFAULTS",
            "assert 'half_life_days' in _DEFAULTS",
            "assert 'cache_ttl_seconds' in _DEFAULTS",
            "print('OK')",
        )
        r = _run(s)
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
