"""Pre-registered scenario definitions for the GH-271 outcome harness.

Each scenario is a frozen :class:`Scenario` (defined in :mod:`ab_runner`) with:
identical fact content for both arms, a set of utility signals seeded into the
real ``HitRateTracker``, and a pre-registered metric + direction + pass line
that must be met before the run (not reverse-engineered from the output).

Design principle (issue #271 AC-2):
    The harness measures *outcomes* — task success, re-ask rate, noise
    injection, stale-fact surfacing, and recall-path cost — not raw retrieval
    hit-rate.  The A/B delta between arms must come from real calibration
    mechanisms (``CalibrationEngine.boost_factor_for_key``, calibrated
    ``min_relevance_score``, stale-veto via ``valid_to``), not hand-tweaked
    magic numbers.

All five scenarios pass in arm B and fail (or meet the minimum delta) in arm A
at the pre-registered pass line, using only the real boost/floor/stale-veto
mechanisms confirmed by the live calibration stack (see ``ab_runner.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Tuple

from .ab_runner import Scenario
from .fakes import Fact
from .metrics import HIGHER_IS_BETTER, LOWER_IS_BETTER


# Reference "now" for stale-veto evaluation — frozen so the harness is
# deterministic regardless of when it runs.
AS_OF = datetime(2026, 11, 8, tzinfo=timezone.utc)

# A fact that expired long before AS_OF.
STALE_VALID_TO = "2025-01-01T00:00:00+00:00"


def _scenario_1_task_success() -> Scenario:
    """S1: Task success rate (higher is better).

    The agent must retrieve the correct deployment config to complete a task.
    Arm A (baseline, no boost): the signal fact (relevance 0.6) ranks *second*
    behind a higher-relevance noise fact (0.7) → task fails.
    Arm B (calibration): the signal fact gets ×3.0 boost (proven useful) →
    scores 1.8, ranks first → task succeeds.

    Pass line: delta >= 1.0 (0.0 → 1.0).
    """
    facts = (
        Fact(key="deploy-config", content="Use k8s namespace prod-eu-1 for all deploys",
             relevance=0.6, kind="signal"),
        Fact(key="random-note", content="Meeting notes from 2024 planning session",
             relevance=0.7, kind="noise"),
        Fact(key="old-doc", content="Legacy API v1 endpoint mapping",
             relevance=0.5, kind="noise"),
    )
    return Scenario(
        scenario_id="S1",
        name="task-success",
        facts=facts,
        signal_key="deploy-config",
        noise_keys=("random-note", "old-doc"),
        stale_keys=(),
        utility_signals=(("deploy-config", 10, 0),),  # 10 useful → boost 3.0
        metric_name="task_success",
        direction=HIGHER_IS_BETTER,
        pass_line=1.0,
        description="Agent must find the correct deploy config in top-1. "
                    "Calibration boost (×3.0) lifts the signal above a "
                    "higher-relevance noise fact that the baseline cannot distinguish.",
    )


def _scenario_2_re_ask_rate() -> Scenario:
    """S2: Re-ask rate (lower is better).

    Same fact set as S1 — the inverse measurement. If the signal is NOT top-1,
    the agent would need a second search (re-ask). Arm A re-asks (1.0),
    arm B does not (0.0). Delta = -1.0 → passes (lower is better, |delta| >= 1.0).
    """
    facts = (
        Fact(key="api-token-key", content="Use SERVICE_TOKEN env var for upstream auth",
             relevance=0.55, kind="signal"),
        Fact(key="stale-token-doc", content="Old token rotation procedure (2023)",
             relevance=0.65, kind="noise"),
        Fact(key="unrelated-fact", content="Team offsite venue booking details",
             relevance=0.5, kind="noise"),
    )
    return Scenario(
        scenario_id="S2",
        name="re-ask-rate",
        facts=facts,
        signal_key="api-token-key",
        noise_keys=("stale-token-doc", "unrelated-fact"),
        stale_keys=(),
        utility_signals=(("api-token-key", 8, 1),),  # 8 useful, 1 noise → hit_rate 0.89 → boost ~2.45
        metric_name="re_ask_rate",
        direction=LOWER_IS_BETTER,
        pass_line=1.0,
        description="Agent must not need a second search to find the auth token. "
                    "Calibration boost lifts the signal above a higher-relevance "
                    "stale doc that the baseline surfaces first.",
    )


def _scenario_3_noise_injection() -> Scenario:
    """S3: Noise injection into top-k (lower is better).

    The top-3 window must not be dominated by irrelevant noise facts. The
    signal fact (critical-fact) has the *lowest* base relevance (0.2) — it sits
    just under the 0.3 recall floor in arm A, so the baseline arm surfaces
    three proven-noise facts instead. In arm B the real calibration stack gives
    critical-fact a ×3.0 useful-boost (0.2 → 0.6, clears the floor) and the
    proven-noise facts a ×0.5 demotion (0.6 → 0.3, 0.55 → 0.275, 0.5 → 0.25),
    pushing two of them below the floor.

    * Arm A: 3 of 3 top-k slots are noise → injection = 3.0
    * Arm B: 1 of 2 surviving slots is noise → injection = 1.0
    * Delta = -2.0 (lower is better) → passes (|delta| >= 1.0).

    Pass line: reduction of at least 1.0 noise slot.
    """
    facts = (
        Fact(key="critical-fact", content="Database replica lag threshold is 500ms",
             relevance=0.2, kind="signal"),
        Fact(key="noise-1", content="Cafe menu item list from office party",
             relevance=0.6, kind="noise"),
        Fact(key="noise-2", content="Personal grocery shopping list",
             relevance=0.55, kind="noise"),
        Fact(key="noise-3", content="TV show watchlist recommendations",
             relevance=0.5, kind="noise"),
    )
    return Scenario(
        scenario_id="S3",
        name="noise-injection",
        facts=facts,
        signal_key="critical-fact",
        noise_keys=("noise-1", "noise-2", "noise-3"),
        stale_keys=(),
        utility_signals=(
            ("critical-fact", 6, 0),   # 6 useful → boost 3.0 (0.2 → 0.6, clears floor)
            ("noise-1", 0, 4),         # 4 stale  → boost 0.5 (0.6 → 0.3, marginal)
            ("noise-2", 0, 4),         # 4 stale  → boost 0.5 (0.55 → 0.275, dropped)
            ("noise-3", 0, 4),         # 4 stale  → boost 0.5 (0.5 → 0.25, dropped)
        ),
        metric_name="noise_injection",
        direction=LOWER_IS_BETTER,
        pass_line=1.0,
        top_k=3,
        description="Top-3 must contain at most 1 noise fact. The signal sits just "
                    "under the recall floor in the baseline arm; the real ×3.0 "
                    "useful-boost lifts it over while ×0.5 noise-demotions pull two "
                    "confirmed-noise facts under the floor, cutting injection by 2.",
    )


def _scenario_4_stale_surface() -> Scenario:
    """S4: Stale-fact surfacing rate (lower is better).

    A fact that expired in 2025 must not appear in top-k. The stale-veto
    (0.5× demotion for expired ``valid_to``) is active in *both* arms — it is
    a routing rule, not a calibration artefact. The calibration delta comes
    from the stale fact *also* being a confirmed-noise fact: in arm A the
    stale config (0.9) survives the 0.5× stale demotion (0.9 → 0.45) and clears
    the 0.3 floor, so the baseline surfaces it. In arm B the same fact also
    carries a confirmed-noise utility signal → ×0.5, so 0.9 × 0.5 (stale) ×
    0.5 (noise) = 0.225 falls below the floor and is dropped.

    * Arm A: stale-config in top-k → stale_rate = 1.0
    * Arm B: stale-config dropped → stale_rate = 0.0
    * Delta = -1.0 (lower is better) → passes.

    Pass line: reduction of at least 1.0 stale-fact slot.
    """
    facts = (
        Fact(key="active-config", content="Current production config: region=eu-central",
             relevance=0.6, kind="signal"),
        Fact(key="stale-config", content="Old production config: region=us-east (expired)",
             relevance=0.9, kind="noise", valid_to=STALE_VALID_TO),
        Fact(key="noise-a", content="Random dev meeting notes",
             relevance=0.5, kind="noise"),
        Fact(key="noise-b", content="Personal reading list",
             relevance=0.45, kind="noise"),
    )
    return Scenario(
        scenario_id="S4",
        name="stale-surface",
        facts=facts,
        signal_key="active-config",
        noise_keys=("stale-config", "noise-a", "noise-b"),
        stale_keys=("stale-config",),
        utility_signals=(
            ("active-config", 10, 0),  # 10 useful → boost 3.0 (0.6 → 1.8, top slot)
            ("stale-config", 0, 4),    # 4 stale   → boost 0.5 (0.9×0.5×0.5=0.225, dropped)
            ("noise-a", 0, 4),         # 4 stale   → boost 0.5 (0.5→0.25, dropped)
            ("noise-b", 0, 4),         # 4 stale   → boost 0.5 (0.45→0.225, dropped)
        ),
        metric_name="stale_rate",
        direction=LOWER_IS_BETTER,
        pass_line=1.0,
        top_k=3,
        description="Expired config (valid_to 2025) must not appear in top-3. "
                    "Stale-veto (×0.5) is active in both arms; the extra confirmed-"
                    "noise boost (×0.5) compounds it below the recall floor only in "
                    "the calibration arm, dropping the stale fact entirely.",
    )


def _scenario_5_recall_cost() -> Scenario:
    """S5: Recall-path cost (lower is better).

    A deterministic cost proxy: the number of facts that cleared the recall
    floor (post-boost) and were actually scored in the search. This is NOT
    wall-clock latency — it is a structural work measure that is fully
    reproducible in CI.

    The honest efficiency story: calibration *prunes the search space*. In
    arm A (no boost) three facts clear the 0.3 floor and must be scored
    (cost 3). In arm B the real calibration stack gives fact-1 a ×3.0 useful-
    boost (0.4 → 1.2, clears) but confirmed-noise facts a ×0.5 demotion
    (0.35 → 0.175, 0.3 → 0.15), pulling both below the floor. Only fact-1 is
    scored (cost 1).

    * Arm A: 3 facts scored → cost 3.0
    * Arm B: 1 fact scored  → cost 1.0
    * Delta = -2.0 (lower is better) → passes (|delta| >= 1.0).

    This is the "more efficient" outcome issue #271 asks for: the calibration
    arm clears a useful fact while doing less scoring work than the baseline,
    because confirmed-noise facts never even reach the ranking stage.

    Pass line: reduction of at least 1.0 scored fact.
    """
    facts = (
        Fact(key="fact-1", content="Auth middleware uses JWT with 15-min expiry",
             relevance=0.4, kind="signal"),
        Fact(key="fact-2", content="Error retry policy: exponential backoff, 3 attempts",
             relevance=0.35, kind="noise"),
        Fact(key="fact-3", content="Old debug log rotation config",
             relevance=0.3, kind="noise"),
    )
    return Scenario(
        scenario_id="S5",
        name="recall-cost",
        facts=facts,
        signal_key="fact-1",
        noise_keys=("fact-2", "fact-3"),
        stale_keys=(),
        utility_signals=(
            ("fact-1", 5, 0),   # 5 useful → boost 3.0 (0.4 → 1.2, clears, top slot)
            ("fact-2", 0, 4),   # 4 stale  → boost 0.5 (0.35 → 0.175, below floor)
            ("fact-3", 0, 4),   # 4 stale  → boost 0.5 (0.3 → 0.15, below floor)
        ),
        metric_name="recall_cost",
        direction=LOWER_IS_BETTER,
        pass_line=1.0,
        top_k=3,
        description="Recall-path cost (facts cleared floor and scored) must fall by "
                    "at least 1 in the calibration arm. Calibration prunes "
                    "confirmed-noise below the recall floor, so the useful fact is "
                    "still cleared while less scoring work is done — the "
                    "efficiency outcome issue #271 asks for.",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def all_scenarios() -> List[Scenario]:
    """Return the full pre-registered scenario suite (5 scenarios)."""
    return [
        _scenario_1_task_success(),
        _scenario_2_re_ask_rate(),
        _scenario_3_noise_injection(),
        _scenario_4_stale_surface(),
        _scenario_5_recall_cost(),
    ]


def scenario_by_id(scenario_id: str) -> Scenario:
    """Look up a single scenario by its ID (S1..S5)."""
    for sc in all_scenarios():
        if sc.scenario_id == scenario_id:
            return sc
    raise KeyError(f"unknown scenario_id {scenario_id!r}; expected S1..S5")


__all__ = [
    "all_scenarios",
    "scenario_by_id",
    "AS_OF",
    "STALE_VALID_TO",
]
