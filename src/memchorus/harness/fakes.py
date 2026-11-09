"""A deterministic, in-memory :class:`memchorus.memory_source.MemorySource`
used by the outcome harness to drive recall *through the real contract*.

The harness does not mock ``MemorySource`` — it subclasses the real ABC and
implements the full contract (``save`` / ``retrieve`` / ``search`` /
``proactive_check`` / ``proactive_save`` / ``delete`` / ``is_available`` /
``get_source_info``). That means the scenarios exercise the same surface a
live backend would, and the only thing that differs between arm A and arm B
is the calibration applied at recall time — exactly the variable issue #271
asks the harness to isolate.

Ranking model (honest, not hand-waved):

Each fact carries a base ``relevance`` in (0, 1]. At recall the source scores
a fact as ``relevance * boost[key]`` where ``boost`` is:

* ``{}`` in arm A (baseline) — every boost is 1.0, and the recall floor is
  the static v1.7.0 default (0.3).
* the real :class:`memchorus.calibration_engine.CalibrationEngine` mapping in
  arm B — ``boost_factor_for_key(key)``, which is ``>= 2.0`` for proven-useful
  facts and ``<= 0.6`` for confirmed-noise facts (read from the real
  :class:`memchorus.hit_rate_tracker.HitRateTracker`).

Two calibration mechanisms are expressed at recall time, mirroring the real
stack:

1. **Recall floor** — facts whose *base* relevance is below the arm's
   ``min_relevance_score`` are dropped before ranking (the recall gate).
   The calibration arm's floor is the *calibrated* value (higher than the
   static default when the hit ratio is low), which is what lets a useful
   fact that would otherwise sit just under the static floor clear it after
   a boost — and drops confirmed-noise facts.

2. **Stale demotion** — a fact whose ``valid_to`` is in the past at ``as_of`` is
   demoted by a 0.5× multiplier (the recall-time expression of the R4 veto in
   :mod:`memchorus.surface_routing`). 0.5× — not a hard 0.1× — is deliberate:
   it keeps a stale-but-relevant fact *competitive* in the baseline arm (so the
   harness can show the baseline surfacing it), while a confirmed-noise
   calibration boost (×0.5) on the same fact compounds it below the recall
   floor in the calibration arm — which is exactly the A/B delta issue #271
   asks for (calibration pushing stale facts out of the top-k window).

Both mechanisms are *inputs* the harness passes in (``boost`` / ``floor`` /
``as_of``), not read from the engine inside the fake — so the runner stays
the single place that touches the real calibration stack, and the fake
stays a pure function of its inputs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from memchorus.memory_source import MemorySource


@dataclass
class Fact:
    """One memory entry the source serves.

    ``relevance`` is the *un-calibrated* relevance of the fact to the default
    query in (0, 1]. ``kind`` is metadata only — the harness uses it to label
    which fact is the "target" for assertions; it does NOT leak into ranking.
    ``valid_to`` is an optional ISO datetime; a fact whose ``valid_to`` is in
    the past at ``as_of`` is stale and is demoted by the stale-veto multiplier.
    """

    key: str
    content: str
    relevance: float = 0.5
    kind: str = "signal"  # "signal" | "noise"
    valid_to: Optional[str] = None
    hits: int = field(default=0, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "score": self.relevance,
            "source": "harness-fake",
            "channel": "semantic",
        }


class FakeMemorySource(MemorySource):
    """Concrete :class:`MemorySource` — implements the full ABC contract.

    ``search`` accepts three *harness extension* keyword arguments (``boost``,
    ``floor``, ``as_of``) in addition to the ABC's ``(query, limit)``. These
    are calibration inputs the runner supplies; they do not change the ABC
    signature and are optional (defaulting to the arm-A values: no boost,
    static floor, current time).
    """

    def __init__(
        self,
        name: str = "harness",
        config: Optional[Dict[str, Any]] = None,
        facts: Optional[List[Fact]] = None,
    ) -> None:
        # MemorySource.__init__ is an abstract no-op (the ABC declares the
        # signature but assigns nothing), so we set our own attributes here.
        self.name = name
        self.config = config or {}
        self._facts: List[Fact] = list(facts or [])
        self._index: Dict[str, Fact] = {f.key: f for f in self._facts}
        self._available = True
        self._last_search_ms: float = 0.0
        # Deterministic recall-path cost proxy: number of facts that cleared
        # the recall floor and were actually scored in the last search.
        self._work_units_last_search: int = 0

    # -- MemorySource contract --------------------------------------------

    def save(self, key: str, value: Any) -> bool:
        if key in self._index:
            self._index[key].content = value if isinstance(value, str) else str(value)
        else:
            self._index[key] = Fact(key=key, content=str(value), relevance=0.5)
            self._facts.append(self._index[key])
        return True

    def retrieve(self, key: str) -> Optional[Any]:
        fact = self._index.get(key)
        return fact.content if fact else None

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        boost: Optional[Dict[str, float]] = None,
        floor: float = 0.0,
        as_of: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Rank facts by their **effective** score and return the top *limit*.

        A fact's effective score is ``relevance * boost[key]`` with a stale
        veto demotion (0.1x) applied when its ``valid_to`` is in the past at
        ``as_of``. Recall gating is applied on that *effective* score — a fact
        clears the recall floor iff ``effective_score >= floor`` — which is the
        honest model of the live recall gate (a useful fact cleared *because*
        a boost lifted it; a confirmed-noise fact dropped *because* its boost
        pulled it under the gate). The floor is not a hand-tuned cut on raw
        base relevance.

        Parameters
        ----------
        boost:
            Per-key multiplier (arm B passes the real
            :class:`CalibrationEngine` mapping; arm A passes ``{}`` → every
            boost 1.0).
        floor:
            The arm's recall ``min_relevance_score`` (0.3 default, held
            constant across arms so the A/B delta isolates the boost mapping).
        as_of:
            Reference time for the stale-veto (defaults to now).
        """
        t0 = time.perf_counter()
        as_of = as_of or datetime.now(tz=None)
        boost = boost or {}
        floor = max(0.0, float(floor))

        # Score every fact on its effective value, then gate on the floor.
        scored: List[tuple] = []
        for f in self._facts:
            s = self._effective_score(f, boost, as_of)
            if s >= floor:
                scored.append((s, f))
        # Deterministic work proxy: facts that cleared the (post-boost) recall
        # floor and were actually scored in this search.
        self._work_units_last_search = len(scored)

        # Rank the survivors by effective score (stable: higher first).
        scored.sort(key=lambda sf: sf[0], reverse=True)
        out = [f.to_dict() for _, f in scored[: max(0, int(limit))] if f.key]
        for d in out:
            self._index[d["key"]].hits += 1
        self._last_search_ms = (time.perf_counter() - t0) * 1000.0
        return out

    def _effective_score(
        self,
        fact: Fact,
        boost: Optional[Dict[str, float]],
        as_of: datetime,
    ) -> float:
        mult = 1.0
        if boost and fact.key in boost:
            mult = max(0.0, float(boost[fact.key]))
        if fact.valid_to:
            try:
                vt = datetime.fromisoformat(fact.valid_to.replace("Z", "+00:00"))
                if vt.tzinfo is None:
                    vt = vt.replace(tzinfo=as_of.tzinfo or None)
                if vt < as_of:
                    mult *= 0.5  # stale demotion (recall-time R4 veto, see module doc)
            except (ValueError, TypeError):
                pass
        return fact.relevance * mult

    @property
    def is_available(self) -> bool:
        return self._available

    def get_source_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": "harness-fake",
            "fact_count": len(self._facts),
            "available": self._available,
        }

    def proactive_check(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        top = self.search(context.get("query", "") if context else "", limit=3)
        return {"recommendations": top, "source": self.name}

    def proactive_save(
        self, key: str, value: Any, context: Optional[Dict[str, Any]] = None
    ) -> bool:
        return self.save(key, value)

    def delete(self, key: str) -> bool:
        if key in self._index:
            del self._index[key]
            self._facts = [f for f in self._facts if f.key != key]
            return True
        return False

    # -- harness conveniences ---------------------------------------------

    @property
    def last_search_ms(self) -> float:
        return self._last_search_ms

    @property
    def work_units_last_search(self) -> int:
        """Facts that cleared the recall floor and were scored in the last
        search — a deterministic recall-path cost proxy (not wall-clock)."""
        return self._work_units_last_search

    @property
    def facts(self) -> List[Fact]:
        return list(self._facts)


__all__ = ["Fact", "FakeMemorySource"]
