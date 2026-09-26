# Pointer Model & the Three Memory Surfaces

The single most important shape a new contributor needs before touching
MemChorus is not "where files are" but *how the system keeps three different
kinds of memory distinct* — and how it lets the token block point at a body
instead of inlining it. This document is the baseline those contracts are
promised against. It is the **source of truth for the pointer-model
contract** and the **orientation doc for the three surfaces**; it does **not**
re-spec the write-time routing classifier or the recall-loop temporal
contract — those each live in their own spec, and these are the *one* place
each is written.

> Read order: the three surfaces → how they reference each other → the pointer
> model → how a pointer can go wrong (the pointer-integrity triad) → the
> routing / validity / temporal contracts at contributor level → how to add a
> new surface.
>
> Cross-reference convention: `See:` lines are **authoritative pointers**. A
> `See:` line means "the full rule lives there, not here." A contract is
> stated fully in exactly one place in the system; this doc states the
> pointer model, and *links* to every other contract.

---

## 1. The three memory surfaces

MemChorus sits on **three distinct memory surfaces**. This is not a storage
layout — it is a *shape* decision made per-fact. The same piece of knowledge
has the wrong shape on the wrong surface, and the downstream recall issues
each exist to manage a mismatch that was set at write time.

| Surface | What it is | When a fact lives here | What the user sees |
|---|---|---|---|
| **MEMORY** — the host agent's persistent personal-memory files (`MEMORY.md` / `USER.md`) | Compact, high-signal standing facts, injected into **every** turn of the host session | A **standing preference** or **rule-of-conduct** that must always be present: short, imperative, timeless ("no agent names in commit trailers"). Cheap because injected, not re-searched. | A one-line fact the agent *always* has. The user sees its effect, never a raw block — it is ambient context, not a recall hit. |
| **DRAWER** — MemPalace drawer layer (the default sink) | Verbatim stored records, reachable by `search()` and `retrieve(key=...)` | A **verbatim artefact**: tool-output dumps, a benchmark run, a decision record, long prose. Anything that is *content*, not *state*. The conservative default. | A body in a recall block — either inlined (short / relevant) or collapsed to a `retrieve(key=...)` pointer (see §3). |
| **KG** — MemPalace knowledge graph | Typed, validity-scoped facts (`subject → predicate → object`), reachable by `kg_query` / `kg_subgraph`; supports `invalidate`, `supersede`, `timeline` | A **rule of state**: a value *expected to change over time* — a version pin, a branch-naming convention, a "current X = Y". It needs a validity window and supersession. | A current value the agent can query with `as-of` scope and a timeline ("what was true then"). |

The surface names above are the canonical identifiers (`MEMORY` / `DRAWER` /
`KG`) used by the write-time routing decision. A record's surface is recorded
on it as `routing_kind` so a reader can tell *why* it is on its surface
(§5).

> **Relationship to the README "Storage Routing Matrix".** The matrix in the
> README (`MemoryProfile` → backend) is the **coarse pre-existing heuristic**
> that already routes some profiles to the right surface by accident of design
> (`user_preference` → local files ≈ MEMORY; `long_lived_knowledge` →
> MemPalace ≈ DRAWER/KG; `relationship_graph` → KG). The write-time routing
> decision formalizes and *supersedes* that heuristic: it makes the
> surface→fact decision **explicit and testable** rather than an implicit
> profile default. They point at the same place; the spec owns the decision.

`See: the routing spec — Issue #226, board card t_db50c29c` (https://github.com/BuboTheWise/MemChorus/issues/226)
— the write-time classifier: the decision rules, the default-fallback rationale,
and the 5+ concrete `(input-fact → expected-surface)` acceptance examples. **This
doc names the surfaces; the spec owns the rules.**

---

## 2. How surfaces reference each other

A surface is never a dead end. The invariant, stated plainly:

> **Every body is reachable by a stable key, regardless of which surface it is
> on.** Surfaces differ in *shape* and *validity*, not in *addressability*. The
> reference between them is a **pointer** — and a pointer is a *contract*, not
> a promise. The two consequences the contract depends on are (1) the pointer
> resolves, and (2) a body on one surface can name the body on another.

The concrete reference mechanisms that exist today:

- **Drawer → body.** `retrieve(key)` returns the verbatim body. This is the
  single most load-bearing pointer in the system — the whole low-signal gate
  (§3) collapses bodies *because* `retrieve(key)` is the sanctioned way to
  re-expand them.
- **Drawer / KG → project record location.** A `project:<slug>` record carries
  two independent structured channels — `location` (a `canonical_root` +
  `verified_at` + a `source` that is either `ssot:<doc>#<anchor>` or
  `derived:<rule>`) and `standard` (a `skill` + `doc_path` + gist + topics).
  `location.source` is precisely a *pointer into the SSoT* for "where does the
  project really live," and `standard` is a *pointer into a skill + doc* for
  "how do I recall it." These are defined in `src/memchorus/project_record.py`;
  the key namespace is `project:` and is case-normalized at both store and
  retrieve time (a case-only miss would otherwise be a silent lookup miss).
- **KG → KG.** `kg_supersede(old, new, boundary=...)` is an *atomic pointer*
  from a stale fact to its successor: a point-in-time query at the boundary
  returns *only the new value*. `kg_invalidate` marks a fact no-longer-true
  with a timestamp. These are the KG's self-referential pointers.
- **MemChorus → MemPalace (the backend).** The drawer/KG surfaces are MemPalace
  MCP tools; MemChorus consumes them through `MempalaceMemorySource`
  (`save()` → `add_drawer`, `kg_query`, `kg_subgraph`). The MEMORY surface is
  the *host* agent's own `memory` tool (`MEMORY.md`/`USER.md`), not a MemPalace
  object. MemChorus routes *to* the memory surface; it does not own the
  host's files.

> `See:` the project-record contract — `src/memchorus/project_record.py`
> (structure-only: key namespace §3.1, `location`/`standard` field contracts §2.1–§2.2,
> validation §2.3, defaults + `verified_at` semantics §2.4) and the design spec
> it cites in its module docstring (authoritative).

---

## 3. The pointer model

This is the contract this doc *owns*. Two distinct pointer kinds exist; both
are "pointer-first, body-on-demand."

### 3.1 The locator pointer (north-star #140)

When the right answer is *a location*, recall returns a compact
**locator + topics** pointer — source, `path_or_url`, title, gist, topics —
instead of inlining the whole body. The agent is handed "where to look"; the
full content stays retrievable on demand via `retrieve(key)`. This is the
*why* behind the pointer model: **recalling is an act, not a passive
re-read.** The value of the injected block is that it forces the agent to *go
get* the body, making the recall act a *usage act* — that is why the system
is shaped pointer-first rather than inlining everything.

### 3.2 The collapse-to-retrieve pointer (north-star #217)

Low-signal recall bodies — a tool-output JSON envelope, a bare URL, a
single-token command stub (`score/rank`) — are **collapsed to a one-line
"read it: `retrieve(key=…)`" gist** rather than injected raw. Collapsing
loses no information *because* the body stays fully reachable via
`retrieve(key)`; it only stops paying the prompt-window cost of injecting a
raw dump every turn. The gate is `_should_collapse_low_signal()` in
`src/memchorus/hooks.py`; it never raises (an unknown body degrades to
"don't collapse," so nothing is silently swallowed).

> **The pointer-miss caveat (stated once, here).** *"A collapsed body is a
> pointer, not a promise."* The pointer is only as good as `retrieve(key)` —
> and `retrieve(key)` can return a **hard `None` on a cold-cache miss**. That
> is the entire class of bug tracked by the pointer-integrity triad (§4).
> `See:` north-star pillar 1–3 — [docs/north-star.md](north-star.md) (locator
> + collapse rationale; Issue #144).

---

## 4. The pointer-integrity triad

Three linked defects that *are the failure mode of the pointer model* — a
pointer the gate hands the agent that may not resolve. These are not
"docs" issues; they are the reasons the pointer contract must be stated as a
*promise with a recovery path*, not a comment.

| # | Surface / site | Defect | Why it is the pointer model failing |
|---|---|---|---|
| **[#217](https://github.com/BuboTheWise/MemChorus/issues/217)** | write + read: recall gate | Collapses to a `retrieve(key=…)` pointer. | The *mechanism*. Correct in shape; depends on #221 not missing. |
| **[#219](https://github.com/BuboTheWise/MemChorus/issues/219)** | read: low-signal gate | Misses the bare Python-repr dump (`{'key': …}` single-quoted). | A body that *should* collapse doesn't (or one that shouldn't does) — the gate's recognizer is incomplete, so the pointer is sometimes handed when it shouldn't be. |
| **[#220](https://github.com/BuboTheWise/MemChorus/issues/220)** | write: structured tool output stored via `str(dict)` | Produces a bare Python-repr that downstream recall cannot cleanly recognise. | The *write* side produces a body the *read* side's recognizer never learned — the pointer's input contract is under-specified. |
| **[#221](https://github.com/BuboTheWise/MemChorus/issues/221)** | read: `retrieve(key)` | Returns a **hard `None` on cache miss**, dangling every pointer the gate hands the agent. | The *recovery path* is missing: a pointer can dangle. Sanctioned recovery is `retrieve(key, fallback="live")`. |

The load-bearing invariant, stated at contributor level:

> **A pointer is only safe if the recovery path is guaranteed.** The gate may
> collapse *only* for bodies whose key is resolvable, and `retrieve(key)` must
> have a `fallback="live"` recovery for the cold-cache case — otherwise the
> pointer is a dangling reference and the agent holds a hint it cannot act on.
> The `fallback="live"` path is the sanctioned recovery; a hard `None` on miss
> is the bug #221 tracks.

---

## 5. The routing / validity / temporal contracts (contributor level)

Stated here at the level a contributor needs to implement or extend — **not**
re-spec'd in full. Each link is the sole owner of the full rule.

### 5.1 Write-time routing (the *where*)

- A write is a **routing decision**, not a single `save()`. The three
  surfaces are **not interchangeable**; choosing between them is an explicit,
  testable function `route(content, context) → Surface ∈ {MEMORY, DRAWER, KG}`.
- **Conservative default: DRAWER when in doubt.** A mis-route to KG/MEMORY is
  more visible (it shows up as the wrong kind of fact) than a mis-route to
  DRAWER (it is just one more prose blob) — so the safe fallback is the
  existing drawer default.
- The decision is **visible on the record** as `routing_kind`
  (`"MEMORY" | "KG" | "DRAWER"`), recorded at write time so a reader can tell
  *why* a fact is on its surface.
- `routing_kind` is **orthogonal** to `emission_kind` (how the body was
  encoded, `json` / `str`): a record can be both `emission_kind="json"` and
  `routing_kind="KG"` (a typed fact, JSON-encoded), or `emission_kind="str"`
  and `routing_kind="DRAWER"` (a prose artefact, str-encoded). Two orthogonal
  concerns, two fields.

`See: the routing spec — Issue #226` (https://github.com/BuboTheWise/MemChorus/issues/226) for the
decision rules (the three initial heuristics + boundaries), the default-fallback
rationale, and the `(input-fact → expected-surface)` acceptance examples.
**The full classifier is written once, there — not in this doc.**

### 5.2 Temporal validity (the *when*)

- KG-backed facts can be **validity-scoped** (`valid_from` / `valid_to`),
  **superseded** (`kg_supersede`), or **invalidated** (`kg_invalidate`). A
  rule-of-state routed to KG at write time *has a validity window for free*.
- Recall is **temporally scoped**: an `as_of` anchor (default now) filters
  out superseded / expired facts, and state-anchored entries that are still
  included carry a **staleness annotation** in the formatted block
  (e.g. `(as of 2026-08-30; may have changed)`).

`See: the recall-loop spec — pillar 2 (temporal), Issue #224`
(https://github.com/BuboTheWise/MemChorus/issues/224) for the *exact*
exclude-vs-deprioritise rule, where the temporal filter sits in the
selection pipeline (pre-score / post-score / post-truncation), and the testable
acceptance criterion. **This doc states the contract; the spec owns the rule.**

### 5.3 Task-aware selection & the enrichment sources (the *what*)

- Recall selection is **task-aware**: when an active project resolves, KG
  entity relevance and project-record proximity are added as scoring signals
  *in addition to* text similarity + recency; with no active project it
  degrades to text-similarity + recency, unchanged.
- Recall can extend **laterally** (cross-domain tunnel adjacency), **into
  agent-voice** (the diary), and **into provenance** (event/artifact
  coordination context) — all as **additive, opt-in** signals. The default
  path is unchanged.

`See: the recall-loop spec — pillars 1 & 3, Issues #223 and #225`
(https://github.com/BuboTheWise/MemChorus/issues/223 · https://github.com/BuboTheWise/MemChorus/issues/225) for the
selection algorithm, weighting formula, tie/empty/budget handling, and the
tunnels/diary/events trigger conditions and pipeline slots. The owner of truth
for the whole recall contract is the recall-loop north-star spec (board card
`t_09abc148`).

> **No-contract-twice rule.** A reader should never need to reconcile the
> routing rules (spec #226), the temporal rule (spec #224), and the
> selection/enrichment rule (spec #223/#225) with this doc — this doc states
> *what is promised* and *where the rule lives*, and links out. If you find the
> same rule written in two places, that is a bug to fix by making one of them a
> `See:` link.

---

## 6. How to add a new surface

A new memory surface is a **shape** (a new way a fact can be stored and
retrieved), not a new backend. Before adding one, the surface must pass the
same bar the existing three do, so the pointer model and the routing contract
survive:

1. **Name it** and state **what shape of fact** lives there (one line) — the
   same sentence as §1's table, for your surface. A surface is defined by the
   *shape of the fact it holds*, not by storage.
2. **State what the user sees** (one line) — the observable surface, not the
   mechanism. This is the north-star "what good looks like" line (§3.1/§3.2
   do this for DRAWER/KG).
3. **Give it a stable, addressable key** so it is *reachable by pointer* like
   the other surfaces. A surface that is only writeable (not retrievable by a
   stable key) breaks the "every body is reachable by a key" invariant (§2) —
   the pointer model fails for it.
4. **Wire a recovery path.** If recall can point at it (collapse or locator),
   `retrieve` must have a `fallback` for the miss case — otherwise you have
   added a dangling-pointer class (see the triad, §4).
5. **Teach the write-time router.** Add your surface to `route()` and decide
   — with the conservative-drawer principle — which *shape* of fact routes to
   it. Record the choice on the record as `routing_kind`. If you route to the
   new surface **only on demand** (like KG today), that is a valid, declared
   policy; but it must be *declared*, not implicit.
6. **State its validity/temporal contract** (if it holds state) — validity
   window, supersession, or "timeless." If it holds neither state nor a
   temporal contract, say so explicitly (MEMORY is timeless; DRAWER is
   content; KG is state).
7. **Add it to the routing spec's acceptance table** (spec #226) with one
   `(input-fact → your-surface)` example, and to the recall spec's
   selection/enrichment description (spec #223/#225) if recall reads it.
8. **Write a test** that a body is routed to the new surface *and* that the
   old surface (DRAWER default) still catches the fallback case — the
   conservative-default regression guard (spec #226 AC: "unrecognised fact →
   DRAWER, never silently to the new surface").

Then update the **§1 table** and the **README delta** (both in this repo) with
the new row, so the *public* baseline stays current. A new surface that is not
in §1 is not part of the baseline — it is an implementation detail with no
public contract.

---

## 7. What this doc is not

- **Not the routing classifier.** That is spec #226 (board card t_db50c29c).
- **Not the temporal or selection rule.** That is the recall-loop spec
  (board card t_09abc148; #223/#224/#225).
- **Not a storage layout.** The surfaces are *shapes*, not paths; see §2 for
  how they map onto MemPalace / the host's memory files.
- **Not a version-pinned spec.** It states the *contract and the
  cross-reference structure*; implementation detail may drift as long as the
  pointer-model invariant (§2–§4) and the no-contract-twice rule (§5) hold.
