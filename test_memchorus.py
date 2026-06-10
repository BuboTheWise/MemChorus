#!/usr/bin/env python3
"""
Test script for MemChorus memory orchestration skill.
"""

import sys
import os

# Add the current directory to the Python path so we can import memchorus
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memchorus import MemoryOrchestrator, HermesDefaultMemorySource, MemPalaceMemorySource

def test_orchestration():
    """Test basic orchestration functionality."""
    print("Testing MemChorus memory orchestration...")
    
    # Create orchestrator
    orchestrator = MemoryOrchestrator()
    
    # Test adding memory sources (this would be actual implementations)
    try:
        hermes_source = HermesDefaultMemorySource()
        mempalace_source = MemPalaceMemorySource()
        
        orchestrator.add_source(hermes_source)
        orchestrator.add_source(mempalace_source)
        
        print("✓ Successfully created memory sources")
        
        # Test initializing all sources
        orchestrator.initialize_all()
        print("✓ Successfully initialized all sources")
        
        # Test listing sources
        sources = orchestrator.list_sources()
        print(f"✓ Found {len(sources)} memory sources")
        
        # Test individual source info
        for source in sources:
            info = orchestrator.source_info(source['name'])
            if info:
                print(f"✓ Source '{source['name']}' info available")
                
        print("All tests passed!")
        return True
        
    except Exception as e:
        print(f"✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_api_compliance():
    """Test that the implementation follows required interfaces."""
    print("Testing API compliance...")
    
    try:
        # Check that required methods exist
        orchestrator = MemoryOrchestrator()
        
        # Test all required methods exist
        required_methods = ['get_context', 'save_context', 'list_sources', 'source_info']
        for method in required_methods:
            if not hasattr(orchestrator, method):
                raise AttributeError(f"Missing required method: {method}")
                
        print("✓ All required API methods present")
        
        # Test that memory sources have required methods
        hermes_source = HermesDefaultMemorySource()
        mempalace_source = MemPalaceMemorySource()
        
        source_methods = ['fetch', 'save', 'list_sources', 'get_source_info']
        for method in source_methods:
            if not hasattr(hermes_source, method):
                raise AttributeError(f"Hermes source missing required method: {method}")
            if not hasattr(mempalace_source, method):
                raise AttributeError(f"MemPalace source missing required method: {method}")
                
        print("✓ All memory source methods present")
        return True
        
    except Exception as e:
        print(f"✗ API compliance test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("Running MemChorus tests...")
    
    success = True
    success &= test_orchestration()
    success &= test_api_compliance()
    
    if success:
        print("\n✓ All tests passed!")
        sys.exit(0)
    else:
        print("\n✗ Some tests failed!")
        sys.exit(1)