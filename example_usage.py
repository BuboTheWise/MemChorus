"""
Example usage of MemChorus implementation
"""

import sys
sys.path.insert(0, '/home/bubo/.hermes/workspace/Code/MemChorus/src')

from memchorus.orchestrator import MemoryOrchestrator

def main():
    print("=== MemChorus v1.0 Example Usage ===")
    
    # Initialize orchestrator
    orchestrator = MemoryOrchestrator()
    
    print("\n1. Saving memories...")
    # Save some test memories
    orchestrator.save("project_status", "MemChorus implementation in progress")
    orchestrator.save("task_summary", "Completed core memory source implementations")
    orchestrator.save("spec_review", "Reviewed MemChorus-Spec.md thoroughly")
    
    print("   ✅ Memories saved successfully")
    
    print("\n2. Retrieving memories...")
    # Retrieve some memories
    task_summary = orchestrator.retrieve("task_summary")
    project_status = orchestrator.retrieve("project_status")
    
    print(f"   Task Summary: {task_summary}")
    print(f"   Project Status: {project_status}")
    
    print("\n3. Searching memories...")
    # Search for related content
    results = orchestrator.search("MemChorus", limit=5)
    print(f"   Found {len(results)} matching memories")
    
    print("\n4. Getting system information...")
    # Get orchestrator info
    info = orchestrator.get_orchestrator_info()
    print(f"   Orchestrator: {info['orchestrator']['name']}")
    print(f"   Available sources: {info['orchestrator']['available_sources']}")
    
    print("\n🎉 MemChorus example usage completed successfully!")

if __name__ == "__main__":
    main()