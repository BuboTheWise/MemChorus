#!/usr/bin/env python3
"""
test_recall_battery.py — IMPL #168: numeric recall quality BATTERY.

Extends (does not replace) ``test_recall_quality.py``. That existing suite pins
structural properties (result-count >= 1, key uniqueness, score bounds and
sortedness, provenance fields, non-empty content) on a small 7-query corpus.
What #168 adds is the missing NUMERIC layer on a committed fixture corpus:

  * recall@5, recall@10, MRR, precision@5 (averaged across 12 bounded queries)
  * PINNED FLOORS for every metric — a >5% absolute-relative drop below a
    pinned floor FAILS the run (not warns); a drop of <5% below a floor
    raises RecallRegressionWarning (visible, non-fatal)
  * content-level dedup: a byte-identical text stored under two keys must
    appear at most once in the top-k set
  * source scoping: records from a disabled source contribute nothing;
    records from an enabled second source are reachable and attributed
  * determinism: two consecutive runs produce identical top-k rankings
  * runtime gate: the whole battery completes in < 30 s

The pipeline under test is the live one: ``MemoryOrchestrator.search()`` →
per-source ``HermesDefaultMemorySource.search()`` (lexicial term-match +
frequency bonus) → ``RelevanceScorer.score_and_rank()`` (unigram F1 quality
+ half-life recency + source prior) → provenance penalty → content-level
dedup. No LLM is in the loop; timestamps are backdated from a fixed per-record
``age_days`` so recency ordering is deterministic on every platform.

Run standalone:  pytest tests/test_recall_battery.py -m "" -q
CI flag form:    pytest --recall-battery -x --tb=short
"""

import json
import os
import sys
import time
import warnings

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from memchorus.orchestrator import MemoryOrchestrator
from memchorus.hermes_memory_source import HermesDefaultMemorySource

FIXTURE_PATH = os.path.join(_HERE, 'data', 'recall_fixture_corpus.json')

# IMPL #168 — mark every test in this module so the (opt-in) --recall-battery
# flag and selective runs can isolate the battery from the fast unit path.
pytestmark = pytest.mark.recall_battery


# ---------------------------------------------------------------------------
# Pinned floors (IMPL #168)
#
# These are the regression gate: a >5% drop below a pinned floor FAILS the
# build (pytest.fail), a 0–5% drop raises a visible warning instead.  Floors
# are pinned below the calibrated baseline with margin so normal float
# drift across Python 3.11/3.12 and OSes cannot flake them, while a real
# recall regression (scoring change, dedup change, threshold change) trips.
# ---------------------------------------------------------------------------

class RecallRegressionWarning(UserWarning):
    """Metric below its pinned floor but within the 5% warning tolerance."""


PINNED_FLOORS = {
    'recall5':      0.80,
    'recall10':     0.85,
    'mrr':          0.75,
    'precision5':   0.25,
}

# >5% relative drop below a pinned floor fails; (0, 5%] warns.
REGRESSION_TOLERANCE = 0.05
BATTERY_BUDGET_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Corpus seeding (committed fixture — never generated at runtime)
# ---------------------------------------------------------------------------

def _backdate(file_path: str, age_days: float, now: float) -> None:
    t = now - age_days * 86400.0
    os.utime(file_path, (t, t))


def _resolve_record_path(memory_dir: str, key: str) -> str:
    for p in (os.path.join(memory_dir, f"{key}.json"),
              os.path.join(memory_dir, f"{HermesDefaultMemorySource._safe_key(key)}.json")):
        if os.path.exists(p):
            return p
    return os.path.join(memory_dir, f"{HermesDefaultMemorySource._safe_key(key)}.json")


def _seed_corpus(dir_path: str) -> None:
    """Seed *dir_path* from the committed fixture with deterministic ages."""
    with open(FIXTURE_PATH) as f:
        data = json.load(f)

    now = time.time()
    seed_src = HermesDefaultMemorySource(
        name='hermes_default', config={'memory_dir': dir_path})

    for rec in data['corpus']['signal']:
        seed_src.save(rec['key'], {
            'text': rec['text'],
            'categories': rec.get('categories', ['CONTEXT']),
        })
        _backdate(_resolve_record_path(dir_path, rec['key']), rec['age_days'], now)

    pair = data['corpus']['dedup_pair']
    for key, age in zip(pair['keys'], pair['age_days']):
        seed_src.save(key, {'text': pair['text'], 'categories': ['NOTE']})
        _backdate(_resolve_record_path(dir_path, key), age, now)

    for rec in data['corpus']['noise']:
        seed_src.save(rec['key'], {'text': rec['text'], 'categories': ['CONTEXT']})
        _backdate(_resolve_record_path(dir_path, rec['key']), rec['age_days'], now)

    # The source-scoping probe corpus lives in dedicated sibling dirs (see
    # test module below).
    scope_data = data['corpus'].get('scope_pair')
    if scope_data:
        for side, payload in (('a', scope_data['side_a']), ('b', scope_data['side_b'])):
            d = os.path.join(dir_path, f'scope_{side}')
            os.makedirs(d, exist_ok=True)
            side_src = HermesDefaultMemorySource(
                name='scope_%s' % side, config={'memory_dir': d})
            for rec in payload:
                side_src.save(rec['key'], {
                    'text': rec['text'],
                    'categories': rec.get('categories', ['CONTEXT']),
                })
                _backdate(_resolve_record_path(d, rec['key']), rec.get('age_days', 10), now)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def battery_corpus_dir(tmp_path_factory):
    d = str(tmp_path_factory.mktemp('battery_memories'))
    os.makedirs(d, exist_ok=True)
    _seed_corpus(d)
    return d


def _make_orchestrator(memory_dir: str) -> MemoryOrchestrator:
    orch = MemoryOrchestrator(config={
        'memory_dir': memory_dir,
        'hermes_default_config': {
            'memory_dir': memory_dir,
            'min_recall_score': 0.1,
        },
        'enforce_on_read': False,
        'enforce_on_write': False,
    })
    # mempalace startup noise (GAP045) + live state.db coupling (GAP057) —
    # the battery must stay hermetic on both CI and dev machines.
    for name in ('mempalace', 'session_history'):
        if name in orch.memory_sources:
            orch.disable_source(name)
    return orch


@pytest.fixture(scope='module')
def battery_orchestrator(battery_corpus_dir):
    return _make_orchestrator(battery_corpus_dir)


def _load_battery_data():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Metric computation (pure, deterministic)
# ---------------------------------------------------------------------------

def _content_text(result) -> str:
    c = result.get('content')
    if isinstance(c, dict):
        t = c.get('text', '')
        return ' '.join(str(t).lower().split()) if t else json.dumps(c)
    return ' '.join(str(c).lower().split())


def _run_queries(orch, queries) -> dict:
    """Execute every battery query once (limit=10) — returns text → results."""
    out = {}
    for q in queries:
        out[q['text']] = orch.search(q['text'], limit=10)
    return out


def _compute_metrics(queries: list, result_map: dict) -> dict:
    """Averaged recall@5, recall@10, MRR, precision@5 across all queries.

    Per-query: expected = expected_keys (ground truth from the fixture).
    MRR = 1/rank of the first expected key in the ranking (0.0 if absent).
    """
    r5, r10, mrr_sums, p5 = [], [], [], []
    per_query = []
    for q in queries:
        expected = set(q['expected_keys'])
        results = result_map.get(q['text'], [])
        keys_top10 = [r.get('key') for r in results[:10]]
        keys_top5 = keys_top10[:5]

        hit5 = expected.intersection(keys_top5)
        hit10 = expected.intersection(keys_top10)

        r5v = len(hit5) / len(expected)
        r10v = len(hit10) / len(expected)
        r5.append(r5v)
        r10.append(r10v)
        p5.append(len(hit5) / 5.0)

        first_rank = None
        for i, k in enumerate(keys_top10, start=1):
            if k in expected:
                first_rank = i
                break
        mrr_v = 1.0 / first_rank if first_rank else 0.0
        mrr_sums.append(mrr_v)
        per_query.append({
            'id': q['id'], 'text': q['text'],
            'expected': sorted(expected), 'top10': keys_top10,
            'recall5': round(r5v, 4), 'recall10': round(r10v, 4),
            'mrr': round(mrr_v, 4), 'precision5': round(len(hit5) / 5.0, 4),
        })

    n = max(len(queries), 1)
    return {
        'recall5': sum(r5) / n,
        'recall10': sum(r10) / n,
        'mrr': sum(mrr_sums) / n,
        'precision5': sum(p5) / n,
        'per_query': per_query,
    }


def _format_report(per_query: list) -> str:
    rows = []
    for pq in per_query:
        rows.append(
            "  %-10s r@5=%.2f  r@10=%.2f  mrr=%.2f  p@5=%.2f  expected=%s  top10=%s"
            % (pq['id'], pq['recall5'], pq['recall10'], pq['mrr'], pq['precision5'],
               pq['expected'], pq['top10'])
        )
    return '\n'.join(rows)


def _gate_metric(name: str, measured: float) -> str:
    """Apply the pinned floor. Returns a report line; raises via pytest.fail
    when the drop beyond the 5% tolerance, warns within it."""
    floor = PINNED_FLOORS[name]
    hard_floor = floor * (1.0 - REGRESSION_TOLERANCE)
    if measured >= floor:
        return '  %-12s %.4f  (pinned floor %.4f) PASS' % (name, measured, floor)
    if measured >= hard_floor:
        warnings.warn(
            f"recall battery: {name}={measured:.4f} is below pinned floor "
            f"{floor:.4f} but within the {REGRESSION_TOLERANCE:.0%} tolerance",
            RecallRegressionWarning,
        )
        return '  %-12s %.4f  (pinned floor %.4f) WARN — below floor, within tolerance' % (name, measured, floor)
    pytest.fail(
        f"RECALL GATE FAILED: {name}={measured:.4f} dropped more than "
        f"{REGRESSION_TOLERANCE:.0%} below the pinned floor {floor:.4f} "
        f"(hard floor {hard_floor:.4f}). This is a regression — investigate "
        f"scoring/dedup/threshold changes before shipping."
    )


# ---------------------------------------------------------------------------
# Battery — numeric metric floors
# ---------------------------------------------------------------------------

class TestRecallBatteryMetrics:
    """recall@5 / recall@10 / MRR / precision@5 vs pinned floors."""

    def test_metric_floors(self, battery_orchestrator):
        data = _load_battery_data()
        queries = data['queries']
        assert len(queries) >= 10, "battery must have 10+ bounded queries"

        result_map = _run_queries(battery_orchestrator, queries)
        metrics = _compute_metrics(queries, result_map)

        report_lines = [
            _gate_metric(name, metrics[name]) for name in
            ('recall5', 'recall10', 'mrr', 'precision5')
        ]
        # Surface the full report in any failure context / -s output.
        print("\n=== recall battery report ===")
        print('\n'.join(report_lines))
        print("per-query detail:")
        print(_format_report(metrics['per_query']))
        print("=== end report ===")

    def test_no_query_empty_and_contract_fields(self, battery_orchestrator):
        """Every battery query returns a non-empty, contract-shaped result set."""
        data = _load_battery_data()
        failures = []
        for q in data['queries']:
            results = battery_orchestrator.search(q['text'], limit=10)
            if not results:
                failures.append(f"  {q['id']}: '{q['text']}' → 0 results")
                continue
            for r in results:
                if not isinstance(r.get('score'), (int, float)):
                    failures.append(f"  {q['id']}: key={r.get('key')} missing numeric score")
                if 'source' not in r:
                    failures.append(f"  {q['id']}: key={r.get('key')} missing source")
        if failures:
            data = _load_battery_data()
            pytest.fail("Battery contract violations:\n" + '\n'.join(failures))

    def test_rankings_sorted_desc(self, battery_orchestrator):
        data = _load_battery_data()
        for q in data['queries']:
            results = battery_orchestrator.search(q['text'], limit=10)
            if len(results) < 2:
                continue
            scores = [r['score'] for r in results]
            assert scores == sorted(scores, reverse=True), (
                f"ranking not sorted desc for {q['id']}: {scores}"
            )


# ---------------------------------------------------------------------------
# Battery — content-level dedup
# ---------------------------------------------------------------------------

class TestRecallBatteryDedup:
    """Duplicate content appears at most once in the top-k set."""

    def test_identical_content_at_most_once_in_topk(self, battery_orchestrator):
        data = _load_battery_data()
        pair = data['corpus']['dedup_pair']
        results = battery_orchestrator.search(
            "identical dedup pair verbatim stored keys", limit=10)

        pair_keys = set(pair['keys'])
        pair_hits = [r for r in results if r.get('key') in pair_keys]
        assert pair_hits, (
            "dedup pair should be recalled at all — neither key returned: "
            f"top10={[r.get('key') for r in results]}"
        )
        canonical = ' '.join(str(pair['text']).lower().split())
        occurrences = 0
        for r in pair_hits:
            if _content_text(r).strip() == canonical:
                occurrences += 1
        assert occurrences <= 1, (
            f"byte-identical dedup-pair text appeared {occurrences} times in "
            f"top-10 (keys {[r.get('key') for r in pair_hits]}) — content-level "
            f"dedup must collapse it to a single result"
        )

    def test_no_duplicate_keys_any_query(self, battery_orchestrator):
        data = _load_battery_data()
        for q in data['queries']:
            results = battery_orchestrator.search(q['text'], limit=10)
            keys = [r.get('key') for r in results]
            assert len(keys) == len(set(keys)), f"duplicate keys for {q['id']}: {keys}"


# ---------------------------------------------------------------------------
# Battery — source scoping
# ---------------------------------------------------------------------------

class TestRecallBatterySourceScoping:
    """A second registered source contributes when enabled and stops contributing
    when disabled (source attribution is intact in both states)."""

    @pytest.fixture(scope='class')
    def scope_dirs(self, tmp_path_factory):
        a = str(tmp_path_factory.mktemp('scope_a'))
        b = str(tmp_path_factory.mktemp('scope_b'))
        src = HermesDefaultMemorySource
        sa = src(name='scope_a', config={'memory_dir': a})
        sa.save('scope-a-deploy', {
            'text': 'deploy canary rollout promote production staged in waves on the staging cluster.',
            'categories': ['REFERENCE'],
        })
        sb = src(name='scope_b', config={'memory_dir': b})
        # Same topic + shared terms (deploy/rollout/production/promotion) but a
        # distinct sentence so content-dedup does not collapse it; this is the
        # record whose presence/absence we assert across the enabled/disabled boundary.
        sb.save('scope-b-promotion', {
            'text': 'production promotion gate: approve the canary rollout before it reaches the live cluster.',
            'categories': ['REFERENCE'],
        })
        return (a, b)

    def _scoped_orch(self, scope_dirs):
        d_a, d_b = scope_dirs
        orch = _make_orchestrator(d_a)
        orch.register_source(
            HermesDefaultMemorySource(name='scope_b', config={'memory_dir': d_b}),
            priority=10)
        return orch

    def test_scoping_enabled_and_disabled(self, scope_dirs):
        query = "deploy canary rollout production promotion"

        # 1) Both sources enabled → scope_b's record is reachable and attributed.
        orch = self._scoped_orch(scope_dirs)
        results = orch.search(query, limit=10)
        by_key = {r.get('key'): r for r in results}
        assert 'scope-b-promotion' in by_key, (
            "enabled scope_b did not contribute its record; returned="
            f"{sorted(by_key)}"
        )
        assert by_key['scope-b-promotion'].get('source') == 'scope_b', (
            f"record scope-b-promotion attributed as "
            f"{by_key['scope-b-promotion'].get('source')!r} (expected 'scope_b')"
        )

        # 2) scope_b disabled → only scope_a (hermes_default) results may remain.
        orch2 = self._scoped_orch(scope_dirs)
        assert orch2.disable_source('scope_b'), "scope_b must be a registered source"
        results2 = orch2.search(query, limit=10)
        for r in results2:
            assert r.get('source') != 'scope_b', (
                f"disabled scope_b leaked record {r.get('key')!r} into results"
            )
        keys2 = {r.get('key') for r in results2}
        assert 'scope-b-promotion' not in keys2, (
            "disabled source_b record still recalled: " + str(sorted(keys2))
        )
        assert 'scope-a-deploy' in keys2, (
            "scoping must not zero out recall — scope_a record missing: "
            + str(sorted(keys2))
        )


# ---------------------------------------------------------------------------
# Battery — determinism + runtime gate
# ---------------------------------------------------------------------------

class TestRecallBatteryDeterminism:
    def test_consecutive_runs_identical(self, battery_orchestrator):
        data = _load_battery_data()
        run1 = {q['text']: [r.get('key') for r in battery_orchestrator.search(q['text'], limit=10)]
                for q in data['queries']}
        run2 = {q['text']: [r.get('key') for r in battery_orchestrator.search(q['text'], limit=10)]
                for q in data['queries']}
        diff = {k for k in run1 if run1[k] != run2.get(k)}
        assert not diff, f"non-deterministic rankings for queries: {sorted(diff)}"

    def test_runtime_budget(self, battery_orchestrator):
        data = _load_battery_data()
        start = time.perf_counter()
        for q in data['queries']:
            battery_orchestrator.search(q['text'], limit=10)
        elapsed = time.perf_counter() - start
        assert elapsed < BATTERY_BUDGET_SECONDS, (
            f"battery runtime {elapsed:.1f}s exceeds {BATTERY_BUDGET_SECONDS:.0f}s budget"
        )


# ---------------------------------------------------------------------------
# Standalone runner (pytest --recall-battery is handled in tests/conftest.py)
# ---------------------------------------------------------------------------

def main():
    import tempfile
    d = tempfile.mkdtemp(prefix='battery_manual_')
    _seed_corpus(d)
    orch = _make_orchestrator(d)
    data = _load_battery_data()
    result_map = _run_queries(orch, data['queries'])
    metrics = _compute_metrics(data['queries'], result_map)
    print("\n=== recall battery report ===")
    for name in ('recall5', 'recall10', 'mrr', 'precision5'):
        print('  %-12s %.4f   (pinned floor %.4f)' % (name, metrics[name], PINNED_FLOORS[name]))
    print("per-query detail:")
    print(_format_report(metrics['per_query']))


if __name__ == '__main__':
    main()
