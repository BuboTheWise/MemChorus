"""Real agent end-to-end tests (issue #197).

These tests run ONE actual ``hermes chat`` subprocess against the natural
language prompt from :data:`tests.natural_language_prompts.NATURAL_TEST_PROMPT`,
using the **installed** MemChorus release and the live local LLM. They are the
acceptance proof that the full agent loop works on a natural prompt — planning,
making *real* tool calls against a seeded fixture tree, persisting findings,
recalling prior analysis, and producing a synthesis.

Why one shared run
------------------
The local 27B model is chain-of-thought heavy: a realistic full four-step run
needs roughly 8-15 minutes. Five independent runs would multiply that, so this
module runs the agent ONCE (module-scoped), persists the transcript and captures
the memory-dir state, then all step assertions are evaluated against that single
artifact. This is also the honest shape of the acceptance test: "run the loop,
then inspect what it did."

Gating (skip, not fail)
------------------------
These live tests are skipped (not failed) when their infrastructure is absent in
a given environment: no ``hermes`` CLI on PATH, or no ``memchorus`` importable in
the runtime venv. That keeps the deterministic unit suites green in offline CI
while the live path still gates the running stack where it is available.

Scope (issue #197): "Test real agent (not just the hooks)." This is a separate
live path from the deterministic suites in ``tests/test_natural_integration.py``
and ``tests/test_memchorus_benchmark.py`` — it proves the agent loop itself.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import textwrap
import time
from typing import Any, Dict

import pytest

try:
    from .natural_language_prompts import NATURAL_TEST_PROMPT
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from natural_language_prompts import NATURAL_TEST_PROMPT

# Serialize this module onto a single xdist worker — the live agent run is a
# single shared subprocess (like the synthetic MCP suite) and cannot run in
# parallel with itself (races on the fixture tree + one agent per run).
pytestmark = pytest.mark.xdist_group("mc_live_e2e")

# The natural prompt names this literal fixture path.
_FIXTURE_ROOT = pathlib.Path("/tmp/mc_test_tree")

_FIXTURE_MODULE_A = textwrap.dedent(
    '''
    """adaptive_threshold.py -- small module under coverage (test fixture)."""

    from typing import List


    def compute(values: List[float], floor: float = 0.25) -> float:
        """Return the largest value that clears the floor, else zero."""
        if not values:
            return 0.0
        big = max(values)
        if big < floor:
            return 0.0
        return big
    '''
).lstrip()

_FIXTURE_MODULE_B = textwrap.dedent(
    '''
    """behavioral_trigger.py -- small module under coverage (test fixture)."""

    TRIGGERS = {"plan", "save", "recall"}


    def fire(name: str) -> bool:
        if name in TRIGGERS:
            return True
        raise ValueError(f"unknown trigger: {name}")
    '''
).lstrip()

_FIXTURE_MODULE_C = textwrap.dedent(
    '''
    """lifecycle_eviction.py -- small module under coverage (test fixture)."""

    class Evictor:
        """Small FIFO evictor with a partially-exercised eviction branch."""

        def __init__(self, cap: int = 3) -> None:
            self.cap = cap
            self.store: list = []

        def add(self, item: str) -> None:
            self.store.append(item)
            if len(self.store) > self.cap:
                self.store.pop(0)
    '''
).lstrip()

_FIXTURE_TEST_FILE = textwrap.dedent(
    '''
    """Minimal tests for the fixture tree (enough for a coverage run)."""

    import memchorus as m
    from memchorus import adaptive_threshold as at
    from memchorus import behavioral_trigger as bt
    from memchorus import lifecycle_eviction as le


    def test_canary() -> None:
        assert m.__version__


    def test_threshold() -> None:
        assert at.compute([3, 1, 2]) == 3
        assert at.compute([], floor=0.5) == 0


    def test_trigger() -> None:
        assert bt.TRIGGERS == {"plan", "save", "recall"}
        for name in bt.TRIGGERS:
            assert bt.fire(name)


    def test_evictor() -> None:
        ev = le.Evictor(cap=2)
        ev.add("a")
        ev.add("b")
        ev.add("c")
        assert ev.store == ["b", "c"]
    '''
).lstrip()


# --- Infrastructure availability (skip, not fail) ------------------------------------


def _hermes_available() -> bool:
    if not shutil.which("hermes"):
        return False
    try:
        cp = subprocess.run(
            ["hermes", "--version"], capture_output=True, text=True, timeout=30,
        )
        return cp.returncode == 0
    except Exception:
        return False


_HAS_HERMES = _hermes_available()

# The local Ollama endpoint the default profile talks to. Present on this host.
_LIVE_ENDPOINT = "http://127.0.0.1:11434/v1"


def _live_llm_reachable() -> bool:
    """Cheap probe: does the local LLM endpoint respond on HTTP at all?

    Not a full chat completion — just a TCP+HTTP reachability check — so we
    skip fast (rather than hang) when the local server is down in a given env.
    """
    import urllib.request
    try:
        req = urllib.request.Request(_LIVE_ENDPOINT + "/models", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 401, 403)  # 200/4xx = server is up
    except Exception:
        # Ollama may return 501 on /models for some versions; fall back to root.
        pass
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434", timeout=5) as resp:
            return True
    except Exception:
        return False


_HAS_LIVE_LLM = _live_llm_reachable()

_SKIP_REASON = "live e2e skipped: " + (
    "no hermes CLI" if not _HAS_HERMES else ""
) + (" " if not _HAS_HERMES else "") + (
    "no local LLM endpoint" if not _HAS_LIVE_LLM else ""
)


# --- The single shared run -------------------------------------------------------------


class _RunResult:
    """Captures one live agent run (transcript + memory-dir snapshot)."""

    def __init__(self, home: pathlib.Path, outpath: pathlib.Path) -> None:
        self.home = home
        self.outpath = outpath
        self.body: str = ""
        self.returncode: int = -1
        self.dur_s: float = 0.0

    def load(self) -> None:
        self.body = self.outpath.read_text(encoding="utf-8", errors="ignore")

    def memory_entries(self, exclude_seeded: bool = True) -> list[pathlib.Path]:
        memdir = self.home / "memories"
        if not memdir.is_dir():
            return []
        seeded = "prior-coverage-baseline-adaptive-threshold"
        out = []
        for p in memdir.iterdir():
            if p.is_file() and (not exclude_seeded or seeded not in p.name):
                out.append(p)
        return out


@pytest.fixture(scope="module")
def live_agent_run(tmp_path_factory: pytest.TempPathFactory) -> _RunResult:
    """Run the natural prompt ONCE against an isolated HERMES_HOME.

    Uses a tmp_path_factory (module scope) so the isolated home and the output
    artifacts live under the test's tmp tree (cleaned up by pytest).
    """
    if not (_HAS_HERMES and _HAS_LIVE_LLM):
        pytest.skip(_SKIP_REASON)

    base = tmp_path_factory.mktemp("mc_live_e2e")
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        textwrap.dedent(
            """
            model:
              name: orcarouter/Qwen3.8-27B-Uncensored:q4_K_S
              provider: ollama
              base_url: http://127.0.0.1:11434/v1
            """
        ).lstrip(),
        encoding="utf-8",
    )

    # Seed the prior-analysis entry the agent is meant to recall (step 4) in the
    # hermes_default memory-dir layout, so session_search + the disk oracle can
    # find it.
    memdir = home / "memories"
    memdir.mkdir(parents=True, exist_ok=True)
    seeded_file = memdir / "prior-coverage-baseline-adaptive-threshold.json"
    seeded_file.write_text(
        '{"key": "prior-coverage-baseline-adaptive-threshold", '
        '"value": {"module": "adaptive_threshold", '
        '"module2": "behavioral_trigger", "module3": "lifecycle_eviction", '
        '"finding": "baseline: adaptive_threshold 62% coverage; '
        'behavioral_trigger missing fire() branch; lifecycle_eviction '
        'eviction path untested; 3 lines missing on compute high-branch.", '
        '"recorded": "2026-09-01 prior session"}}',
        encoding="utf-8",
    )

    # Build the deterministic fixture tree at the literal path the prompt names.
    root = _FIXTURE_ROOT
    if root.exists():
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass  # another worker may have cleaned it between check and rmtree
    pkg = root / "src" / "memchorus"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        '"""Mini memchorus fixture for live-e2e coverage tests."""\n'
        "__version__ = '0.0.e2e'\n", encoding="utf-8")
    (pkg / "adaptive_threshold.py").write_text(_FIXTURE_MODULE_A, encoding="utf-8")
    (pkg / "behavioral_trigger.py").write_text(_FIXTURE_MODULE_B, encoding="utf-8")
    (pkg / "lifecycle_eviction.py").write_text(_FIXTURE_MODULE_C, encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_fixture_modules.py").write_text(_FIXTURE_TEST_FILE, encoding="utf-8")

    outpath = base / "transcript.txt"

    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["MEMCHORUS_AUTO_ENABLED"] = "true"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    # Generous budget: a full four-step run on the local 27B model takes ~8-15 min.
    t0 = time.monotonic()
    try:
        cp = subprocess.run(
            [
                "hermes", "chat",
                "-Q",
                "--yolo",
                "--accept-hooks",
                "--ignore-rules",
                "--max-turns", "60",
                "-q", NATURAL_TEST_PROMPT,
            ],
            capture_output=True, text=True, timeout=1500,
            env=env, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        dur = time.monotonic() - t0
        cp = subprocess.CompletedProcess(args=[], returncode=-1, stdout="",
                                          stderr=f"hermes chat exceeded 1500s budget")
    else:
        dur = time.monotonic() - t0

    result = _RunResult(home=home, outpath=outpath)
    result.returncode = cp.returncode
    result.dur_s = dur
    result.outpath.write_text(
        cp.stdout + "\n==STDERR==\n" + cp.stderr
        + f"\n==META==\nreturncode={cp.returncode} duration_s={dur:.1f}",
        encoding="utf-8",
    )
    result.load()
    return result


# --- The four step assertions (all against the single shared run) --------------------


@pytest.mark.skipif(not (_HAS_HERMES and _HAS_LIVE_LLM), reason=_SKIP_REASON)
class TestNaturalPromptLiveAgent:
    """Acceptance proof: the real agent loop works on a natural prompt (#197)."""

    def test_step1_plan_names_the_modules(self, live_agent_run: _RunResult) -> None:
        """The agent produces a plan that references the modules under test."""
        text = live_agent_run.body
        assert len(text) > 300, (
            f"transcript too short/empty — agent did not plan "
            f"(returncode={live_agent_run.returncode}); "
            f"capture: {live_agent_run.outpath}"
        )
        for mod in ("adaptive_threshold", "behavioral_trigger", "lifecycle_eviction"):
            assert mod in text, (
                f"transcript does not reference module {mod!r} — step 1 "
                f"not evidenced (capture: {live_agent_run.outpath})"
            )

    def test_step2_real_tool_calls(self, live_agent_run: _RunResult) -> None:
        """The agent ran real tool calls (coverage / file reads / imports)."""
        body = live_agent_run.body
        tools: list[str] = []
        if "pytest" in body and (
            "--cov" in body or "coverage" in body or "cov-report" in body
        ):
            tools.append("pytest+coverage")
        if body.count("read_file") >= 2 or body.count("open(") >= 2:
            tools.append("file-reads")
        if any(
            f"import {m}" in body
            for m in ("adaptive_threshold", "behavioral_trigger", "lifecycle_eviction")
        ):
            tools.append("module-import")
        assert tools, (
            "no real tool-call evidence — step 2 not evidenced "
            f"(capture: {live_agent_run.outpath})"
        )

    def test_step3_findings_persisted(self, live_agent_run: _RunResult) -> None:
        """The agent saved concrete findings (new memory entry, or save signal)."""
        # Disk oracle: a NEW memory entry (excluding the seeded prior).
        new = live_agent_run.memory_entries(exclude_seeded=True)
        if new:
            # Sanity: the fresh entry is non-empty and not the seeded baseline.
            assert any(p.stat().st_size > 0 for p in new), (
                f"new memory entries exist but are empty: {new}"
            )
            return
        # Fallback: the transcript shows an explicit save action.
        body = live_agent_run.body.lower()
        save_signal = any(
            n in body for n in ("memory", "add_drawer", "add-drawer", "capture")
        )
        assert save_signal, (
            f"step 3 not evidenced: no new memory entry {new} AND no save "
            f"signal in transcript (capture: {live_agent_run.outpath})"
        )

    def test_step4_prior_analysis_recalled(self, live_agent_run: _RunResult) -> None:
        """The agent recalled prior analysis (session_search or a compare)."""
        body = live_agent_run.body.lower()
        recalled = (
            "session_search" in body
            or ("baseline" in body and "compare" in body)
            or ("prior" in body and "analysis" in body)
        )
        assert recalled, (
            f"step 4 not evidenced: no recall/session_search in transcript "
            f"(capture: {live_agent_run.outpath})"
        )

    def test_final_synthesis_labelled(self, live_agent_run: _RunResult) -> None:
        """The agent produced a labelled synthesis section (non-boilerplate)."""
        import re
        body = live_agent_run.body
        header_re = re.compile(
            r"^(#{1,6}\s+.+|synthesis|summary synthesis|summary|findings)\s*[:\-]?",
            flags=re.MULTILINE | re.IGNORECASE,
        )
        headers = header_re.findall(body)
        assert headers, (
            f"no labelled synthesis section found — final step not evidenced "
            f"(capture: {live_agent_run.outpath})"
        )

    def test_seed_not_clobbered(self, live_agent_run: _RunResult) -> None:
        """Regression guard: the seeded prior entry still exists after the run."""
        seeded = live_agent_run.home / "memories" / \
            "prior-coverage-baseline-adaptive-threshold.json"
        assert seeded.is_file(), (
            f"seeded prior-analysis entry missing after run — the recall "
            f"target was destroyed (capture: {live_agent_run.outpath})"
        )
