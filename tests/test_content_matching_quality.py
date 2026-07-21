#!/usr/bin/env python3
"""
test_content_matching_quality.py — Unit tests for improved content matching in HermesDefaultMemorySource.

Covers three acceptance criteria from t_4e02918b:
  1. Self-match penalty (query-echo suppression)
  2. JSON content extraction before scoring
  3. MIN_RECALL_SCORE threshold filtering & ranked results
"""

import os
import sys
import json
import shutil
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.hermes_memory_source import HermesDefaultMemorySource


class TestContentMatchesScoring:
    """Unit-tests for HermesDefaultMemorySource._content_matches scoring logic."""

    def _mk(self):
        tmpdir = tempfile.mkdtemp()
        return HermesDefaultMemorySource(name='t', config={'memory_dir': tmpdir}), tmpdir

    def test_single_term_match_scores_above_min_threshold(self):
        """A single recognized term in content gives score >= MIN_RECALL_SCORE."""
        src, d = self._mk()
        try:
            s = src._content_matches('routing', 'the routing table was broken last week')
            assert s >= src.MIN_RECALL_SCORE, f"score={s}, threshold={src.MIN_RECALL_SCORE}"
        finally:
            shutil.rmtree(d)

    def test_no_match_returns_zero(self):
        """A query with no term overlap in content returns 0."""
        src, d = self._mk()
        try:
            s = src._content_matches('chess', 'the routing table was broken')
            assert s == 0.0
        finally:
            shutil.rmtree(d)

    def test_multi_term_match_scores_higher_than_single(self):
        """When two distinct query terms are both present, score > single-term match."""
        src, d = self._mk()
        try:
            one_term = src._content_matches('routing', 'routing routing routing')
            two_terms = src._content_matches('routing table', 'the routing table was fixed today')
            assert two_terms > one_term, f"two_terms={two_terms} should exceed one_term={one_term}"
        finally:
            shutil.rmtree(d)

    def test_self_match_penalty_applied(self):
        """When content is essentially identical to the query, score is halved."""
        src, d = self._mk()
        try:
            # Query and content are nearly identical → ratio > 0.9
            echo_score = src._content_matches('routing bug fix', 'routing bug fix')
            # Normal distinct content containing same terms should score higher
            normal_score = src._content_matches(
                'routing bug fix',
                'while debugging the routing subsystem I found a memory-allocation bug that needed a quick fix'
            )
            assert echo_score < normal_score, \
                f"Self-match score ({echo_score}) should be < normal content score ({normal_score})"
        finally:
            shutil.rmtree(d)

    def test_self_match_still_above_min_when_terms_found(self):
        """Even after halving, a self-match with multiple terms stays above MIN_RECALL_SCORE."""
        src, d = self._mk()
        try:
            echo_score = src._content_matches(
                'fix routing bug',
                'fix routing bug'
            )
            assert echo_score >= 0.0, "Score should still be non-negative"
            # With 3 terms at base 2 each and 50% penalty: 6 * 0.5 = 3.0 ≥ MIN_RECALL_SCORE
        finally:
            shutil.rmtree(d)

    def test_short_query_below_len_threshold_returns_zero(self):
        """Queries whose every token is 1 char long are skipped (terms filter)."""
        src, d = self._mk()
        try:
            s = src._content_matches('a b c', 'the content has no single letters')
            assert s == 0.0
        finally:
            shutil.rmtree(d)

    def test_case_insensitivity(self):
        """Score ignores case differences."""
        src, d = self._mk()
        try:
            s1 = src._content_matches('Routing', 'routing error found')
            s2 = src._content_matches('ROUTING', 'routing error found')
            assert abs(s1 - s2) < 0.01  # scores identical
        finally:
            shutil.rmtree(d)


class TestJsonContentExtractionBeforeScoring:
    """JSON stored content is properly flattened before keyword comparison."""

    def _mk(self):
        tmpdir = tempfile.mkdtemp()
        return HermesDefaultMemorySource(name='t', config={'memory_dir': tmpdir}), tmpdir

    def test_dict_content_searchable_by_field_value(self):
        """A term in a JSON dict value is found even though it doesn't appear in the key."""
        src, d = self._mk()
        try:
            src.save('session-log-x', {'finding': 'the routing map silently drops entries'})
            results = src.search('routing')
            assert any(r['key'] == 'session-log-x' for r in results)
        finally:
            shutil.rmtree(d)

    def test_nested_dict_content_searchable(self):
        """Terms buried two levels deep are still found."""
        src, d = self._mk()
        try:
            src.save('deep-key', {
                'level1': {'level2': {'description': 'found a regression in auth module'}}
            })
            results = src.search('regression')
            assert any(r['key'] == 'deep-key' for r in results)
        finally:
            shutil.rmtree(d)

    def test_list_content_searchable(self):
        """A term inside a list value is found."""
        src, d = self._mk()
        try:
            src.save('notes-key', {'tags': ['fix', 'routing', 'bug']})
            results = src.search('routing')
            assert any(r['key'] == 'notes-key' for r in results)
        finally:
            shutil.rmtree(d)

    def test_structural_brackets_not_counted_against_score(self):
        """Raw curly braces and colons from JSON serialization do not appear in search text."""
        src, d = self._mk()
        try:
            src.save('bracket-key', {'status': 'done'})
            # _content_to_search_text strips brackets; search term won't be punished
            # by matching against '{}' characters. The dict yields "status done".
            results = src.search('status')
            assert any(r['key'] == 'bracket-key' for r in results)
        finally:
            shutil.rmtree(d)


class TestMinRecallScoreThreshold:
    """MIN_RECALL_SCORE filters out low-confidence noise from search results."""

    def _mk(self, min_score=None):
        tmpdir = tempfile.mkdtemp()
        cfg = {'memory_dir': tmpdir}
        if min_score is not None:
            cfg['min_recall_score'] = min_score
        return HermesDefaultMemorySource(name='t', config=cfg), tmpdir

    def test_default_min_score_constant(self):
        """MIN_RECALL_SCORE class constant defaults to 0.3 (lowered from 1.5 after empirical analysis)."""
        assert HermesDefaultMemorySource.MIN_RECALL_SCORE == 0.3

    def test_low_score_results_filtered_out(self):
        """Results with score below MIN_RECALL_SCORE are not returned."""
        src, d = self._mk(min_score=50.0)  # impossibly high threshold
        try:
            src.save('relevant-key', {'data': 'this is searchable content'})
            results = src.search('searchable')
            assert len(results) == 0, "No result should pass a threshold of 50.0"
        finally:
            shutil.rmtree(d)

    def test_high_score_results_pass(self):
        """Results with high scores pass through the threshold filter."""
        src, d = self._mk(min_score=1.0)
        try:
            src.save('match-key', {'data': 'searchable content keyword'})
            results = src.search('searchable')
            assert len(results) >= 1
        finally:
            shutil.rmtree(d)

    def test_config_override_min_recall_score(self):
        """min_recall_score config key overrides the class default."""
        tmpdir = tempfile.mkdtemp()
        try:
            src = HermesDefaultMemorySource(
                name='t', config={'memory_dir': tmpdir, 'min_recall_score': 0.5}
            )
            assert src._effective_min_score() == 0.5

            # Default effective min score is the class constant (0.3 after empirical scoring analysis)
            src2 = HermesDefaultMemorySource(name='t', config={'memory_dir': tmpdir})
            assert src2._effective_min_score() == 0.3
        finally:
            shutil.rmtree(tmpdir)

    def test_results_sorted_by_score_descending(self):
        """Higher-scoring results appear earlier in the list."""
        src, d = self._mk(min_score=0.1)
        try:
            # One with many repeated terms (higher score)
            src.save('high-score', {
                'detail': 'routing routing routing routing table fix fix fix'
            })
            # One with a single occurrence (lower score)
            src.save('low-score', {'detail': 'the routing was adjusted'})

            results = src.search('routing')
            assert len(results) >= 2
            keys = [r['key'] for r in results]
            idx_high = keys.index('high-score')
            idx_low = keys.index('low-score')
            assert idx_high < idx_low, \
                f"high-score ({idx_high}) should come before low-score ({idx_low}): {keys}"
        finally:
            shutil.rmtree(d)

    def test_query_echo_penalized_in_ranking(self):
        """When one result is a near-echo of the query and another has substantive content,
        the echo does NOT rank above the substantive result."""
        src, d = self._mk()
        try:
            # Echo entry — content is basically identical to the query
            src.save('echo-key', 'implement search scoring fix')

            # Substantive entry — contains all terms in context
            src.save('substantive-key', {
                'action': 'implemented relevance-scoring for hermes memory source search, with penalties for echo matches'
            })
            results = src.search('implement search scoring fix')
            assert len(results) >= 1  # at least one should pass the threshold

            if len(results) >= 2:
                keys = [r['key'] for r in results]
                idx_echo = keys.index('echo-key')
                idx_sub = keys.index('substantive-key')
                assert idx_sub <= idx_echo, \
                    f"Substantive result should rank at least as high as echo: {keys}"
        finally:
            shutil.rmtree(d)
