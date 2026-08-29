# MemChorus Design North Star

> *"At its core MemChorus is meant to give you the most human-like memory so that you can*
> ***real-time make better contextual decisions, not repeat work, be more efficient, and grow.***"

That is the north star. It is **not** a feature list and it is **not** a per-issue spec —
it is the *shared acceptance bar*. Every recall or store change is judged against the pillar it
serves, not in the abstract. If a proposed fix helps no pillar, it is out of scope, however clever.

This document codifies that standard so that triage, implementation, and review can all point at
the same reference (see [`#144`](https://github.com/BuboTheWise/MemChorus/issues/144)).

---

## Reading this: intent vs. observable behavior

Each pillar is stated three ways, because intent alone is too soft to gate on:

- **Intent** — the thing being aimed at, in one line.
- **What "good" looks like** — the *observable* behavior. This is the part you can actually see
  in an injected token block or in `retrieve()` output. Acceptance is written against this line,
  not the intent line.
- **How you know it's off** — the *observable* failure signature. This is what a reviewer greps
  for, or what a reproduction must demonstrate.

The "I already know where that doc lives and which topics it covers, so I'll just go look there"
behavior (a concrete case of [`#140`](https://github.com/BuboTheWise/MemChorus/issues/140)) is a
*symptom-level expression* of pillar 1 and a *cost-saving instance* of pillar 3 — it is not itself
the goal. Locator capture is one instrument the north star calls for; the goal is memory that
*behaves like human recall*.

---

## Pillar 1 — Contextual: real-time decisions

**Intent.** Surface what is relevant to the *current* turn, judged against the current context —
not against stored metadata, recency, or source priority.

**What "good" looks like.**
- The injected block contains *only* entries relevant to the decision being made *right now*. An entry's
  presence is earned by the live query, not by the fact that it was ever saved.
- Irrelevant and low-relevance entries are **suppressed or deprioritized, never allowed to fill the
  block**. A recall floor keeps weak/off-topic entries off the block even when a source is "available."
- When the right answer is *a location* ("I know which doc covers this — go read it"), the system
  answers with a compact **locator + topics** pointer (source, `path_or_url`, title, gist, topics)
  instead of inlining the whole body — the agent is handed "where to look," and the full content stays
  retrievable on demand (`retrieve(key)`).

**How you know it's off.**
- The block is dominated by entries that have nothing to do with the current query (absence of a
  relevance floor — the signature of [`#139`](https://github.com/BuboTheWise/MemChorus/issues/139)).
- The agent is made to pay for a whole long body in *every* turn when the decision only needed a
  pointer to it (the pre-`#140` behavior).
- Rejection / weak entries surface as if they were strong just because a backend returned them.

**Serving issues.** `#139` (relevance floor), `#140` (locator + topics), plus the `#141`/`#142` wave in
so far as each also changes *what* is worth surfacing against the current context.

---

## Pillar 2 — No repeated work

**Intent.** Do not re-present what the agent already holds in working memory, and do not let
near-identical stores both appear.

**What "good" looks like.**
- An entry already injected in the **current session** is *not* re-injected on a later turn *unless it
  has become newly relevant again* — the system carries a cross-turn suppression window, not a
  stateless "everything I have, again."
- Near-duplicate memories do not **co-surface** in a single block. Two entries that fold under a
  similarity gate (Jaccard *or* containment past threshold) are rendered as one, distinct entries
  below that bar are preserved as two.
- Re-saving the same source content reuses the canonical key rather than stacking a second copy, so the
  store itself does not grow a duplicate that later has to be suppressed.

**How you know it's off.**
- The same entry appears on consecutive turns with no change in relevance (the `#141` signature).
- A 15-line entry and the 60-line document it is wholly contained in both show up (the `#142`
  long-doc / subset-duplicate signature).
- `save()` on unchanged content produces a fresh entry keyed by timestamp instead of a stable
  fingerprint, so the store silently accumulates duplicates.

**Serving issues.** `#141` (cross-turn suppression), `#142` (content dedup / containment), the save-path
fingerprint half of `#142`.

---

## Pillar 3 — Efficiency

**Intent.** Bounded token cost per turn, and no database artifacts leaking into the model's view.
The block should read like **notes, not a database**.

**What "good" looks like.**
- Per-turn injected-token cost is **bounded** and *predictable* — not a function of how many backends
  happened to be alive or how much raw text they held.
- No raw `repr(dict)`, raw JSON blobs, `source_id=/key=` metadata, or tool-call `output:` dumps in the
  block. Structured content is rendered as prose the model can act on.
- The **save path stays fast**: a save must not acquire new locks, do new MCP round-trips, or run
  similarity scans against *unrelated* keys, or every write inherits a measurable regression
  (`#136`'s perf half).

**How you know it's off.**
- `source_id=...`, `content={...}` or `key=...` strings appear in the injected block
  (the `#143` dict-repr signature).
- The block's size is driven by backend count / raw text volume rather than relevance (the
  `#139` noise-floor signature, which is *efficiency* at the block level too).
- A single save measurably slows down because it re-ran work that belongs to recall (`#136` perf).

**Serving issues.** `#143` (dict-repr → notes), `#139` (noise floor), `#136` (save-path perf), the
locator half of `#140`, the dedup half of `#142`.

---

## Pillar 4 — Growth

**Intent.** The system should get better *the more it is used*: usage signals feed back into
ranking/thresholds, and saves are **durable** — a save that silently fails, or a learning loop that
never fires, both break growth.

**What "good" looks like.**
- The **record / apply / read** loop of auto-tuning actually has live call sites in normal operation —
  not a function that exists and is never invoked. Usage signals move the thresholds that drive
  pillars 1–3, so *quality improves with use*, not just with operator hand-tuning.
- **Saves are durable and honestly reported.** `save()` returns `True` only when the backend actually
  acknowledged persistence; a backend rejection or MCP failure surfaces as failure (or an explicit,
  logged fallback), never as a quiet `True` backed by a local-cache write the backend never saw.
- Durable state survives the session: a fact recorded now is still recorded next run, not only in
  some in-memory buffer that disappears when the process exits.

**How you know it's off.**
- The auto-tuning loop's record/apply/read joints have **zero call sites** — `#138`, the clearest
  instance of "growth that never happens."
- `save("key","val") → True` but `retrieve("key") → None`, because the MCP call failed and the code
  fell through to a local cache that always returns `True` (`#136`).
- A learning signal is captured but never read back, so every run starts from the same defaults.

**Serving issues.** `#138` (open-loop auto-tuning), `#136` (silent save failure + perf), the
durable-state and learning-loop halves of the north star.

---

## Issue → pillar map

The concrete issues from the `#138`–`#143` wave, mapped to the pillar(s) each one serves and
its shipping status as of v2.0.18 (2026-08-29). Read this as *evidence the north star is testable* —
each issue is an instance of a pillar, not a bespoke bug.

| Issue          | Pillar(s) served                        | Status   | What shipping it changes, observably |
| -------------- | --------------------------------------- | -------- | ------------------------------------ |
| `#136`         | **growth** (+efficiency on the save path) | shipped  | `save()` reports real MCP outcome; save path stops re-doing recall work. |
| `#139`         | **contextual** + **efficiency**          | shipped  | Relevance floor: weak/off-topic entries off the block; block cost decoupled from backend noise. |
| `#143`         | **efficiency**                           | shipped  | No `repr(dict)` / raw JSON / metadata strings in the injected block. |
| `#140`         | **efficiency** + **contextual** (+growth via locator durability) | shipped (v2.0.17) | Locator + topics pointer rendered in place of the full body; on-demand full-body retrieval preserved. |
| `#141`         | **no-repeated-work**                     | shipped (v2.0.16) | Cross-turn suppression window: same entry not re-injected on a later turn unless newly relevant. |
| `#142`         | **no-repeated-work** + **efficiency**    | shipped (v2.0.18) | Content dedup via Jaccard *or* containment + stable save-path fingerprint. |
| `#138`         | **growth**                               | **open** | Auto-tuning record/apply/read joints are wired so the loop actually runs in normal operation. |

> The card's acceptance bar — *"at least one issue filed under it resolves consistent with its
> pillar"* — is satisfied by the shipped members of this wave (`#136`, `#139`, `#140`, `#141`,
> `#142`, `#143`); `#138` is the still-open instance of pillar 4.

---

## Rule: every fix states which pillar it advances

When a change ships, its changelog entry and commit message **must name the pillar(s)** it advances.
This is what makes growth *visible over time* — the `#138`–`#143` wave is already readable that way.
It is not a bureaucratic exercise: it is the observable trace that the north star is being *lived*,
not just *written about*.

Concrete pattern (adapt the existing v2.0.16–v2.0.18 entries to this form):

```
- **<short name> (closes #1NN, advances <pillar(s)>):** <one paragraph of what changed,
  observably, and the test that locks it in>.
```

Reviewers should reject a PR that *claims* to fix a recall/store issue **without mapping it to at
least one pillar** — either the mapping is wrong (it doesn't actually serve what the author said) or
the change is out of scope (it serves none).

---

## Triage checklist (using the `north-star` label)

For a new recall/store issue, before implementation starts:

1. **Which pillar(s) does this serve?** If the answer is "none," it is out of scope for MemChorus
   — close or split.
2. **What observable failure is the acceptance written against?** A line a reproduction, a test,
   or a reviewer can *see*, not a restatement of intent.
3. **Which "how you know it's off" signature does this hit, from the pillar above?** That signature
   is the minimum the fix must clear.
4. **If growth is claimed, does the learning loop actually have live call sites after the fix?**
5. **If efficiency is claimed, does the block size / per-turn cost have a *bound or a test* after
   the fix?** Not an assertion, a measurable.

Label such issues `north-star` so the standard is discoverable from the issue itself.

---

## What this is *not*

- Not a per-feature spec. Each issue has its own spec; this is the bar those specs must clear.
- Not a substitute for a test. A pillar is "advanced" only when the *observable* behavior is
  locked in by a test or a reproduction a reviewer can run.
- Not a reason to skip the version-sync / CI gates. Docs, labels, and triage are additive to
  the release discipline, not a substitute for it.
