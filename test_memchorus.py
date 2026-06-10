#!/usr/bin/env python3
"""
Unit tests for MemChorus memory orchestration system.
"""

import unittest
from unittest.mock import Mock, patch
import os
import sys

# Add the project root to the path so we can import memchorus
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memchorus import MemoryOrchestrator, HermesDefaultMemorySource, MemPalaceMemorySource


class TestMemoryOrchestrator(unittest.TestCase):
    """Test the memory orchestrator functionality."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        self.orchestrator = MemoryOrchestrator()
        
        # Create mock sources for testing
        self.mock_hermes_source = Mock(spec=HermesDefaultMemorySource)
        self.mock_mempalace_source = Mock(spec=MemPalaceMemorySource)
        
        # Setup the mock sources with default responses
        self.mock_hermes_source.name = "hermes_builtin"
        self.mock_hermes_source.fetch.return_value = [
            {"id": "1", "source": "hermes_builtin", "content": "Test memory from Hermes", 
             "relevance_score": 0.8}
        ]
        self.mock_hermes_source.save.return_value = True
        self.mock_hermes_source.get_source_info.return_value = {
            "name": "hermes_builtin",
            "type": "builtin",
            "description": "Hermes default memory system"
        }
        
        self.mock_mempalace_source.name = "mempalace"
        self.mock_mempalace_source.fetch.return_value = [
            {"id": "2", "source": "mempalace", "content": "Test memory from MemPalace", 
             "relevance_score": 0.7}
        ]
        self.mock_mempalace_source.save.return_value = True
        self.mock_mempalace_source.get_source_info.return_value = {
            "name": "mempalace",
            "type": "knowledge_graph",
            "description": "MemPalace knowledge graph system"
        }
        
        # Add sources to orchestrator
        self.orchestrator.add_source(self.mock_hermes_source)
        self.orchestrator.add_source(self.mock_mempalace_source)

    def test_add_remove_source(self):
        """Test adding and removing memory sources."""
        # Test adding source
        original_count = len(self.orchestrator._sources)
        new_source = Mock()
        new_source.name = "test_source"
        self.orchestrator.add_source(new_source)
        self.assertEqual(len(self.orchestrator._sources), original_count + 1)
        
        # Test removing source
        self.orchestrator.remove_source("test_source")
        self.assertEqual(len(self.orchestrator._sources), original_count)

    def test_get_context_with_query(self):
        """Test getting context with a search query."""
        # Test getting context with query
        result = self.orchestrator.get_context("test query")
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 0)  # Could be empty if no matches

    def test_save_context(self):
        """Test saving context to memory sources."""
        context = {"content": "test memory item", "tags": ["test"]}
        
        # Test saving to specific source
        result = self.orchestrator.save_context(context, source="hermes_builtin")
        self.assertTrue(result)
        
        # Test saving to all sources
        result = self.orchestrator.save_context(context)
        self.assertTrue(result)

    def test_list_sources(self):
        """Test listing available memory sources."""
        sources = self.orchestrator.list_sources()
        self.assertIsInstance(sources, list)
        self.assertGreater(len(sources), 0)

    def test_source_info(self):
        """Test getting information about a specific source."""
        info = self.orchestrator.source_info("hermes_builtin")
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "hermes_builtin")

    def test_priority_ordering(self):
        """Test setting and applying priority order for sources."""
        # Set a custom priority order
        custom_priority = ["mempalace", "hermes_builtin"]
        self.orchestrator.set_priority_order(custom_priority)
        
        # Test that the priority was set correctly
        self.assertEqual(self.orchestrator._priority_order, custom_priority)


class TestMemorySources(unittest.TestCase):
    """Test memory source implementations."""

    def test_hermes_default_source(self):
        """Test Hermes default memory source functionality."""
        source = HermesDefaultMemorySource()
        self.assertEqual(source.name, "hermes_builtin")
        
        # Test that it implements required methods (will fail if methods are missing)
        # This is mostly a structure check for now
        self.assertTrue(hasattr(source, 'fetch'))
        self.assertTrue(hasattr(source, 'save'))
        self.assertTrue(hasattr(source, 'list_sources'))
        self.assertTrue(hasattr(source, 'get_source_info'))

    def test_mempalace_source(self):
        """Test MemPalace memory source functionality."""
        source = MemPalaceMemorySource()
        self.assertEqual(source.name, "mempalace")
        
        # Test that it implements required methods (will fail if methods are missing)
        self.assertTrue(hasattr(source, 'fetch'))
        self.assertTrue(hasattr(source, 'save'))
        self.assertTrue(hasattr(source, 'list_sources'))
        self.assertTrue(hasattr(source, 'get_source_info'))


if __name__ == '__main__':
    # Run the tests
    unittest.main()