"""context_loop_guard.py — Detect context compression loop spiraling in long AI sessions.

During extended LLM sessions, context compression can cause the model to repeat
similar content patterns across many rounds until output quality collapses.
This module provides lightweight detection and scoring using only Python stdlib.

Usage example::

    from context_loop_guard import scan_for_loops, estimate_compression_pressure, suggest_intervention

    # Detect repetition in recent messages
    messages = [
        "The solution involves processing the data.",
        "The solution involves processing the data.",  # repeated
        "Different content here",
    ]
    cycles, avg_sim = scan_for_loops(messages)
    print(f"Loops: {cycles}, Similarity: {avg_sim:.2f}")

    # Score context health from session stats
    stats = {
        "turn_count": 25,
        "context_tokens_total": 98000,
        "compressed_rounds": 12,
        "avg_response_len": 480,
    }
    health = estimate_compression_pressure(stats)
    action = suggest_intervention(health)
    print(f"Health: {health}, Action: {action}")
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


# ---------------------------------------------------------------------------
# 1. scan_for_loops — pairwise Jaccard similarity on n-gram sets
# ---------------------------------------------------------------------------

def _make_ngrams(text: str, n: int = 3) -> set[str]:
    """Return a set of character-level n-grams from *text*."""
    text = text.lower()
    return {text[i : i + n] for i in range(max(0, len(text) - n + 1))}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two sets."""
    if not a and not b:
        return 1.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union) if union else 0.0


def scan_for_loops(
    text_window: list[str], threshold: float = 0.8
) -> tuple[int, float]:
    """Detect repetition cycles in a window of message strings.

    Parameters
    ----------
    text_window : list[str]
        Ordered list of recent messages / turns.
    threshold : float
        Minimum Jaccard similarity to count as a "loop match". Default 0.8.

    Returns
    -------
    repeats : int
        Number of pairwise comparisons that exceeded *threshold*.
    avg_similarity : float
        Average Jaccard similarity across all comparable pairs (0-1).
    """
    if len(text_window) < 2:
        return 0, 0.0

    ngram_sets = [_make_ngrams(t, 3) for t in text_window]

    total_sim = 0.0
    count = 0
    repeats = 0

    for i in range(len(text_window)):
        for j in range(i + 1, len(text_window)):
            sim = _jaccard(ngram_sets[i], ngram_sets[j])
            total_sim += sim
            count += 1
            if sim >= threshold:
                repeats += 1

    avg_similarity = total_sim / count if count else 0.0
    return repeats, avg_similarity


# ---------------------------------------------------------------------------
# 2. estimate_compression_pressure — session health scoring (0-100)
# ---------------------------------------------------------------------------

def estimate_compression_pressure(session_stats: dict[str, Any]) -> int:
    """Score context-health from session metadata.

    Parameters
    ----------
    session_stats : dict
        Must contain:
        - ``turn_count``  (int): total conversation turns so far.
        - ``context_tokens_total`` (int): cumulative context token usage.
        - ``compressed_rounds`` (int): rounds that triggered compression.
        - ``avg_response_len`` (int): average output length in tokens/words.

    Returns
    -------
    health_score : int
        0-100 score. ≥70 healthy, 40-69 caution, <40 danger.
    """
    turn_count = session_stats.get("turn_count", 0)
    context_tokens = session_stats.get("context_tokens_total", 0)
    compressed_rounds = session_stats.get("compressed_rounds", 0)
    avg_response_len = session_stats.get("avg_response_len", 0)

    # Start at 100 and deduct on risk factors
    score = 100.0

    # Penalty A: excessive turns (diminishing returns after ~20)
    if turn_count > 5:
        score -= min(30, (turn_count - 5) * 1.5)

    # Penalty B: context token bloat (>50k tokens is heavy)
    if context_tokens > 50_000:
        score -= min(25, ((context_tokens - 50_000) // 10_000) * 5)

    # Penalty C: many compression rounds (direct loop indicator)
    score -= min(30, compressed_rounds * 4)

    # Penalty D: shrinking response lengths signal degradation
    if avg_response_len < 800 and turn_count > 10:
        deficit = max(0, 800 - avg_response_len)
        score -= min(15, deficit / 40)

    # Clamp to [0, 100]
    return int(max(0, min(100, round(score))))


# ---------------------------------------------------------------------------
# 3. suggest_intervention — action text based on health tier
# ---------------------------------------------------------------------------

def suggest_intervention(health_score: int) -> str:
    """Return an intervention recommendation for the given health score.

    Tiers
    -----
    ≥70 : healthy — continue normally.
    40-69 : caution — shorten responses, prefer tool output over prose.
    <40 : danger — extract key context immediately and restart session.
    """
    if health_score >= 70:
        return (
            "Context is healthy. Continue working normally — no intervention needed."
        )
    elif health_score >= 40:
        return (
            "CONTEXT CAUTION: Repetition patterns detected or token pressure rising. "
            "Shorten your responses, prefer tool calls over long explanations, and "
            "avoid re-stating earlier conclusions. Consider extracting key decisions "
            "into a scratch file to free context."
        )
    else:
        return (
            "CONTEXT DANGER: Severe compression pressure or loop repetition detected. "
            "Immediately extract all critical decisions, files, and state into separate "
            "scratch files. Start a fresh session with only the extracted context. Do NOT "
            "continue this session — output quality will continue to degrade."
        )


# ---------------------------------------------------------------------------
# CLI quick-test (-- doctest-style smoke test)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("context_loop_guard.py — Smoke Test")
    print("=" * 60)

    # Test scan_for_loops with repeated input
    loop_window = [
        "The solution involves processing the data correctly.",
        "The solution involves processing the data correctly.",
        "Processing data is the key solution approach.",
        "Completely unrelated content here in this line.",
        "Another different message for comparison.",
    ]
    reps, avg_sim = scan_for_loops(loop_window, threshold=0.8)
    print(f"\n--- Loop Detection ---")
    print(f"Repetition cycles (≥0.8): {reps}")
    print(f"Avg pairwise similarity:   {avg_sim:.3f}")

    # Test with unique input
    unique_window = [
        "Alpha goes first.",
        "Beta takes second place.",
        "Gamma comes third.",
        "Delta rounds out the set.",
    ]
    reps_u, avg_sim_u = scan_for_loops(unique_window, threshold=0.8)
    print(f"\nUnique window — cycles: {reps_u}, avg sim: {avg_sim_u:.3f}")

    # Test estimate_compression_pressure with known inputs
    danger_stats = {
        "turn_count": 25,
        "context_tokens_total": 98_000,
        "compressed_rounds": 12,
        "avg_response_len": 480,
    }
    health_danger = estimate_compression_pressure(danger_stats)
    print(f"\n--- Compression Pressure ---")
    print(f"Danger case stats {danger_stats}")
    print(f"Health score:       {health_danger}")
    assert health_danger < 40, f"Expected danger (<40), got {health_danger}"
    print("✓ Danger case passes (score < 40)")

    healthy_stats = {
        "turn_count": 8,
        "context_tokens_total": 32_000,
        "compressed_rounds": 1,
        "avg_response_len": 620,
    }
    health_healthy = estimate_compression_pressure(healthy_stats)
    print(f"Health case stats {healthy_stats}")
    print(f"Health score:       {health_healthy}")
    assert health_healthy >= 70, f"Expected healthy (≥70), got {health_healthy}"
    print("✓ Healthy case passes (score ≥ 70)")

    # Test suggest_intervention for each tier
    tiers = [85, 55, 25]
    print(f"\n--- Intervention Suggestions ---")
    for s in tiers:
        action = suggest_intervention(s)
        label = "HEALTHY" if s >= 70 else ("CAUTION" if s >= 40 else "DANGER")
        print(f"[{label}] score={s}: {action[:80]}…")

    print("\n" + "=" * 60)
    print("All smoke tests passed.")
    print("=" * 60)
