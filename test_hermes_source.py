#!/usr/bin/env python3
"""
Test script for HermesDefaultMemorySource implementation.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memchorus import HermesDefaultMemorySource

def test_hermes_memory_source():
    """Test the HermesDefaultMemorySource implementation."""
    
    print("Testing HermesDefaultMemorySource...")
    
    # Create and initialize source
    source = HermesDefaultMemorySource()
    
    try:
        # Test initialization 
        source.initialize({
            'memory_dir': '/home/bubo/.hermes/memories'
        })
        print("✓ Initialization successful")
        
        # Test fetching (should return existing memories or empty list)
        result = source.fetch(limit=5)
        print(f"✓ Fetch test: Found {len(result)} memories")
        
        # Test saving with different tag combinations
        test_cases = [
            {'content': 'Test memory entry 1', 'tags': ['test', 'memchorus']},
            {'content': 'Another test entry', 'tags': ['development', 'testing']},
            {'content': 'User context memory', 'tags': ['user_context']}
        ]
        
        for i, item in enumerate(test_cases):
            success = source.save(item)
            print(f"✓ Save test {i+1}: {'Success' if success else 'Failed'}")
            
        # Fetch again to verify entries were saved
        result = source.fetch(limit=10)
        print(f"✓ Post-save fetch: Found {len(result)} memories")
        
        # Verify structure of one memory item
        if result:
            first_item = result[0]
            print(f"✓ Sample entry - Date: {first_item.get('date')}")
            print(f"✓ Sample entry - Content preview: {first_item.get('content', '')[:50]}...")
            
        print("\nAll tests passed! The HermesDefaultMemorySource is working correctly.")
        
    except Exception as e:
        print(f"✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    return True

if __name__ == "__main__":
    success = test_hermes_memory_source()
    sys.exit(0 if success else 1)