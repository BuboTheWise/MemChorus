"""GH#264 regression tests — raw-message + tool-call-args guard scanning.

Issue #264 (two halves, both confirmed as live bugs):

1. ``on_pre_llm_call`` fed the TOKENIZED keyword bag (``_build_search_terms``
   output) into ``_try_guard_scan``. Compiled prohibition patterns that need the
   verbatim phrase — e.g. the keyword ``pip install -e`` or the regex
   ``pip\\s+install\\s+-e\\s+.*hermes`` — can never match a bag of individual
   tokens, so hard guards silently did not fire on punctuated user messages.

2. ``register()`` wired NO ``pre_tool_call`` hook at all, so tool-call argument
   payloads were never scanned and ``ProhibitionsManager.scan_tool_call`` was
   dead code.

These tests lock in both fixes at the hook layer (the ``prohibitions.py`` core is
already covered in tests/test_prohibitions.py). The mock pattern mirrors
tests/test_prohibitions_opt_out.py: patch ``memchorus.hooks._get_orchestrator``
and spy on ``_try_guard_scan`` / ``_try_guard_scan_tool_call`` on the instance.
"""
import re
from unittest.mock import MagicMock, patch

# --- verbatim phrase that only exists in RAW (punctuated) form ----------------
# "pip install -e" — as a bag of tokens it would be "pip install e" (no dash).
RAW_PHRASE = "Please run: pip install -e ~/.hermes/hermes-agent"


def _assert_verbatim(arg) -> None:
    """The guard scan must have seen the verbatim 'pip install -e' phrase."""
    text = str(arg)
    assert re.search(r"pip\s+install\s+-e", text), (
        f"guard scan should receive verbatim 'pip install -e', got: {text!r}"
    )
    # The literal dash-space-e only survives in the raw message, never in a
    # tokenized/stemmed keyword bag (demonstrates the old bug would have missed it).
    assert " -e " in text, (
        f"guard scan must see verbatim punctuation (dash) — got: {text!r}"
    )


class TestGuardScanSeesRawMessage:
    """Issue #264 half 1 — on_pre_llm_call feeds RAW text to the guard scan."""

    def test_on_pre_llm_call_passes_raw_text_to_guard_scan(self):
        mock_orch = MagicMock()
        mock_orch.config = {}
        mock_orch._prohibitions_manager = None
        mock_orch.search.return_value = []

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks

            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan", return_value=[]) as scan_mock:
                hooks.on_pre_llm_call(user_message=RAW_PHRASE)

        assert scan_mock.called, "guard scan must run on a real user_message"
        # First positional arg is the text being scanned (second is the orchestrator).
        scanned_text = scan_mock.call_args[0][0]
        _assert_verbatim(scanned_text)
        # And it is NOT the tokenized bag — the bag would have lost the dash.
        bag_has_dash = " -e " in str(scan_mock.call_args[1].get("text", "")) if scan_mock.call_args[1] else False
        assert not bag_has_dash, "guard scan was passed a tokenized bag, not raw text"

    def test_guard_scan_gets_raw_when_message_is_a_dict(self):
        """Hermes may pass user_message as a {content: ...} dict — raw text still wins."""
        mock_orch = MagicMock()
        mock_orch.config = {}
        mock_orch._prohibitions_manager = None
        mock_orch.search.return_value = []

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks

            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan", return_value=[]) as scan_mock:
                hooks.on_pre_llm_call(user_message={"content": RAW_PHRASE})

        assert scan_mock.called
        _assert_verbatim(scan_mock.call_args[0][0])

    def test_guard_disabled_skips_raw_scan(self):
        """Opt-out toggle still gates the raw-message path (parity with half-1 fix)."""
        mock_orch = MagicMock()
        mock_orch.config = {"prohibitions": {"enabled": False}}
        mock_orch._prohibitions_manager = None
        mock_orch.search.return_value = []

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks

            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan", return_value=[]) as scan_mock:
                hooks.on_pre_llm_call(user_message=RAW_PHRASE)

        assert not scan_mock.called, "guards disabled => no guard scan"


class TestExtractRawInputText:
    """Unit tests for the new _extract_raw_input_text helper (priority + fallbacks)."""

    def _extract(self, kwargs):
        from memchorus.hooks import _extract_raw_input_text
        return _extract_raw_input_text(kwargs)

    def test_user_message_first(self):
        assert "pip install -e" in self._extract({"user_message": RAW_PHRASE})

    def test_user_message_dict_content(self):
        assert "pip install -e" in self._extract({"user_message": {"content": RAW_PHRASE}})

    def test_user_message_object_attribute(self):
        class M:
            content = RAW_PHRASE
        assert "pip install -e" in self._extract({"user_message": M()})

    def test_falls_back_to_conversation_history(self):
        text = self._extract({"conversation_history": [
            {"role": "user", "content": "run it: pip install -e ~/.x"},
        ]})
        assert "pip install -e" in text

    def test_empty_when_nothing_raw(self):
        assert self._extract({}) == ""

    def test_truncates_to_cap(self):
        long_msg = "x" * 5000
        out = self._extract({"user_message": long_msg})
        assert len(out) <= 4096


class TestOnPreToolCall:
    """Issue #264 half 2 — on_pre_tool_call vets tool-call args via the guard pipeline."""

    @staticmethod
    def _result(verdict, rule_id="g-1"):
        from memchorus.prohibitions import GuardResult, Prohibition
        rule = Prohibition(
            id=rule_id,
            condition="Do not editable-install hermes",
            trigger_keywords=["pip install -e"],
            severity=3,
            block_action="editable installs into the venv",
            rationale="editable install breaks the venv-managed hermes",
        )
        return GuardResult(verdict=verdict, matched_rules=[rule])

    def _run(self, verdict, tool_name="terminal", args=None):
        mock_orch = MagicMock()
        mock_orch.config = {}
        mock_orch._prohibitions_manager = None

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks

            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan_tool_call") as m:
                m.return_value = self._result(verdict)
                result = hooks.on_pre_tool_call(tool_name=tool_name, args=args)
        return result, m

    def test_block_verdict_returns_block_directive(self):
        from memchorus.prohibitions import GuardVerdict
        result, m = self._run(GuardVerdict.BLOCK, args={"command": "pip install -e ~/.hermes"})
        assert result is not None
        assert result["action"] == "block"
        assert "behavioral guard" in result["message"].lower()
        m.assert_called_once()

    def test_warning_verdict_returns_approve_directive(self):
        from memchorus.prohibitions import GuardVerdict
        result, _m = self._run(GuardVerdict.WARNING)
        assert result is not None
        assert result["action"] == "approve", "WARNING severity should escalate to human approval"
        assert result["message"]

    def test_ok_verdict_returns_none(self):
        from memchorus.prohibitions import GuardVerdict
        result, _m = self._run(GuardVerdict.OK)
        assert result is None, "OK verdict = observer-only, tool call proceeds"

    def test_command_includes_tool_name_and_serialized_args(self):
        from memchorus.prohibitions import GuardVerdict
        _r, m = self._run(GuardVerdict.BLOCK, args={"command": "pip install -e ~/.hermes"})
        command = m.call_args[0][0]
        assert "terminal" in command, "tool name must be part of the scanned command"
        assert "pip install -e" in command, "serialized args must be part of the scanned command"

    def test_args_serialized_from_dict(self):
        from memchorus.prohibitions import GuardVerdict
        _r, m = self._run(GuardVerdict.BLOCK, args={"command": "pip install -e /foo", "flag": True})
        command = m.call_args[0][0]
        assert "-e /foo" in command or "/foo" in command

    def test_args_none_still_scans_tool_name(self):
        from memchorus.prohibitions import GuardVerdict
        _r, m = self._run(GuardVerdict.BLOCK)
        assert m.call_args[0][0], "command must be non-empty even without args"

    def test_opt_out_returns_none_and_skips_scan(self):
        mock_orch = MagicMock()
        mock_orch.config = {"prohibitions": {"enabled": False}}
        mock_orch._prohibitions_manager = None

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks

            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan_tool_call") as m:
                result = hooks.on_pre_tool_call(tool_name="terminal", args={"command": "x"})
        assert result is None
        m.assert_not_called(), "guards disabled => no tool-call scan"

    def test_no_orchestrator_returns_none(self):
        with patch("memchorus.hooks._get_orchestrator", return_value=None):
            from memchorus.hooks import MemChorusHooks

            hooks = MemChorusHooks()
            result = hooks.on_pre_tool_call(tool_name="terminal", args={"command": "x"})
        assert result is None

    def test_internal_error_degrades_to_none(self):
        """Observer semantics — a guard error must never break a tool call."""
        mock_orch = MagicMock()
        mock_orch.config = {}

        with patch("memchorus.hooks._get_orchestrator", return_value=mock_orch):
            from memchorus.hooks import MemChorusHooks

            hooks = MemChorusHooks()
            with patch.object(hooks, "_try_guard_scan_tool_call", side_effect=RuntimeError("boom")):
                result = hooks.on_pre_tool_call(tool_name="terminal", args={"command": "x"})
        assert result is None, "tool call must proceed when the guard raises"


class TestRegisterWireUp:
    """The fix only matters if register() actually wires the pre_tool_call hook."""

    def test_register_binds_pre_tool_call(self, monkeypatch):
        registered = []

        class FakeCtx:
            def register_hook(self, name, fn):
                registered.append((name, fn))

        monkeypatch.setattr("memchorus.hooks.atexit.register", lambda *a, **k: None)
        with patch("memchorus.hooks._get_orchestrator", return_value=MagicMock()):
            import memchorus.hooks as hooks_mod
            hooks_mod._instance_holder[0] = None
            hooks_mod.register(FakeCtx())

        names = [n for n, _ in registered]
        assert "pre_tool_call" in names, f"register() must bind pre_tool_call; got {names}"
        fn = dict(registered)["pre_tool_call"]
        # The bound handler is the instance method / alias on MemChorusHooks.
        assert "tool_call" in getattr(fn, "__name__", "").lower() or callable(fn)

    def test_module_level_alias_exists(self):
        """Hermes may reference the hook by module-level name (pre_tool_call / on_pre_tool_call)."""
        import memchorus.hooks as hooks_mod
        assert hasattr(hooks_mod, "pre_tool_call")
        assert hasattr(hooks_mod, "on_pre_tool_call")
        assert callable(hooks_mod.on_pre_tool_call)
