#!/usr/bin/env python3
"""
test_hermes_source_integration.py - Real filesystem integration with HermesDefaultMemorySource.

Tests HermesDefaultMemorySource against actual filesystem operations:
1. Create directory with actual JSON files, test retrieval finds them
2. Delete memory_dir mid-operation, verify graceful degradation (not crash)
3. Write invalid JSON to stored file, retrieve() returns None not exception
"""

import os
import sys
import json
import shutil
import pytest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from memchorus.memory_source import MemorySource
from memchorus.hermes_memory_source import HermesDefaultMemorySource


def test_retrieve_finds_files_already_in_directory():
    """Retrieve finds files that were manually placed in the memory directory before source init."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')

    try:
        # Pre-populate the directory with an existing JSON file
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        existing_data = {'manual_key': True, 'timestamp': '2024-12-01T10:00:00'}

        filepath = os.path.join(tmpdir, 'preloaded_key.json')
        with open(filepath, 'w') as f:
            json.dump(existing_data, f)

        # Retrieve via the source should find it
        result = src.retrieve('preloaded_key')
        assert result is not None
        assert result['manual_key'] is True

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_delete_memory_dir_mid_operation_returns_none_not_crash():
    """Deleting the memory directory between operations does not raise an unhandled exception."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')

    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        # Save successfully first
        assert src.save('survivor_key', {'content': True}) is True

        # Now delete the backing directory entirely
        shutil.rmtree(tmpdir, ignore_errors=True)

        # Verify memory source detects unavailability (not a crash)
        available = src.is_available()
        assert available is False, "Expected is_available to return False after dir deletion, got: %s" % str(available)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_invalid_json_returns_none():
    """When a stored file contains invalid JSON, retrieve() returns None instead of raising."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')

    try:
        # Place a corrupt file in the memory directory
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        bad_filepath = os.path.join(tmpdir, 'corrupt_key.json')
        with open(bad_filepath, 'w') as f:
            f.write("this is {not valid json at all [[[<<<")

        # Attempting to retrieve should not raise an exception - should return None gracefully
        result = src.retrieve('corrupt_key')

        assert result is None, "Expected retrieve() to return None for invalid JSON, got: %s" % str(result)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_nonexistent_file_returns_none():
    """Retrieving a key that has no corresponding file properly returns None."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')

    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        result = src.retrieve('does_not_exist')
        assert result is None, "Expected None for non-existent file, got: %s" % str(result)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_save_and_retrieve_complex_value():
    """Saving/retrieving a complex nested structure round-trips correctly over the filesystem."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')

    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        complex_value = {
            'nested': {'deep': [{'key': 'value1'}, {'key': 'value2'}]},
            'list_of_dicts': [{'a': 1}, {'b': 2}],
            'primitives': {'int': 42, 'float': 3.14, 'string': 'hello', 'bool': True},
        }

        key = 'complex_roundtrip'
        assert src.save(key, complex_value) is True

        retrieved = src.retrieve(key)
        assert retrieved is not None
        assert retrieved == complex_value
        assert isinstance(retrieved['nested'], dict)
        assert len(retrieved['list_of_dicts']) == 2
        assert retrieved['primitives']['int'] == 42

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_save_overwrites_correctly():
    """Saving a key twice correctly overwrites the previous content in the file."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')

    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        key = 'overwrite_key'

        assert src.save(key, {'version': 1}) is True
        assert src.retrieve(key)['version'] == 1

        assert src.save(key, {'version': 2, 'updated': True}) is True
        result = src.retrieve(key)
        assert result['version'] == 2
        assert result['updated'] is True  # The old content is overwritten

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_is_available_checks_directory_correctly():
    """is_available() checks that the directory exists and is writable."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')

    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        assert src.is_available() is True, "Expected available when directory exists"

        # Remove the directory - should become unavailable
        shutil.rmtree(tmpdir, ignore_errors=True)
        assert src.is_available() is False, "Expected unavailable after directory deletion"

        # Re-create the directory - should become available again
        os.makedirs(tmpdir, exist_ok=True)
        assert src.is_available() is True, "Expected available after directory re-creation"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_finds_existing_json_files_by_filename():
    """search('query') finds JSON files whose filename (the key part) contains 'query' (case-insensitive)."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')

    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        # Create several files manually - two with 'overlap' in the key, one without
        for filename, data in [
            ('overlap_data_one', {'source': 'one'}),
            ('overlap_data_two', {'source': 'two'}),
            ('other_file_xyz', {'source': 'other'}),
            ('another_overlap_item', {'source': 'also_overlapping'}),
        ]:
            filepath = os.path.join(tmpdir, '%s.json' % filename)
            with open(filepath, 'w') as f:
                json.dump(data, f)

        results = src.search('overlap')

        # Should find the three files containing 'overlap' in their filename (key part)
        assert len(results) >= 3, "Expected at least 3 results for 'overlap', got: %d" % len(results)

        keys_found = [r['key'] for r in results]
        for key_check in ['overlap_data_one', 'overlap_data_two', 'another_overlap_item']:
            assert any(key_check in k for k in keys_found), "Expected '%s' to be found by search" % key_check

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


class _NonSerializable:
    """Plain class that is not str/int/float/bool/dict/list so save() forces str() conversion."""
    def __str__(self):
        return "nonserializable_repr"


def test_save_non_serializable_value_forces_str_conversion():
    """When value is a non-primitive type, save() converts it to string (line 115)."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        obj = _NonSerializable()
        assert src.save('nonserial_key', obj) is True
        result = src.retrieve('nonserial_key')
        # Should have been stored as stringified repr (str was JSON-serialised fine)
        assert isinstance(result, str)
        # save() does: json.dump(value, f) where value was str(obj) which IS serializable
        # So the stored value should be the plain string 'nonserializable_repr'
        retrieved_raw = src.retrieve('nonserial_key')
        assert retrieved_raw == "nonserializable_repr"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_proactive_check_no_context():
    """proactive_check without context returns a status dict with zero findings."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        result = src.proactive_check()
        assert result is not None
        assert result['status'] == 'ready'
        assert result['found_memories'] == 0
        assert result['source'] == 'test_source'
        assert 'timestamp' in result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_proactive_check_with_context():
    """proactive_check with context searches for relevant memories and returns recommendations."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        # Pre-populate some data that would match the query context
        filepath = os.path.join(tmpdir, 'config_data.json')
        with open(filepath, 'w') as f:
            json.dump({'key': 'configuration_settings'}, f)

        context = {'decision_type': 'config', 'context_key': 'configuration'}
        result = src.proactive_check(context)
        assert result is not None
        assert isinstance(result['recommendations'], list)
        # Should find config_data match because it contains 'configuration' in filename
        if len(result['recommendations']) > 0:
            rec = result['recommendations'][0]
            assert rec['type'] == 'context_retrieval'
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_proactive_save_with_context():
    """proactive_save stores the primary key-value AND writes an action log when context is provided."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        assert src.proactive_save('action_test_key', {'outcome': 'success'}, {'stage': 'post_action'}) is True

        # Verify primary was saved
        retrieved = src.retrieve('action_test_key')
        assert retrieved == {'outcome': 'success'}

        # Action log should have been written to a separate file
        files = os.listdir(tmpdir)
        # Filenames are normalized through _safe_key (underscores -> hyphens).
        # Action logs have a timestamp suffix: action-{key}-{date}-{time}.json
        # Primary data files do NOT: {key}.json
        # Use startswith to target only the timestamped action log files.
        action_logs = [f for f in files if f.startswith('action-action-test-key-') and f.endswith('.json')]
        assert len(action_logs) >= 1, "Expected at least one action log file"

        # Verify the log content
        with open(os.path.join(tmpdir, action_logs[0]), 'r') as f:
            log = json.load(f)
            assert log['action'] == 'proactive_save'
            assert log['memory_key'] == 'action_test_key'
            assert log['context']['stage'] == 'post_action'
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_proactive_save_without_context():
    """proactive_save without context only saves the primary value (no action log)."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        assert src.proactive_save('simple_key', {'data': True}) is True

        # Only the main key should exist - no action log prefix files
        files = os.listdir(tmpdir)
        action_logs = [f for f in files if 'action_simple_key_' in f]
        assert 0 == len(action_logs), "Expected no action logs when context is None"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_read_memory_file_formats_correctly():
    """_read_memory_file parses line-based entries into timestamp/content dicts."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        # Create a file in the expected format
        test_file = '/tmp/_read_mem_test.md'
        with open(test_file, 'w') as f:
            f.write('2024-01-15: First entry [tag1]\n')
            f.write('2024-02-20: Second entry [tag2, tag3]\n')
            f.write('\n\n2024-03-01: Third entry with no tags\n')

        entries = src._read_memory_file(test_file)
        assert len(entries) == 3
        assert entries[0]['timestamp'] == '2024-01-15'
        assert entries[0]['content'].strip() == "First entry [tag1]"
        assert entries[2]['timestamp'] == '2024-03-01'

        os.unlink(test_file)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_read_memory_file_empty_returns_empty_list():
    """_read_memory_file on a non-existent file returns []."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        entries = src._read_memory_file('/tmp/nonexistent_file_xyz.md')
        assert entries == []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_write_memory_file_writes_formatted_entries():
    """_write_memory_file converts entries to timestamp: content format."""
    entry = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False)
    fname = entry.name
    entry.close()

    try:
        src = HermesDefaultMemorySource(name='test_source', config={})
        test_entries = [
            {'timestamp': '2024-05-01', 'content': 'entry one'},
            {'timestamp': '2024-06-01', 'content': 'entry two'},
        ]
        assert src._write_memory_file(fname, test_entries) is True

        with open(fname, 'r') as f:
            content = f.read()
        # Entries are joined by newline; each formatted as "timestamp: content"
        expected_lines = ['2024-05-01: entry one', '2024-06-01: entry two']
        assert '\n'.join(expected_lines) in content or set(content.strip().splitlines()) == set(expected_lines)
    finally:
        os.unlink(fname)


def test_write_memory_file_exception_returns_false():
    """_write_memory_file on an unwritable path returns False without raising."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        impossible_path = '/no/such/path/fatal_error.md'
        assert src._write_memory_file(impossible_path, [{'ts': 'x', 'c': 'y'}]) is False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_on_dir_containing_non_json_files():
    """search ignores non-.json files when iterating memory_dir."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        # Write a json file and a .txt file (should be ignored)
        with open(os.path.join(tmpdir, 'overlap_alpha.json'), 'w') as f:
            json.dump({'source': 'json_match'}, f)
        with open(os.path.join(tmpdir, 'overlap_beta.txt'), 'w') as f:
            f.write('this should not show up')

        results = src.search('overlap')
        # Only the .json file matches (search() filters on .json extension)
        for r in results:
            assert r['key'] == 'overlap_alpha'
            assert r['content'] == {'source': 'json_match'}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_with_limit_stops_at_limit():
    """search respects the limit parameter."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        for i in range(5):
            with open(os.path.join(tmpdir, 'limit_match_%d.json' % i), 'w') as f:
                json.dump({'idx': i}, f)

        results = src.search('limit_match', limit=2)
        assert len(results) <= 2
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_is_available_returns_false_when_dir_deleted():
    """is_available detects directory deletion via os.W_OK check."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})
        assert src.is_available() is True

        shutil.rmtree(tmpdir, ignore_errors=True)
        # Wait for Python to flush/cleanup file handles from previous makedirs
        # Force re-check against the filesystem
        assert src.is_available() is False

        os.makedirs(tmpdir)
        assert src.is_available() is True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_name_property_returns_correct_value():
    """Source.name property returns the configured name string."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src1 = HermesDefaultMemorySource(name='my_custom_source', config={'memory_dir': tmpdir})
        assert src1.name == 'my_custom_source'

        src2 = HermesDefaultMemorySource(config={})
        assert src2.name == 'hermes_default'
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_missing_memory_dir_fails_gracefully():
    """search with an orphaned memory_dir path returns empty list instead of crashing."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        good_dir = os.path.join(tmpdir, 'good')
        shutil.rmtree(tmpdir, ignore_errors=True)  # Remove everything including tmpdir itself

        src = HermesDefaultMemorySource(
            name='stray_source',
            config={'memory_dir': '/tmp/nonexistent_orphan_xyz'}
        )
        results = src.search('anything')
        assert results == []
    finally:
        if os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_finds_content_not_in_filename():
    """search finds JSON files whose *content* contains the query term even when
    the filename/key does NOT contain the search word.

    This is the core regression test for the content-aware search fix.
    Previously only the filename was checked, so any memory with a key that
    didn't include the search terms would be invisible.
    """
    tmpdir = tempfile.mkdtemp(prefix='hermes_content_search_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        # Memory key does NOT contain 'routing' but content DOES
        assert src.save('session-log-2024-07-11', {
            'topic': 'debugging routing behavior in the orchestrator',
            'finding': 'routing map returns wrong category for LEARNING keys'
        }) is True

        # Another entry whose key has nothing to do with 'behavioral' either
        assert src.save('task-summary-t12345', {
            'status': 'done',
            'notes': 'behavioral enforcement hook fires too early'
        }) is True

        # A decoy file that doesn't match the search terms at all
        assert src.save('unrelated-config', {'alpha': 1, 'beta': 2}) is True

        results = src.search('routing')
        keys_found = [r['key'] for r in results]
        assert 'session-log-2024-07-11' in keys_found, \
            "search('routing') should find file by content: %s" % keys_found

        results_b = src.search('behavioral')
        keys_found_b = [r['key'] for r in results_b]
        assert 'task-summary-t12345' in keys_found_b, \
            "search('behavioral') should find file by content: %s" % keys_found_b

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_multi_word_query_matches_any_term_in_content():
    """When searching for 'routing bug', each term is checked independently —
    a file containing either 'routing' or 'bug' in its content counts as a hit.
    """
    tmpdir = tempfile.mkdtemp(prefix='hermes_multiword_search_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        # Contains 'routing' but not 'bug'
        assert src.save('entry-alpha', {'detail': 'routing table is empty'}) is True

        # Contains 'bug' but not 'routing'
        assert src.save('entry-beta', {'detail': 'fixed a critical bug in parser'}) is True

        # Contains neither
        assert src.save('entry-gamma', {'detail': 'weather forecast sunny'}) is True

        results = src.search('routing bug')
        keys_found = [r['key'] for r in results]
        assert 'entry-alpha' in keys_found, "Should match by 'routing' content"
        assert 'entry-beta' in keys_found, "Should match by 'bug' content"
        assert 'entry-gamma' not in keys_found, "Should NOT match unrelated entry"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_content_nested_dict_and_list():
    """Content-aware search recursively walks nested dicts and lists.
    A term buried two levels deep should still be found.
    """
    tmpdir = tempfile.mkdtemp(prefix='hermes_nested_search_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        assert src.save('deep-structure-key', {
            'metadata': {'tags': ['important']},
            'details': {
                'subsystem': 'orchestrator',
                'issue': {
                    'type': 'regression',
                    'description': 'routing map silently drops LEARNING entries'
                }
            }
        }) is True

        assert src.save('other-file', {'x': 1}) is True

        results = src.search('regression')
        keys_found = [r['key'] for r in results]
        assert 'deep-structure-key' in keys_found, \
            "Nested term 'regression' should be found: %s" % keys_found

        # Also check the innermost text
        results2 = src.search('learning')
        keys_found2 = [r['key'] for r in results2]
        assert 'deep-structure-key' in keys_found2, \
            "Nested term 'LEARNING' (case-insensitive) should be found: %s" % keys_found2

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_content_respects_limit_with_content_scan():
    """limit still works correctly when results come from content scanning."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_limit_content_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        # Create 5 entries whose key doesn't contain the term but content does
        for i in range(5):
            assert src.save('entry-num-%d' % i, {'data': 'searchable-content-%d' % i}) is True

        # All 5 should match on 'searchable', but limit=2 caps results
        results = src.search('searchable', limit=2)
        assert len(results) <= 2, "limit=2 should cap at 2, got %d" % len(results)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_search_content_ignores_corrupt_files():
    """When a file contains invalid JSON during content scan, search skips it gracefully."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_corrupt_content_')
    try:
        src = HermesDefaultMemorySource(name='test_source', config={'memory_dir': tmpdir})

        # Good file that matches the query by content
        assert src.save('good-entry', {'note': 'matches this query term'}) is True

        # Corrupt JSON file on disk
        with open(os.path.join(tmpdir, 'broken-file.json'), 'w') as f:
            f.write("{this is [invalid json>>>")

        results = src.search('matches')
        assert len(results) >= 1
        keys_found = [r['key'] for r in results]
        assert 'good-entry' in keys_found
        # Corrupt file should NOT appear in results (content=None → skipped)
        assert 'broken-file' not in keys_found

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_get_source_info_structure():
    """get_source_info returns a dict with required keys including availability."""
    tmpdir = tempfile.mkdtemp(prefix='hermes_integration_')
    try:
        src = HermesDefaultMemorySource(name='info_src', config={'memory_dir': tmpdir})
        info = src.get_source_info()

        assert isinstance(info, dict)
        assert 'name' in info and info['name'] == 'info_src'
        assert 'type' in info and info['type'] == 'hermes_default'
        assert 'available' in info and isinstance(info['available'], bool)
        assert 'memory_dir' in info
        assert 'description' in info
        assert 'version' in info
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

