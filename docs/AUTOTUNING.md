# MemChorus Auto-Tuning Framework

## Problem

Static thresholds for memory relevance, retention and eviction dont work across diverse agent profiles. Manual tuning fails because usage density varies wildly between code-review-heavy agents and quiet email-triage ones. A score of 0.6 means something entirely different depending on workload context.

## Solution

Lightweight automatic calibration based on empirical observation during real operation. Track what actually happened — hit rates, recall frequency, correction signals from user behavior — then adjust thresholds accordingly without intervention or significant overhead.

## Design Rules

1. **≤ 15μs inline overhead** per memory operation. No blocking I/O during save/recall hot paths.
2. **Graceful degradation** — missing tuning modules just use v1.7.0 defaults. Never block core functionality.
3. **Bounded adjustments** — no parameter swings more than ±40% per calibration cycle. Prevents runaway feedback from one bad session distorting everything permanently.
4. **Observable only** — acts on empirical hit rates and detected correction signals. No theoretical estimates assigned at capture time.

## Four Modules

### 1. HitRateTracker (`hit_rate_tracker.py`)

Adds counters inline with existing `_meta` dict in HermesDefaultMemorySource entries:

```python
"_hit_rate": {
    "total_recalls": 0,       # Times entry appeared in recall results
    "useful_flags": 0,        # Positive signals (no user correction around this block)
    "noise_flags": 0,         # Correction detected ("I already told you that")
    "first_saved_at": ts,
    "last_seen_at": ts
}
```

Called inside `MemoryOrchestrator.full_recall()` after results assembled. Each recalled entry key triggers `record_recallhit(key)` — piggybacks existing file locking paths.

### 2. MistakeDetector (`mistake_detector.py`)

Scans user turn text for three pattern classes at turn-end via hook:

- **correction_repetition** — "I already told you X" / repeating info present in current recall block → entries flagged `noise_flags += 1`
- **correction_outdated** — explicitly correcting a fact ("that changed", "actually its now") → corrected entries get `noise_flags += 1`, replacement saved separately gets `useful_flags += 2`
- **no_correction_positive** — recall content carried forward naturally without pushback → `useful_flags += 1`

String matching against configurable pattern list stored in `_CORRECTION_PATTERNS`. Heuristic only — false positives acceptable since bounded adjustment range protects against over-correction from noisy signals.

### 3. AdaptiveThreshold (`adaptive_threshold.py`)

Adjusts three hardcoded v1.7.0 parameters based on rolling hit-rate statistics:
- `min_relevance_score` (default 0.3, range 0.1–0.8)
- `dedup_similarity_threshold` (default 0.6, range 0.3–0.9)
- `retention_scan_interval_days` (default 14, range 7–60)

**Profile normalization:** Computes hit_ratio = total_recalls / total_saves over CALIBRATION_WINDOW (default 50 entries). If hit_ratio drops below 0.25, relevance threshold nudges upward. If exceeds 0.75, threshold nudges downward. Moving average prevents single-session anomalies from dominating. Hard caps at ±40% per cycle prevent runaway drift.

### 4. CalibrationEngine (`calibration_engine.py`)

Aggregates HitRateTracker + MistakeDetector outputs into concrete parameter adjustments. Runs during existing retention sweep — no new scheduling needed. Writes adjustments back alongside current config. Exposes CLI entry point `memchorus recalibrate` for manual trigger if operator wants to force a calibration cycle outside normal sweep window.

## Configuration File

Per-profile tuning state stored at:
```
~/.hermes/data/memchorus/_tuning/<profile_name>.yaml
```

Example:
```yaml
calibration_window: 50
min_relevance_score: 0.34          # Adjusted from default 0.3 by +13%
dedup_similarity_threshold: 0.56   # Down from 0.6 due to high hit_ratio (0.82)
retention_scan_interval_days: 12   # Tightened because volume profile=high
profile_volume_writes_per_day: 147
last_calibrated_at: "2026-08-12T18:30:00Z"
calibration_count: 3
```

## Integration Points

- `MemoryOrchestrator.__init__()` loads CalibrationEngine if `_tuning/` directory exists. Falls back to static defaults otherwise (zero regression path for existing installs).
- `full_recall()` calls `HitRateTracker.record_recallhit()` on each returned entry key inline.
- `on_turn_end` hook fires MistakeDetector scan of user message text from current turn buffer.
- Retention sweep calls CalibrationEngine once per cycle to compute + apply adjustments.

## Testing Requirements

1. HitRateTracker: verify counters increment correctly on recall, persist across runs, handle missing `_hit_rate` key gracefully (first-run scenario).
2. MistakeDetector: unit tests for each pattern class with real conversation excerpts showing triggers fire correctly plus false positive rate verification below 15% on benign text corpus.
3. AdaptiveThreshold: verify bounded adjustments dont exceed ±40%, profile normalization scales correctly across three simulated volume profiles (low/medium/high), calibration window sliding properly drops oldest entries.
4. CalibrationEngine: integration test verifying full pipeline from hit recording through adjustment computation to config file write-back plus CLI trigger works end-to-end.
