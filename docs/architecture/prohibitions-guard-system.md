# Behavioral Prohibitions Guard System

**Version:** 1.0 | **Status:** Implemented on master | **Committed:** 2026-08-18 (a296265)
**Modules:** `prohibitions.py`, `prohibition_distiller.py`, hooks wiring in `hooks.py`

---

## 1. Problem Statement

Agents repeatedly self-destruct by re-running destructive actions: editable installs that break their own environments, path deletions causing ModuleNotFoundError, venv corruption, toolchain breakage. These patterns get stored as "memories" (soft-recall data) but are never enforced at call time — the agent sees the past mistake in context but still repeats it because soft recall is advisory, not prohibitive. The guard system converts high-severity mistakes into hard behavioral rules that block before execution.

## 2. Architecture Overview

Two entry points in the prohibitions architecture:

| Entry Point | Module | Trigger | Action |
|---|---|---|---|
| **Entry #1 — Pre-flight scan** | `hooks.py::_scan_prohibitions()` called from `on_pre_llm_call` (line ~266) | Every LLM call, before soft recall injection | Scans incoming user_message + recent conversation history against all active prohibition rules; injects `[[[[GUARD]]]` blocks into context when matched |
| **Entry #2 — Post-storage distillation** | `hooks.py::_try_distill_prohibition()` called after error-path tool results (line ~193) | After `on_post_tool_call` fires on failed or errored actions (lines 120, 186) | Classifies the mistake severity, auto-creates new prohibition rules from CRITICAL self-breaking patterns and persists them via `ProhibitionsManager` |

### Data flow diagram

```
Agent takes action
        │
        ▼  ┌───────────────┐
  Tool executes ──►│ post_tool_call  │
                   │ hook fires      │
        │          └───────┬─────────┘
        │                  │
        │ failed/errored?  │ yes
        │                  ▼
        │         ┌───────────────────┐
        │   ╭─►│ _try_distill_       │──► CRITICAL? → new Prohibition rule saved
        │  │  │ prohibition()        │    via ProhibitionsManager.persist()
        ▼  ╰─►│ (Entry #2)           │──► not CRITICAL → ignored
     Next LLM call                     LOW/medium → ignored
        │
        ▼  ┌───────────────┐
         │ on_pre_llm_call │──► _scan_prohibitions() (Entry #1)
        │ hook fires      │    checks all rules against current context
        │                 │    matched? → injects [[GUARD]] block into "context" key
        ▼  └───────────────┘
   LLM receives guarded prompt with prohibitive context
```

## 3. Core Modules

### 3.1 `prohibitions.py` — Rule Model + Manager (352 lines)

**Key classes:**

- **`Prohibition` (dataclass):** Immutable rule definition containing:
  - `id`: unique identifier (e.g., `"seed-pip-install-e"`)
  - `condition`: human-readable "Never do X because Y" statement
  - `trigger_keywords`: list of substrings to match against agent input
  - `tool_call_check`: regex pattern for tool-call argument matching
  - `severity`: integer severity level (3 = CRITICAL)
  - `block_action`: what text gets injected when this rule fires
  - `rationale`: explanation of why the rule exists
  - `source`: origin tag (`"seed"` for built-in, `"distilled-from-mistake"` for auto-generated)
  - `type`: category (e.g., `"infrastructure"`)
  - `created`: ISO timestamp string

- **`GuardResult` (dataclass with counters):** Return type from scan operations containing:
  - `triggered_rules`: list of matched Prohibition objects
  - `injection_blocks`: formatted [[GUARD]] blocks ready for prompt injection
  - `scan_ms`: timing measurement, match counts per severity tier

- **`ProhibitionsManager`:** Runtime coordinator that:
  - Loads seed rules from `_DEFAULT_SEED_RULES` on init (3 built-in rules covering editable installs, scratch directory deletion, OPSEC leak prevention)
  - `scan(input_text, conversation_history=None)`: checks text + recent messages against all active rules; returns GuardResult with matched rules and injection blocks
  - `persist(rule_dict)`: adds a distilled rule to the active set (dedup by ID)
  - Cached on orchestrator as `_prohibitions_manager` attribute for reuse across turns

### 3.2 `prohibition_distiller.py` — Auto-Guard Generation (383 lines)

**Key classes:**

- **`MistakeSeverity(Enum):`** CRITICAL=3, MEDIUM=2, LOW=1
- **`DistillationConfig`:** Controls distillation thresholds:
  - `minimum_severity = 3` (only CRITICAL → new rules)
  - `max_rules_per_session = 2` (prevent guard explosion)
  - `cooldown_hours = 24.0` (no duplicate guards within 24h of same pattern hash)

- **`ProhibitionDistiller`:** The distillation engine:
  - `SELFBREAK_KEYWORDS` (13 patterns): ModuleNotFoundError, broken editable install, .pth shim file, site-packages corrupted, venv damaged, hermes broken, cli crashed, env pollution, pip install -e, source path deleted, etc.
  - `NONDESTRUCTIVE_KEYWORDS` (7 patterns): recall returned nothing useful, no matching memories, search returned empty, cache miss, etc. — these are explicitly excluded
  - `is_worthy_of_guard(error_text) → MistakeSeverity`: keyword-based classifier
  - `distill(error_text, context=None) → Optional[dict]`: full distillation pipeline with 3 gates (severity threshold, session cap, cooldown). Returns a dict matching Prohibition.from_dict() schema or None.
  - `_extract_keywords`, `_build_condition`, `_build_rationale`, `_build_block_action`, `_build_tool_call_regex`: keyword extraction and rule construction helpers

### 3.3 Hooks Wiring (`hooks.py`)

Two internal functions handle the integration:

- **`_try_distill_prohibition(text, orchestrator)`:** Called from error-handling paths in `post_tool_call`. Imports distiller, calls `.distill()`, persists any CRITICAL result through the prohibitions manager. Gracefully degrades if either module is unavailable.

- **`_scan_prohibitions(input_text, conversation_history, orchestrator)`:** Called early in `on_pre_llm_call` before soft recall injection. Reuses or creates `_prohibitions_manager` on the orchestrator. Scans current message + recent history against all rules. If matched, appends formatted `[GUARD]` blocks to the return dict's `"context"` key (matching the Hermes turn_context.py contract at `turn_context.py:538-569`).

## 4. Acceptance Criteria Verification

| Criterion | Status | Verified By |
|---|---|---|
|| Seed rules cover pip install -e, scratch directory deletion, OPSEC leak prevention | ✅ PASS | `prohibitions.py:120-199` — 3 seed rules defined in `_DEFAULT_SEED_RULES` |
| `ModuleNotFoundError hermes_cli` → CRITICAL classification | ✅ PASS | `prohibition_distiller.py:60-72` — "modulenotfounderror" in SELFBREAK_KEYWORDS, line 126-132 routes to CRITICAL |
| `recall returned nothing useful` → NON_WORTHY | ✅ PASS | `prohibition_distiller.py:75-83` — exact string in NONDESTRUCTIVE_KEYWORDS, returns LOW at line 119 |
| Distilled rules persist via ProhibitionsManager after storage path failure | ✅ PASS | `hooks.py:203-226` — full persistence pipeline with lazy manager initialization |
| Pre-flight scan fires before soft recall on every LLM call | ✅ PASS | `hooks.py:357-386` — wired into `on_pre_llm_call` at line ~95 |
| Cooldown prevents duplicate guards within 24h window | ✅ PASS | `prohibition_distiller.py:337-349` — MD5 hash fingerprint + timestamp comparison |

## 5. Seed Rules (Built-in)

The following prohibitions ship with the system — no training or distillation required:

| Rule ID | Trigger Keywords | Condition |
|---|---|---|
| `guard-001-no-editable-install` | `pip install -e`, `editable install`, `pip install .`, `-e ~/.hermes` | Never run editable installs into self-critical venv paths — always install from pushed GitHub commits |
| `guard-002-no-scratch-delete` | `rm -rf ~/.hermes`, `delete site-packages`, `unlink hermes`, `remove hermes dir` | Never delete ~/.hermes or the active installation directory (removes the agent itself) |
| `guard-003-no-public-opsec-leak` | commit messages, documentation with personally identifiable information | Do not leak internal identifiers to public repositories |

## 6. Testing

Two dedicated test modules verify prohibitions and distillation behavior:

- **`tests/test_prohibitions.py`** — 31 tests across 4 classes covering GuardVerdict/GuardResult semantics, Prohibition serialization round-trips, regex pattern compilation (including invalid-regex handling), case-insensitive text matching, tool_call_check binding, and full ProhibitionsManager operations (seed loading, add/remove/dedup persistence, scan_text and scan_tool_call against each guard).
- **`tests/test_distiller.py`** — 25 tests across 8 classes covering MistakeSeverity enumeration, DistillationConfig thresholds, severity classification of self-breaking vs nondestructive patterns, the full distill() pipeline with gate checks (severity threshold, session rule cap, cooldown dedup), keyword extraction, condition/rationale builders, tool-call regex generation, and cooldown hash helpers.

Hook wiring is additionally covered indirectly by `test_hooks.py` and end-to-end scenarios in `test_session_simulation.py`.

## 7. Configuration

Runtime knobs are controlled via `config_example.yaml` under the `prohibitions:` block:

| Config Key | Default | Description |
|---|---|---|
| `prohibitions.enabled` | `true` | Master toggle — when false, guard scan is entirely skipped |
| `prohibitions.distillation_enabled` | `true` | Auto-distillation toggle — when false, no new rules are created from CRITICAL mistakes |
| `distillation.minimum_severity` | `3` | Floor for distillation (CRITICAL only) |
| `distillation.max_rules_per_session` | `2` | Cap on new rules per cycle prevents runaway generation |
| `distillation.cooldown_hours` | `24.0` | Pattern-hash window preventing duplicate guards from near-identical errors |

Set `prohibitions.enabled: false` to disable all guard scanning without removing seed rules or distilled state.
Set `prohibitions.distillation_enabled: false` to allow pre-existing guards while stopping auto-creation of new ones.

### Default Behavior Without Config

If no external configuration file is provided, the system uses safe defaults that are equivalent to:
```yaml
prohibitions:
  enabled: true
  distillation:
    enabled: true
    minimum_severity: 3
    max_rules_per_session: 2
    cooldown_hours: 24.0
```

Only CRITICAL severity mistakes (score 3) become new guards. Maximum 2 new rules per session instance prevents runaway auto-generation. 24-hour cooldown window per pattern hash prevents duplicate rules from near-identical errors.
