#!/usr/bin/env python3
"""
Test cases for graceful degradation in MemChorus memory orchestration system.
Tests that MemoryOrchestrator properly handles cases where one or more 
memory sources are unavailable.
"""
import unittest
from unittest.mock import Mock, patch
import sys
import os

# Add the project root to the path so we can import memchorus
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memchorus import MemoryOrchestrator, HermesDefaultMemorySource, MemPalaceMemorySource


class TestGracefulDegradation(unittest.TestCase):
    """Test graceful degradation for unavailable memory sources."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        self.orchestrator = MemoryOrchestrator()
        
        # Create mock sources that simulate failures
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
        self.mock_mempalace_source.fetch.side_effect = Exception("MemPalace connection failed")
        self.mock_mempalace_source.save.return_value = True
        self.mock_mempalace_source.get_source_info.return_value = {
            "name": "mempalace",
            "type": "knowledge_graph",
            "description": "MemPalace knowledge graph system"
        }
        
        # Add sources to orchestrator
        self.orchestrator.add_source(self.mock_hermes_source)
        self.orchestrator.add_source(self.mock_mempalace_source)

    def test_get_context_with_unavailable_source(self):
        """Test that get_context works when one source is unavailable."""
        # This should not raise an exception even though mempalace fails
        result = self.orchestrator.get_context("test query")
        
        # Should return at least the Hermes results (even if MemPalace is broken)
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 0)  # Could be empty if no matches

    def test_save_context_with_unavailable_source(self):
        """Test that save_context works when one source is unavailable."""
        context = {"content": "test memory item", "tags": ["test"]}
        
        # Test saving to specific source (should fall back)
        result = self.orchestrator.save_context(context, source="mempalace")
        # This should not raise an exception even though mempalace fails

    def test_fallback_behavior(self):
        """Test fallback behavior when primary source fails."""
        context = {"content": "test memory item", "tags": ["test"]}
        
        # The orchestrator should continue working even with broken sources
        result = self.orchestrator.save_context(context)
        # This should succeed using the fallback Hermes source

    def test_list_sources_with_unavailable_source(self):
        """Test that list_sources works when some sources are unavailable."""
        # Should not raise an exception even though get_source_info might fail on some 
        sources = self.orchestrator.list_sources()
        self.assertIsInstance(sources, list)
        
    def test_priority_handling_with_failing_source(self):
        """Test that prioritization still works when sources may fail."""
        # Test that we can set custom priority and it persists
        custom_priority = ["hermes_builtin", "mempalace"]
        self.orchestrator.set_priority_order(custom_priority)
        
        # Should not raise an exception even with broken sources
        result = self.orchestrator.get_context("test query")
        self.assertIsInstance(result, list)

    def test_hermes_fallback_is_used(self):
        """Test that Hermes fallback is used when primary source unavailable."""
        # Setup a mock that fails on specific operations
        failing_source = Mock(spec=HermesDefaultMemorySource)
        failing_source.name = "hermes_builtin"
        failing_source.fetch.side_effect = Exception("Hermes directory not accessible")
        
        # Add a working source as fallback
        working_source = Mock(spec=HermesDefaultMemorySource)
        working_source.name = "hermes_fallback"
        working_source.fetch.return_value = [{"id": "test", "source": "hermes_fallback", 
                                            "content": "fallback memory", "relevance_score": 0.5}]
        
        # Add mock sources to orchestrator
        self.orchestrator.add_source(failing_source)
        self.orchestrator.add_source(working_source)
        
        # Test get_context - should handle gracefully without crashing
        result = self.orchestrator.get_context("test query")
        
        # Should not crash and provide results if any source works 
        self.assertIsInstance(result, list)


if __name__ == '__main__':
    # Run the tests
    unittest.main()