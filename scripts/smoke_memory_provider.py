"""Smoke test: import the MemChorus provider against the LOCAL repo and
exercise the core MemoryProvider ABC methods.

Run from the repo root. We force the local src/ ahead of sys.path so we do
not accidentally bind to the stale offloaded workspace.
"""
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(REPO, "src")
sys.path.insert(0, REPO)          # so `integrations` is importable
sys.path.insert(0, SRC)           # so `memchorus` resolves to the local tree
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))  # agent.memory_provider

# 1) Confirm we are bound to the LOCAL memchorus, not the offloaded one.
import memchorus
print("memchorus path ->", getattr(memchorus, "__file__", "?"))
assert "t_06936ea3" not in (memchorus.__file__ or ""), f"stale workspace: {memchorus.__file__}"

# 2) Import the provider module.
from integrations.provider import MemChorusMemoryProvider, register  # noqa: E402
print("provider import OK")

# 3) Verify it is a real MemoryProvider subclass and has no unimplemented ABCs.
try:
    from agent.memory_provider import MemoryProvider as MP
except Exception as e:  # pragma: no cover
    MP = None
    print("WARN: could not import agent.memory_provider ->", e)

if MP is not None:
    assert issubclass(MemChorusMemoryProvider, MP), "not a MemoryProvider subclass"
    print("subclass of agent.memory_provider.MemoryProvider: OK")
    # A concrete ABC with unimplemented abstractmethods raises at __new__ time.
    p = MemChorusMemoryProvider()
    print("instantiation (no abstract leftovers): OK")
else:
    p = MemChorusMemoryProvider()
    print("instantiated against ABC fallback base: OK")

# 4) Exercise the lifecycle surface.
print("name ==", p.name)
print("is_available ==", p.is_available())

p.initialize(
    "smoke-sess",
    hermes_home=os.path.expanduser("~/.hermes"),
    platform="cli",
    agent_context="primary",
    session_title="smoke test",
)
print("initialize: OK")

block = p.system_prompt_block()
assert block and "MemChorus Orientation" in block, f"unexpected block: {block!r}"
print("system_prompt_block (fallback header): OK")
print("---- orientation block ----")
print(block)
print("---- end block ----")

pref = p.prefetch("anything", session_id="smoke-sess")
assert isinstance(pref, str) and pref, "prefetch must return non-empty str"
print("prefetch: OK")

p.queue_prefetch("anything")
print("queue_prefetch: OK")

p.on_turn_start(1, "hello", remaining_tokens=1000)
print("on_turn_start: OK")

p.sync_turn("user said hi", "assistant said hello", session_id="smoke-sess",
           messages=[{"role": "user", "content": "hi"}])
print("sync_turn: OK")

p.on_session_end([{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "hello"}])
print("on_session_end: OK")

contrib = p.on_pre_compress([{"role": "user", "content": "hi"}])
assert isinstance(contrib, str), f"on_pre_compress must return str, got {type(contrib)}"
print("on_pre_compress ->", repr(contrib[:60]), "OK")

p.on_session_switch("new-sess", parent_session_id="smoke-sess", reset=True)
assert p._session_id == "new-sess", f"session switch not applied: {p._session_id}"
print("on_session_switch (reset=True rebind): OK")

# 5) Tool schemas + dispatch.
schemas = p.get_tool_schemas()
names = [s["name"] for s in schemas]
print("tool schemas:", names)
for want in ("memchorus_orient", "memchorus_recall", "memchorus_remember"):
    assert want in names, f"missing tool {want}"
for s in schemas:
    assert "description" in s and "parameters" in s and "type" in s["parameters"]

out = p.handle_tool_call("memchorus_orient", {"limit": 3})
print("handle_tool_call(orient) ->", out[:120])
out2 = p.handle_tool_call("memchorus_recall", {"query": "x", "limit": 2})
print("handle_tool_call(recall) ->", out2[:120])
out3 = p.handle_tool_call("memchorus_remember", {"key": "k1", "content": "v1", "category": "LEARNING"})
print("handle_tool_call(remember) ->", out3[:120])
out4 = p.handle_tool_call("nope", {})
print("handle_tool_call(unknown) ->", out4[:120])

# 6) register() entrypoint (what the plugin loader calls).
import inspect
print("register signature ->", inspect.signature(register))

p.shutdown()
print("shutdown: OK")

print("\nALL SMOKE CHECKS PASSED")
