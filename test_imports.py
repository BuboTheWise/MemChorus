#!/usr/bin/env python3
"""
Simple test to verify import and basic functionality
"""
try:
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    # This should work if the module is properly importable
    print("Testing imports...")
    
    # Importing memchorus directly (should not error)
    import memchorus
    print("memchorus imported successfully")
    
    # Test basic functionality
    from memchorus import MemoryOrchestrator, HermesDefaultMemorySource
    
    print("Basic classes can be imported")
    
    # Create instances to make sure they work
    orchestrator = MemoryOrchestrator()
    print("MemoryOrchestrator created successfully")
    
    hermes_source = HermesDefaultMemorySource()
    print("HermesDefaultMemorySource created successfully")
    
    print("All basic imports and instantiations successful")
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()