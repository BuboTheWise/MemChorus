#!/usr/bin/env python3
"""
Example usage script demonstrating MemChorus in practice.
"""

import sys
import os

# Add current directory to Python path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memchorus import MemoryOrchestrator, HermesDefaultMemorySource, MemPalaceMemorySource

def demonstrate_usage():
    """Demonstrate typical usage patterns."""
    
    print("=== MemChorus Usage Demonstration ===\n")
    
    # Initialize orchestrator
    print("1. Creating Memory Orchestrator...")
    orchestrator = MemoryOrchestrator()
    print("✓ Created orchestrator\n")
    
    # Add memory sources (these would be properly configured in real usage)
    print("2. Adding memory sources...")
    hermes_source = HermesDefaultMemorySource()
    mempalace_source = MemPalaceMemorySource()
    
    orchestrator.add_source(hermes_source)
    orchestrator.add_source(mempalace_source)
    print("✓ Added memory sources\n")
    
    # Initialize all sources
    print("3. Initializing memory sources...")
    orchestrator.initialize_all({
        "hermes_builtin": {"path": "/home/user/.hermes/memory"},
        "mempalace": {"host": "localhost", "port": 8080}
    })
    print("✓ Memory sources initialized\n")
    
    # List available sources
    print("4. Available memory sources:")
    sources = orchestrator.list_sources()
    for source in sources:
        print(f"  - {source['name']}: {source['description']}")
    print()
    
    # Simulate context retrieval
    print("5. Retrieving relevant memories...")
    context = orchestrator.get_context("project planning", relevance_threshold=0.3)
    print(f"✓ Retrieved {len(context)} memories")
    if context:
        print(f"  First result: {context[0].get('content', 'No content')[:100]}...")
    print()
    
    # Save memory example
    print("6. Saving new memory...")
    success = orchestrator.save_context({
        "content": "Discussed project timeline with team",
        "tags": ["project", "discussion"],
        "timestamp": "2026-06-10T10:00:00Z"
    })
    print(f"✓ Memory save {'successful' if success else 'failed'}")
    print()
    
    # Query specific source
    print("7. Querying specific source...")
    source_info = orchestrator.source_info("hermes_builtin")
    if source_info:
        print(f"✓ Source info: {source_info['name']} - {source_info['status']}")
        print(f"  Description: {source_info['description']}")
    
    print("\n=== Demo Complete ===")

if __name__ == "__main__":
    demonstrate_usage()