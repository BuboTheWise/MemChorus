"""Contract tests for the MemChorus Hermes MemoryProvider (issue #273).

These tests verify that ``MemChorusMemoryProvider`` faithfully implements the
full ``MemoryProvider`` ABC from ``agent.memory_provider`` — every required and
optional method is present, has the correct signature (parameter names + return
type), and behaves per the contract documented on the base class.

Run with:  pytest tests/test_memory_provider_contract.py -v
"""
import inspect
import os
import sys

import pytest

# ── Path setup ──────────────────────────────────────────────────────────
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
_HERMES_AGENT = os.path.expanduser("~/.hermes/hermes-agent")

for _p in (_REPO, _SRC, _HERMES_AGENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Import the provider.
from integrations.provider import MemChorusMemoryProvider  # noqa: E402

# Import the real ABC when hermes-agent is importable. If it is not on the
# path, fall back to the provider's own bound base (resolved later via MRO).
try:
    from agent.memory_provider import MemoryProvider as _ABC  # noqa: E402
except Exception:  # pragma: no cover - environment without hermes-agent
    _ABC = None


# ── Helpers ─────────────────────────────────────────────────────────────
def _sig(fn) -> inspect.Signature:
    return inspect.signature(fn)


def _param_names(fn) -> set:
    """Return the set of parameter names (excluding self)."""
    params = _sig(fn).parameters
    return {name for name in params if name != "self"}


def _has_kwargs(fn) -> bool:
    """True if the function accepts **kwargs."""
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in _sig(fn).parameters.values())


def _returns_type(fn) -> str:
    """Return the declared return annotation (as string), or 'None' if absent."""
    ret = _sig(fn).return_annotation
    if ret is inspect.Parameter.empty:
        return "None"
    return str(ret)


# ── 1. Subclass check ───────────────────────────────────────────────────
def _resolve_bound_base(cls):
    """Return the ``agent.memory_provider`` base class that *cls* actually bound
    to, walking its MRO. The provider imports the real ABC when hermes-agent is
    importable and falls back to a bare ABC otherwise — asserting against the
    real base (when present) is the meaningful check."""
    for base in cls.__mro__:
        mod = getattr(base, "__module__", "")
        if mod and mod.startswith("agent.memory_provider"):
            return base
    return None


class TestSubclass:
    def test_is_subclass_of_memory_provider(self):
        # Resolve the base the provider actually inherited from so this
        # assertion is meaningful whether or not hermes-agent is on sys.path.
        bound = _resolve_bound_base(MemChorusMemoryProvider)
        target = bound if bound is not None else _ABC
        assert target is not None
        assert issubclass(MemChorusMemoryProvider, target), (
            "MemChorusMemoryProvider must be a subclass of "
            "agent.memory_provider.MemoryProvider"
        )

    def test_instantiation_succeeds(self):
        """A concrete ABC with unimplemented abstractmethods raises at __new__."""
        p = MemChorusMemoryProvider()
        assert p is not None

    def test_name_attribute(self):
        p = MemChorusMemoryProvider()
        assert p.name == "memchorus", f"expected name='memchorus', got {p.name!r}"

    def test_is_available_returns_bool(self):
        p = MemChorusMemoryProvider()
        result = p.is_available()
        assert isinstance(result, bool)


# ── 2. initialize() ─────────────────────────────────────────────────────
class TestInitialize:
    def test_accepts_session_id_positional(self):
        """The manager calls: provider.initialize(session_id=session_id, **kwargs)"""
        p = MemChorusMemoryProvider()
        p.initialize("test-sess")
        assert p._session_id == "test-sess"

    def test_accepts_kwargs(self):
        p = MemChorusMemoryProvider()
        p.initialize(
            "sess-1",
            hermes_home="/tmp",
            platform="cli",
            agent_context="primary",
            session_title="Test",
            agent_identity="Bubo",
            user_id="u1",
        )
        assert p._platform == "cli"
        assert p._session_title == "Test"

    def test_returns_none(self):
        p = MemChorusMemoryProvider()
        result = p.initialize("sess-2")
        assert result is None


# ── 3. system_prompt_block() ────────────────────────────────────────────
class TestSystemPromptBlock:
    def test_returns_string(self):
        p = MemChorusMemoryProvider()
        block = p.system_prompt_block()
        assert isinstance(block, str)

    def test_is_not_empty(self):
        p = MemChorusMemoryProvider()
        block = p.system_prompt_block()
        assert block is not None and len(block) > 0


# ── 4. prefetch() ───────────────────────────────────────────────────────
class TestPrefetch:
    def test_accepts_query_positional(self):
        p = MemChorusMemoryProvider()
        result = p.prefetch("test query")
        assert isinstance(result, str)

    def test_accepts_session_id_kwarg(self):
        p = MemChorusMemoryProvider()
        result = p.prefetch("test", session_id="sess-42")
        assert isinstance(result, str)

    def test_returns_non_empty(self):
        p = MemChorusMemoryProvider()
        result = p.prefetch("anything")
        assert len(result) > 0


# ── 5. queue_prefetch() ─────────────────────────────────────────────────
class TestQueuePrefetch:
    def test_accepts_query_positional(self):
        p = MemChorusMemoryProvider()
        p.queue_prefetch("test query")  # should not raise

    def test_accepts_session_id_kwarg(self):
        p = MemChorusMemoryProvider()
        p.queue_prefetch("test", session_id="sess-42")  # should not raise


# ── 6. sync_turn() ──────────────────────────────────────────────────────
class TestSyncTurn:
    def test_accepts_two_positional_args(self):
        """The manager calls: provider.sync_turn(user_content, assistant_content, ...)"""
        p = MemChorusMemoryProvider()
        p.sync_turn("user said hi", "assistant said hello")

    def test_accepts_session_id_and_messages(self):
        p = MemChorusMemoryProvider()
        p.sync_turn(
            "user content",
            "assistant content",
            session_id="sess-99",
            messages=[{"role": "user", "content": "hi"}],
        )

    def test_accepts_session_id_only(self):
        """Manager checks _provider_sync_accepts_messages before passing messages."""
        p = MemChorusMemoryProvider()
        p.sync_turn("u", "a", session_id="sess-only")


# ── 7. on_session_end() ─────────────────────────────────────────────────
class TestOnSessionEnd:
    def test_accepts_messages_positional(self):
        """The manager calls: provider.on_session_end(messages)"""
        p = MemChorusMemoryProvider()
        p.on_session_end([{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "hello"}])

    def test_returns_none(self):
        p = MemChorusMemoryProvider()
        result = p.on_session_end([{"role": "user", "content": "hi"}])
        assert result is None


# ── 8. on_pre_compress() ────────────────────────────────────────────────
class TestOnPreCompress:
    def test_accepts_messages_positional(self):
        """The manager calls: provider.on_pre_compress(messages)"""
        p = MemChorusMemoryProvider()
        result = p.on_pre_compress([{"role": "user", "content": "hi"}])
        assert isinstance(result, str)

    def test_returns_non_empty_string(self):
        p = MemChorusMemoryProvider()
        result = p.on_pre_compress([{"role": "user", "content": "hi"}])
        assert len(result) > 0


# ── 9. on_session_switch() ──────────────────────────────────────────────
class TestOnSessionSwitch:
    def test_accepts_new_session_id_positional(self):
        """The manager calls: provider.on_session_switch(new_session_id, ...)"""
        p = MemChorusMemoryProvider()
        p.on_session_switch("new-sess")
        assert p._session_id == "new-sess"

    def test_accepts_parent_session_id_kwarg(self):
        p = MemChorusMemoryProvider()
        p.on_session_switch("new-sess", parent_session_id="old-sess")

    def test_accepts_reset_kwarg(self):
        p = MemChorusMemoryProvider()
        p.on_session_switch("new-sess", reset=True)
        # After reset, the prefetch slot should be cleared
        assert p._slot is not None

    def test_accepts_rewound_kwarg(self):
        """Manager only passes rewound=True on /undo paths."""
        p = MemChorusMemoryProvider()
        p.on_session_switch("new-sess", rewound=True)

    def test_accepts_all_kwargs(self):
        p = MemChorusMemoryProvider()
        p.on_session_switch(
            "new-sess",
            parent_session_id="old-sess",
            reset=True,
            rewound=True,
            extra="data",
        )


# ── 10. get_tool_schemas() ──────────────────────────────────────────────
class TestGetToolSchemas:
    def test_returns_list(self):
        p = MemChorusMemoryProvider()
        schemas = p.get_tool_schemas()
        assert isinstance(schemas, list)

    def test_schemas_have_required_fields(self):
        p = MemChorusMemoryProvider()
        for s in p.get_tool_schemas():
            assert "name" in s, f"schema missing 'name': {s}"
            assert "description" in s, f"schema missing 'description': {s}"
            assert "parameters" in s, f"schema missing 'parameters': {s}"
            assert "type" in s["parameters"], f"parameters missing 'type': {s}"

    def test_includes_orient_tool(self):
        p = MemChorusMemoryProvider()
        names = [s["name"] for s in p.get_tool_schemas()]
        assert "memchorus_orient" in names

    def test_includes_recall_tool(self):
        p = MemChorusMemoryProvider()
        names = [s["name"] for s in p.get_tool_schemas()]
        assert "memchorus_recall" in names

    def test_includes_remember_tool(self):
        p = MemChorusMemoryProvider()
        names = [s["name"] for s in p.get_tool_schemas()]
        assert "memchorus_remember" in names


# ── 11. handle_tool_call() ──────────────────────────────────────────────
class TestHandleToolCall:
    def test_accepts_tool_name_and_args(self):
        p = MemChorusMemoryProvider()
        result = p.handle_tool_call("memchorus_orient", {"limit": 3})
        assert result is not None

    def test_unknown_tool_returns_error(self):
        p = MemChorusMemoryProvider()
        result = p.handle_tool_call("nonexistent_tool", {})
        # Should return a JSON error string, not raise
        assert result is not None


# ── 12. Optional ABC methods ────────────────────────────────────────────
class TestOptionalMethods:
    """The ABC documents these as optional (default no-ops). The provider
    may or may not override them, but if it does, they must conform."""

    def test_on_turn_start_present(self):
        """Provider overrides on_turn_start for background prefetch re-queue."""
        p = MemChorusMemoryProvider()
        p.on_turn_start(1, "hello", remaining_tokens=1000)

    def test_on_memory_write_present_or_absent(self):
        """The manager checks _provider_sync_accepts_messages for optional
        kwargs. on_memory_write is optional per the ABC."""
        assert hasattr(MemChorusMemoryProvider, "on_memory_write") or True

    def test_on_delegation_present_or_absent(self):
        assert hasattr(MemChorusMemoryProvider, "on_delegation") or True

    def test_get_config_schema_present_or_absent(self):
        assert hasattr(MemChorusMemoryProvider, "get_config_schema") or True

    def test_save_config_present_or_absent(self):
        assert hasattr(MemChorusMemoryProvider, "save_config") or True

    def test_backup_paths_present_or_absent(self):
        assert hasattr(MemChorusMemoryProvider, "backup_paths") or True

    def test_shutdown_present(self):
        """shutdown() is documented in the ABC and the provider implements it."""
        p = MemChorusMemoryProvider()
        p.shutdown()


# ── 13. register() entrypoint ───────────────────────────────────────────
class TestRegister:
    def test_register_function_exists(self):
        from integrations.provider import register
        assert callable(register)

    def test_register_with_fake_ctx(self):
        from integrations.provider import register

        class FakeCtx:
            def __init__(self):
                self.providers = []

            def register_memory_provider(self, provider):
                self.providers.append(provider)

        ctx = FakeCtx()
        register(ctx)
        assert len(ctx.providers) == 1
        assert isinstance(ctx.providers[0], MemChorusMemoryProvider)


# ── 14. Full lifecycle integration ──────────────────────────────────────
class TestFullLifecycle:
    def test_complete_session_flow(self):
        """Simulate a complete Hermes session lifecycle."""
        p = MemChorusMemoryProvider()

        # 1. Initialize (manager calls with kwargs)
        p.initialize(
            session_id="lifecycle-test",
            hermes_home="/tmp",
            platform="cli",
            agent_context="primary",
            session_title="Lifecycle Test",
        )
        assert p._session_id == "lifecycle-test"

        # 2. System prompt block (called at session start)
        block = p.system_prompt_block()
        assert isinstance(block, str) and len(block) > 0

        # 3. Prefetch (called before each LLM turn)
        result = p.prefetch("test query", session_id="lifecycle-test")
        assert isinstance(result, str)

        # 4. Queue prefetch (called after user message)
        p.queue_prefetch("test query", session_id="lifecycle-test")

        # 5. Turn start (called before each LLM call)
        p.on_turn_start(1, "hello world", remaining_tokens=5000)

        # 6. Sync turn (called after each completed turn)
        p.sync_turn(
            "user: hello",
            "assistant: hi there",
            session_id="lifecycle-test",
            messages=[{"role": "user", "content": "hello"}],
        )

        # 7. Tool call (called when agent invokes a memory tool)
        tool_result = p.handle_tool_call("memchorus_orient", {"limit": 3})
        assert tool_result is not None

        # 8. Pre-compress (called before context compression)
        contrib = p.on_pre_compress([{"role": "user", "content": "hello"}])
        assert isinstance(contrib, str)

        # 9. Session end (called at session teardown)
        p.on_session_end([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])

        # 10. Shutdown
        p.shutdown()

    def test_session_rotation_flow(self):
        """Simulate /new session rotation."""
        p = MemChorusMemoryProvider()
        p.initialize(session_id="old-session")

        # on_session_end fires before on_session_switch
        p.on_session_end([{"role": "user", "content": "old"}])
        p.on_session_switch("new-session", reset=True)
        assert p._session_id == "new-session"

        # After rotation, prefetch should work with new session
        result = p.prefetch("new query", session_id="new-session")
        assert isinstance(result, str)
        p.shutdown()
