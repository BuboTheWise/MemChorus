# MemChorus as a Hermes MemoryProvider

> **Scope:** This document describes how MemChorus plugs into Hermes as a
> first-class **memory provider** — implementing the full
> `agent.memory_provider.MemoryProvider` ABC — and how it coexists with
> MemPalace and the Hermes built-in memory surfaces. It covers the
> orientation/policy layer, the storage-routing boundary, the fallback
> story, and the two integration entry points (the blocking hook plugin vs.
> the provider plugin).

---

## 1. What this is

MemChorus ships a Hermes **memory provider**: a Python class that implements
the abstract `MemoryProvider` interface Hermes uses to talk to *any* memory
backend (Honcho, Mem0, Hindsight, …). From Hermes' point of view MemChorus is
just another provider, selected by name. What makes it different is that
MemChorus is an **orchestrator** in front of one or more real stores —
MemPalace (via MCP) and/or the Hermes built-in default memory files.

Two responsibilities are cleanly separated:

| Concern | Owner |
|---|---|
| **Orientation & policy** — what context to force on session start, how deep to recall, which guards/prohibitions apply, provenance attribution | **MemChorus** (this provider) |
| **Storage** — durable semantic search, knowledge graph, per-domain "wings", diary | **MemPalace** (or the Hermes built-in files as the always-available fallback) |

MemChorus **owns orientation and policy, not storage.** It wraps whatever
store is reachable and decides *when* and *what* to surface, never
*where the bytes physically live* beyond routing.

---

## 2. The provider contract (what the ABC demands)

Hermes' `MemoryProvider` ABC defines a fixed surface. MemChorus implements
every required method and the optional lifecycle hooks:

| Method | Contract | MemChorus behaviour |
|---|---|---|
| `name` | Provider identifier | `"memchorus"` |
| `is_available()` | Can this provider serve? | `True` — orientation works even with no store reachable (see §5) |
| `initialize(session_id, **kwargs)` | Session bootstrap | Captures `session_id`, platform, title, agent identity |
| `system_prompt_block()` | Static guidance injected into the system prompt | Orientation preamble + provenance rules |
| `prefetch(query, **kw)` | Context for the upcoming LLM call | **Synchronous** read of the cached prefetch slot (built by the background worker) |
| `queue_prefetch(query, **kw)` | Kick off a background build | Spawns/refreshes the background prefetch slot |
| `on_turn_start(turn, message, **kw)` | Per-turn hook | Re-queues prefetch keyed to the live message |
| `sync_turn(user, assistant, **kw)` | Post-turn write capture | Writes the turn through the enforcement + checkpoint pipeline |
| `on_session_end(messages)` | Session teardown | Final checkpoint flush |
| `on_pre_compress(messages)` | Before context compression | Emits a contribution string summarising must-keep context |
| `on_session_switch(new_id, **kw)` | Session rotation | Flushes the old slot, resets orientation cache, adopts new id |
| `get_tool_schemas()` | Agent-callable tools | `memchorus_orient`, `memchorus_recall`, `memchorus_remember` |
| `handle_tool_call(name, args)` | Tool dispatch | Routes to the orchestrator (orient / search / save) |
| `shutdown()` | Provider teardown | Cancels the background worker, flushes |

**Contract tests:** `tests/test_memory_provider_contract.py` verifies every
one of these against the *real* `agent.memory_provider.MemoryProvider` ABC
(subclass check, per-method signatures, return types, and two full
end-to-end lifecycle simulations). The suite passes with `44/44`.

---

## 3. Background prefetch — replacing the blocking read

The legacy hook integration (`integrations/hermes/`) ran recall in
`pre_llm_call`, which is a **blocking** synchronous read on the critical path
before every LLM turn (~110 ms typical, much worse on a cold MemPalace).

The provider restructures this:

1. `queue_prefetch` / `on_turn_start` fire **immediately** and dispatch work
   to a background worker (`_PrefetchSlot` + a thread).
2. The worker resolves orientation, searches the store, and renders the
   orientation block into the slot.
3. `prefetch` — the one call that *does* sit on the critical path — reads the
   **already-built** slot. If the worker hasn't finished, it degrades to the
   cached/forced-orientation fallback rather than blocking.

Net effect: the turn's critical path touches only the in-memory slot; the
expensive store round-trip runs out-of-band and is consumed on the *next*
turn.

---

## 4. Coexistence with MemPalace

MemChorus and MemPalace are **complementary, not competing**:

```
                       ┌──────────────────────────────────────────┐
   Hermes agent  ─────▶│  MemChorus provider (orientation + policy)│
   (MemoryProvider ABC)└──────────────────────────────────────────┘
                       │        ▲
              routes    │        │  recall/provenance
             reads/writes│       │
                       ▼        │
        ┌──────────────────────────────────────────────────┐
        │  MemPalace  (semantic search + knowledge graph)   │
        │  — via MCP stdio, per-profile palace DB          │
        └──────────────────────────────────────────────────┘
                       │
              fallback  │  (MCP unreachable / no [mcp] extra)
                       ▼
        ┌──────────────────────────────────────────────────┐
        │  Hermes built-in default memory files            │
        │  — always-available local layer                  │
        └──────────────────────────────────────────────────┘
```

- **MemPalace** is the *storage* enhancement: semantic search, the knowledge
  graph, wings, diary. Reached via MCP stdio.
- **Hermes built-in files** are the *guaranteed floor*: they always work and
  are what orientation falls back to when MemPalace is cold or down.
- **MemChorus** is the *policy brain*: it decides routing (see the Storage
  Routing Matrix in the README), enforces behavioural guards, forces
  session-start context, and attributes provenance — wrapping whichever
  store is reachable.

This is the opposite of a swap: MemPalace is never *disabled* by MemChorus.
MemChorus *adds* the orientation/policy layer on top, and degrades (not
fails) when the store is absent.

---

## 5. The "woke up" forced-context fallback

A hard requirement: **orientation must still work with MemPalace
disabled.** When the store is unreachable (MCP server down, `[mcp]` extra not
installed, cold cache), the provider:

- still emits `system_prompt_block()` (the orientation preamble is static —
  it never depends on the store);
- serves `prefetch` from the cached/forced-orientation slot instead of a live
  search;
- `is_available()` returns `True` (the *policy* layer is always available,
  even if the *storage* layer is not);
- routes `sync_turn` / `memchorus_remember` writes to whichever source is
  reachable (Hermes default files), so no write is dropped.

The store's absence is logged as a warning, never raised as a fatal error.

---

## 6. Two integration entry points

MemChorus exposes **two** ways to integrate with Hermes. They are distinct
and can coexist:

| Entry point | Path | Plugin name | Mode |
|---|---|---|---|
| **Hook plugin** | `integrations/hermes/` | `memchorus-integration` | Blocking `pre_llm_call` recall + `post_tool_call` capture |
| **Provider plugin** | `integrations/provider/` | `memchorus` | Full `MemoryProvider` ABC, background prefetch |

- The **hook plugin** is the original, minimal integration: synchronous,
  blocking, and focused on the pre-decision recall + post-action capture
  hooks. It has no ABC to satisfy.
- The **provider plugin** is the new, first-class integration: it plugs into
  Hermes' memory-provider selection (`plugins/memory/`), implements the full
  ABC, and replaces the blocking read with background prefetch.

Hermes' provider loader (`plugins/memory/__init__.py`) discovers providers by
scanning `plugins/memory/<name>/` (bundled) and `$HERMES_HOME/plugins/<name>/`
(user-installed) for a directory containing `__init__.py` with a
`MemoryProvider`-implementing class or a `register(ctx)` entrypoint, plus an
optional `plugin.yaml` for the description. Install the provider by placing
`integrations/provider/` under the user plugins path as `memchorus/`:

```
~/.hermes/plugins/memchorus/
    __init__.py      # from integrations/provider/__init__.py
    plugin.yaml      # name: memchorus
```

then select it as the active memory provider.

---

## 7. Testing & verification

- **Contract suite:** `pytest tests/test_memory_provider_contract.py -v`
  — 44 tests, all passing against the real ABC.
- **Smoke import:** `_smoke_provider.py` (repo root) forces the local `src/`
  tree onto `sys.path` (to avoid a stale installed `memchorus`) and exercises
  every ABC method end-to-end.
- **Coexistence proof:** the suite asserts `is_available() == True` and
  non-empty `system_prompt_block()` / `prefetch` under the no-store
  condition, which is the "woke up with MemPalace down" guarantee.

---

*Issue: BuboTheWise/MemChorus #273 — "Implement MemChorus as a Hermes
MemoryProvider".*
