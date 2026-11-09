#!/usr/bin/env python3
"""GH-272 — Guards as real ``pre_tool_call`` gates: host-verified e2e test.

Issue #272 acceptance criteria, verified against the Hermes host source
(hermes-agent, model_tools.py + hermes_cli/plugins.py):

1. A behavioral prohibition that matches a tool call MUST block that tool
   call **before execution** — the tool handler must never run.  This is
   the ``pre_tool_call`` directive path, not the prompt-injection path
   (``on_pre_llm_call`` / ``inject_blocks``), which is fallback-only
   since #264.  A severity-3 (BLOCK) guard maps to the directive
   ``{"action": "block", "message": <reason>}``.

2. The block must be proven **host-verified, not just mocked**: these tests
   run the *real* Hermes dispatch chain —
     ``handle_function_call`` (model_tools.py, the tool-dispatch entry
     point every execution path funnels through)
       → ``resolve_pre_tool_block`` (hermes_cli/plugins.py)
       → ``_get_pre_tool_call_directive_details`` → ``invoke_hook("pre_tool_call")``
   with the *real* ``PluginManager`` hook registry and the *real*
   ``memchorus.prohibitions.ProhibitionsManager`` (seeded rule, compiled
   regex, verdict mapping), and the *real* ``MemChorusHooks.on_pre_tool_call``
   registered as the hook callback.  A sentinel tool handler registered on
   the real tool registry proves NON-execution: when the gate fires the
   handler is never called and the tool result is ``{"error": <veto>}``,
   not the handler's output.

   What is faked: the orchestrator singleton (``_get_orchestrator``) —
   MemChorus-internal plumbing that in production is built from real
   memories by ``MemoryOrchestrator``.  The gate contract under test
   (hook registration → directive translation → block → no execution) is
   fully host-side and exercised for real.

Host block-action contract (documented in docs/guard-block-contract.md):

  - Hook returns ``{"action": "block", "message": M}`` —
    ``_get_pre_tool_call_directive_details`` requires ``action`` to be
    exactly "block" or "approve" and a non-empty ``message`` for block
    (a block without a message is silently ignored — the tool proceeds);
  - ``resolve_pre_tool_block`` returns ``M``;
  - ``handle_function_call`` short-circuits BEFORE handler dispatch: the
    tool handler is NOT called, the tool result the model sees is
    ``json.dumps({"error": M})``, and the ``post_tool_call`` observers
    fire with ``status="blocked", error_type="plugin_block"``;
  - a ``"modify"`` directive is translated the same way (args replaced);
  - an ``"approve"`` directive routes through the human approval gate
    (``tools.approval.request_tool_approval``) and is fail-closed (gate
    error/deny/timeout → block with a message).

The MemChorus verdict → directive mapping (hooks.py ``on_pre_tool_call``):

  - severity-3 (BLOCK)    -> ``{"action": "block", "message": ..., "reason": ...}``
  - severity-2 (WARNING)  -> ``{"action": "approve", ...}`` (human approval gate)
  - severity-1 / no match -> ``None`` (observer-only; tool proceeds)
  - guard-internal error  -> ``None`` (observer semantics: never veto on a guard bug)
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# Ensure src/ is importable regardless of pytest runner cwd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Host package — the hermes-agent checkout is the host under test.
_HERMES_HOME = os.path.expanduser("~/.hermes/hermes-agent")
if os.path.isdir(_HERMES_HOME) and _HERMES_HOME not in sys.path:
    sys.path.insert(0, _HERMES_HOME)

try:
    import hermes_cli.plugins as hermes_plugins  # noqa: F401
except ImportError:  # pragma: no cover
    hermes_plugins = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A severity-3 tool_call_check rule in the canonical seed-rule shape
# (cf. guard-001 in prohibitions.py).
SENTINEL_RULE = {
    "id": "guard-272-no-protected-push",
    "condition": "Never push directly to a protected branch via gh push.",
    "trigger_keywords": ["protected branch push"],
    "tool_call_check": r"gh\s+push\s+origin\s+protected/",
    "severity": 3,
    "block_action": "pushing directly to the protected branch",
    "rationale": "GH-272 e2e: protected-branch pushes must go through review.",
    "source": "system",
    "created": "2026-11-09T00:00:00Z",
    "type": "infrastructure",
}


def _make_orchestrator(pm) -> mock.MagicMock:
    """Minimal orchestrator stand-in: config + prohibitions manager only."""
    orch = mock.MagicMock()
    orch.config = {}  # prohibitions.enabled defaults to True
    orch._prohibitions_manager = pm
    orch.search.return_value = []
    return orch


def _seeded_manager(tmpdir: str):
    """Real ProhibitionsManager seeded with the sentinel rule in tmpdir."""
    from pathlib import Path
    from memchorus.prohibitions import ProhibitionsManager
    pm = ProhibitionsManager(data_dir=Path(tmpdir))
    pm.load()  # seeds the shipped defaults (incl. guard-001 pip-install-hermes)
    return pm


class _GateHarness:
    """Wire a hook onto the REAL host chain the way production does.

    Production path: ``model_tools.handle_function_call`` →
    ``hermes_cli.plugins.resolve_pre_tool_block`` → ``invoke_hook`` →
    ``get_plugin_manager().invoke_hook``.  We point the module-level
    ``get_plugin_manager`` at a fresh ``PluginManager`` that has the hook
    under test registered, and save/restore the real tool registry.
    """

    def __init__(self):
        from model_tools import registry
        self.registry = registry
        self._tools = dict(registry._tools)
        from hermes_cli.plugins import PluginManager
        self.manager = PluginManager()
        self._patch = mock.patch(
            "hermes_cli.plugins.get_plugin_manager",
            return_value=self.manager,
        )
        self._patch.start()

    def register_hook(self, callback):
        # PluginManager stores hooks in self._hooks[hook_name] = [callbacks].
        # In production the plugin handle's register_hook() appends here
        # (plugins.py:1158-1170). We mirror that same storage path.
        self.manager._hooks.setdefault("pre_tool_call", []).append(callback)

    def register_tool(self, name, sink):
        # tools/registry.dispatch() calls ``entry.handler(args, **kwargs)`` —
        # the args dict is passed POSITIONALLY as the first arg (see
        # tools/registry.py:dispatch), so the sentinel must accept it.
        def _tool(args=None, **kwargs):
            sink.append(args if args is not None else kwargs)
            return json.dumps({"executed": True})

        self.registry.register(
            name=name,
            toolset="gh272",
            schema={"type": "object", "properties": {}},
            handler=_tool,
            override=True,
        )

    def cleanup(self):
        self._patch.stop()
        self.registry._tools.clear()
        self.registry._tools.update(self._tools)


# ---------------------------------------------------------------------------
# AC2 — host contract: block directive translation (no MemChorus involved)
# ---------------------------------------------------------------------------


@unittest.skipIf(hermes_plugins is None, "hermes-agent not available in test env")
class TestHostBlockDirectiveContract(unittest.TestCase):
    """The Hermes host must translate a pre_tool_call block into NO execution.

    This is the contract side of GH-272: it pins down what a
    ``{"action": "block", "message": M}`` directive means to the host,
    independently of MemChorus.  Uses the real ``PluginManager``,
    real ``invoke_hook``, and the real ``handle_function_call`` dispatch.
    """

    def setUp(self):
        self.h = _GateHarness()
        self._tmp = tempfile.mkdtemp(prefix="gh272_")

    def tearDown(self):
        self.h.cleanup()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_block_directive_short_circuits_execution(self):
        """AC2: a registered block directive → handler NOT called, veto result."""
        from model_tools import handle_function_call

        calls = []
        self.h.register_tool("protected_push", calls)
        self.h.register_hook(
            lambda **kwargs: {
                "action": "block",
                "message": "VETO: protected-branch push",
                "reason": "VETO: protected-branch push",
            }
        )

        result = handle_function_call("protected_push", {"target": "protected/main"})

        # The tool handler must NEVER have run.
        self.assertEqual(calls, [], "blocked tool handler must not execute")
        # And the result must be the veto message, not the tool output.
        payload = json.loads(result)
        self.assertEqual(payload["error"], "VETO: protected-branch push")

    def test_no_directive_allows_execution(self):
        """With no pre_tool_call hook directive, the tool proceeds (control)."""
        from model_tools import handle_function_call

        calls = []
        self.h.register_tool("plain_tool", calls)

        result = handle_function_call("plain_tool", {"x": 1})
        self.assertEqual(len(calls), 1, "unblocked tool must execute")
        self.assertEqual(json.loads(result), {"executed": True})

    def test_block_without_message_is_ignored(self):
        """Host contract: a block directive with no message is dropped.

        ``_get_pre_tool_call_directive_details`` only honors a block when
        ``message`` is a non-empty string — a messageless block must let
        the tool proceed.  This is why the MemChorus hook ALWAYS pairs
        ``action`` with ``message`` (and the ``reason`` alias).
        """
        from model_tools import handle_function_call

        calls = []
        self.h.register_tool("quiet_block", calls)
        self.h.register_hook(lambda **kwargs: {"action": "block"})

        result = handle_function_call("quiet_block", {})
        self.assertEqual(len(calls), 1, "messageless block must not veto")
        self.assertEqual(json.loads(result), {"executed": True})


# ---------------------------------------------------------------------------
# AC1 — MemChorus gate: real ProhibitionsManager + real host dispatch
# ---------------------------------------------------------------------------


@unittest.skipIf(hermes_plugins is None, "hermes-agent not available in test env")
class TestMemChorusPreToolCallGate(unittest.TestCase):
    """GH-272 AC1: a severity-3 prohibition MUST block the tool call.

    End-to-end through the real host dispatch (``handle_function_call``
    → ``resolve_pre_tool_block`` → ``invoke_hook``) with the *real*
    MemChorus ``on_pre_tool_call`` hook registered on the real plugin
    registry and the *real* ``ProhibitionsManager`` (seeded rule,
    compiled regex, verdict mapping).  The only fake is the orchestrator
    singleton — the gate contract itself is host-side.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="gh272_")
        self.h = _GateHarness()

    def tearDown(self):
        self.h.cleanup()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_severity3_rule_blocks_real_dispatch(self):
        """AC1: a matching severity-3 prohibition → handler never runs."""
        from model_tools import handle_function_call
        from memchorus.hooks import MemChorusHooks
        from memchorus.prohibitions import Prohibition

        pm = _seeded_manager(self._tmp)
        pm.add_rule(Prohibition.from_dict(SENTINEL_RULE))
        orch = _make_orchestrator(pm)
        hooks = MemChorusHooks()

        calls = []
        self.h.register_tool("gh", calls)
        self.h.register_hook(hooks.on_pre_tool_call)

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=orch):
            result = handle_function_call(
                "gh", {"command": "gh push origin protected/main --force"}
            )

        # The handler must NOT have run — the gate fired before execution.
        self.assertEqual(calls, [], "severity-3 rule must block execution (AC1)")
        # And the result must carry the veto message (contract).
        payload = json.loads(result)
        self.assertIn("error", payload)
        self.assertIn("guard-272-no-protected-push", payload["error"])

    def test_severity3_blocks_on_seeded_default_rule(self):
        """AC1 also holds for the shipped seed rule (guard-001, pip install -e)."""
        from model_tools import handle_function_call
        from memchorus.hooks import MemChorusHooks

        pm = _seeded_manager(self._tmp)  # guard-001 is seeded by load()
        orch = _make_orchestrator(pm)
        hooks = MemChorusHooks()

        calls = []
        self.h.register_tool("terminal", calls)
        self.h.register_hook(hooks.on_pre_tool_call)

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=orch):
            result = handle_function_call(
                "terminal", {"command": "pip install -e ~/.hermes/hermes-agent"}
            )

        self.assertEqual(calls, [], "seed rule guard-001 must block execution")
        payload = json.loads(result)
        self.assertIn("guard-001-no-editable-install", payload["error"])

    def test_no_match_allows_execution(self):
        """A non-matching tool call proceeds through the same path (control)."""
        from model_tools import handle_function_call
        from memchorus.hooks import MemChorusHooks

        pm = _seeded_manager(self._tmp)
        pm.add_rule(__import__("memchorus.prohibitions", fromlist=["Prohibition"]).Prohibition.from_dict(SENTINEL_RULE))
        orch = _make_orchestrator(pm)
        hooks = MemChorusHooks()

        calls = []
        self.h.register_tool("gh", calls)
        self.h.register_hook(hooks.on_pre_tool_call)

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=orch):
            result = handle_function_call("gh", {"command": "gh issue list"})

        self.assertEqual(len(calls), 1, "non-matching tool must execute")
        self.assertEqual(json.loads(result)["executed"], True)

    def test_opt_out_toggle_disables_gate(self):
        """prohibitions.enabled=False → the gate is off; the tool proceeds."""
        from model_tools import handle_function_call
        from memchorus.hooks import MemChorusHooks

        pm = _seeded_manager(self._tmp)
        orch = _make_orchestrator(pm)
        orch.config = {"prohibitions": {"enabled": False}}
        hooks = MemChorusHooks()

        calls = []
        self.h.register_tool("terminal", calls)
        self.h.register_hook(hooks.on_pre_tool_call)

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=orch):
            result = handle_function_call(
                "terminal", {"command": "pip install -e ~/.hermes/hermes-agent"}
            )

        self.assertEqual(len(calls), 1, "opt-out must disable the gate")

    def test_severity2_escalates_to_approve(self):
        """A severity-2 match routes to the human approval gate (warning)."""
        from memchorus.hooks import MemChorusHooks
        from memchorus.prohibitions import Prohibition

        pm = _seeded_manager(self._tmp)
        pm.add_rule(Prohibition.from_dict({
            "id": "guard-272-warn",
            "condition": "Warn when the agent recursively deletes the workspace.",
            "trigger_keywords": ["rm -rf workspace"],
            "tool_call_check": r"rm\s+-rf\s+.*workspace",
            "severity": 2,
            "block_action": "recursive delete of the workspace directory",
            "rationale": "GH-272: severity-2 escalates to the approval gate.",
            "source": "system",
            "created": "2026-11-09T00:00:00Z",
            "type": "infrastructure",
        }))
        orch = _make_orchestrator(pm)
        hooks = MemChorusHooks()

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=orch):
            directive = hooks.on_pre_tool_call(
                tool_name="terminal", args={"command": "rm -rf ~/workspace"}
            )

        self.assertIsInstance(directive, dict)
        self.assertEqual(directive["action"], "approve")
        self.assertIn("guard-272-warn", directive["message"])

    def test_guard_failure_degrades_to_allow(self):
        """A guard-internal error must NOT block execution (observer semantics)."""
        from memchorus.hooks import MemChorusHooks

        pm = mock.MagicMock()
        pm.scan_tool_call.side_effect = RuntimeError("guard boom")
        orch = _make_orchestrator(pm)
        hooks = MemChorusHooks()

        with mock.patch("memchorus.hooks._get_orchestrator", return_value=orch):
            directive = hooks.on_pre_tool_call(
                tool_name="terminal", args={"command": "anything"}
            )

        # Observer semantics: on internal guard error the tool must proceed.
        self.assertIsNone(directive)


if __name__ == "__main__":
    unittest.main(verbosity=2)
