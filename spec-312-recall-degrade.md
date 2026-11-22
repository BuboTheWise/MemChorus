# Behavior Contract Spec — MemChorus #312: recall-injection loud-degrade + doctor in-process interpreter probe

Status: DECIDED (2026-11-22) · Decided by: orchestrator profile · Repo: BuboTheWise/MemChorus
Live base: master at 9d0cd46. Affected code: `src/memchorus/hooks.py`
(`_get_orchestrator` L72–L79, `MemChorusHooks.on_pre_llm_call` L362ff),
`src/memchorus/install_doctor.py` (probe D pattern at ~L2655).

## Problem (from #312)

Recall injection can be **silently dead** in the gateway. The hook
(`memchorus.hooks`, loaded via the `hermes_agent.plugins` entry point into the
gateway's in-process tool interpreter) does
`orchestrator = _get_orchestrator()` → `if orchestrator is None: return None`
(hooks.py L371–L373). No warning, no log line: the LLM call proceeds with no
recall block, and nothing in the gateway-visible log says why. Meanwhile the
MCP surface (a separate stdio subprocess under the install venv) can stay
green, so the failure only ever surfaces as "memories stopped being injected".

Root cause: the in-process interpreter (the gateway/tool python under
`~/.hermes/tools/python-*/bin/python3`) and the install venv used by the
CLI/MCP subprocesses are **different interpreters**. When memchorus is
importable only in the install venv, `import memchorus` fails inside the
gateway process and every `on_pre_llm_call` silently no-ops. Verified on this
box (2026-11-22):

```
$ ~/.hermes/tools/python-3.14.9+20261021-linux-x64/bin/python3 -c "import memchorus"
ModuleNotFoundError: No module named 'memchorus'
```

Three contract clauses close the gap. All three are binding on the follow-up
dev cards.

## Clause 1 — Loud-degrade in `MemChorusHooks.on_pre_llm_call`

`MemChorusHooks.on_pre_llm_call` (src/memchorus/hooks.py) MUST NOT fail
silently: when the orchestrator cannot be resolved in the in-process
interpreter (`import memchorus` fails, or `_get_orchestrator()` returns
None), the hook MUST:

- **(a)** emit exactly ONE WARNING per process via the gateway-visible
  logger — message:
  `MemChorus recall disabled: orchestrator unavailable in this interpreter (<reason>)`
  where `<reason>` is `ModuleNotFoundError: <name>` or `no orchestrator instance`;
- **(b)** still return None (never raise into the LLM call);
- **(c)** not repeat the warning on subsequent calls in the same process.

Expected-vs-actual semantics:

- **Expected (degraded path):** first `on_pre_llm_call` in a process where the
  orchestrator is unresolvable produces exactly ONE WARNING line (message above,
  real reason) in the gateway-visible log; return value `None`.
- **Actual (today):** no WARNING at all — `_get_orchestrator()` logs at DEBUG
  (hooks.py L78) and the None branch returns silently (hooks.py L372–L373); the
  operator sees no signal that recall is dead.
- **Expected (repeat calls):** a second call in the same process emits no new
  warning line and still returns `None`.
- **Expected (healthy path):** when the orchestrator resolves, injection
  behavior is unchanged from today; the once-flag must not suppress or alter a
  healthy render.

## Clause 2 — Doctor in-process probe (probe E)

`memchorus-doctor` (src/memchorus/install_doctor.py) MUST add probe E
(following the probe D pattern at ~line 2655) that verifies the
**in-process interpreter** — the gateway/tool python under
`~/.hermes/tools/python-*/bin/python3`, NOT the install venv — can
`import memchorus` and resolve the orchestrator module. Red on failure with
expected-vs-actual:

```
expected: import memchorus OK under <interpreter>
actual:   <first line of traceback>
```

Expected-vs-actual semantics:

- **Expected (green):** the tool python (first match of
  `~/.hermes/tools/python-*/bin/python3`, sorted glob) runs the import and
  orchestrator-module resolution; the probe reports `in-process interpreter OK
  (<path>)`.
- **Expected (red):** the probe reports exactly the `expected:` / `actual:`
  pair above, where `actual` is the first line of the traceback captured from
  the tool-python run (e.g. `ModuleNotFoundError: No module named 'memchorus'`); the
  doctor's overall exit code goes non-zero per the existing red-probe contract.
- **Expected (no tool python found):** yellow/skip with reason
  `no tool python found under ~/.hermes/tools` — a skip must be distinguishable
  from a red.
- **Must be capable of red:** on a box whose tool python lacks memchorus, a
  probe that stays green is itself a defect.

## Clause 3 — Interpreter interop (documented invariant)

The MCP server (separate subprocess, install venv) and the in-process hook
(gateway tool python) resolve memchorus under DIFFERENT interpreters.
Therefore:

- green MCP does NOT imply a live in-process hook;
- green install-venv CLI does NOT imply a live in-process hook;
- `hermes gateway restart` alone does NOT fix a missing in-process import — it
  re-executes the same interpreter that failed.

Correct repair: make memchorus importable under the gateway/tool interpreter
(install into / align with that interpreter's site-packages, or re-provision
the tool python), then restart the gateway. Probe E (Clause 2) is the gate that
distinguishes these two surfaces; its red output must name the interpreter path
it actually probed.

## Implementation outline

1. `src/memchorus/hooks.py`:
   - module-level once-flag + `_warn_recall_disabled(reason)`: on first call in
     a process, `logger.warning("MemChorus recall disabled: orchestrator
     unavailable in this interpreter (%s)", reason)` and set the flag;
   - at the `orchestrator is None` site (L371–L373): reason
     `no orchestrator instance`; keep `return None`;
   - at the import-failure site in `_get_orchestrator()` (L72–L79, currently
     `logger.debug`): keep the debug AND surface
     `ModuleNotFoundError: <name>` through the same once-only warning path;
   - no other behavior change; no new deps.
2. `src/memchorus/install_doctor.py`: probe E per Clause 2, mirroring probe D's
   integration points (report dict, render helper, default probe run,
   `--json` output).
3. Docs: the Clause 3 paragraph must be reachable from the README/GAP doc so
   triage does not conflate the MCP surface with the in-process surface.

## Acceptance criteria (for IMPL)

- First `on_pre_llm_call` with orchestrator unavailable → exactly ONE WARNING
  with the reason string; returns None. Second call in the same process → no
  new warning; returns None. (New test: monkeypatch `_get_orchestrator` → None;
  call twice; assert exactly one warning via caplog.)
- Healthy interpreter → injection unchanged (existing hooks tests pass).
- `memchorus-doctor` default run → probe E shows the expected-vs-actual pair
  when the tool python lacks memchorus; doctor exit code non-zero.
- Probe function called directly with an interpreter that CAN import memchorus
  → green report.
- Probe E test with a python that lacks memchorus (or a nonexistent path) →
  red, expected-vs-actual strings asserted in the report.
- OPSEC: no agent names, no absolute home paths beyond `~/.hermes/...`
  patterns, no email domains in any diff or commit message/trailer.

## Out of scope (this cycle)

- Auto-repair of the tool interpreter (doctor reports; it does not install
  into the tool python).
- Unifying MCP and in-process resolution under a single interpreter
  (long-term shape; follow-up spec if pursued).
