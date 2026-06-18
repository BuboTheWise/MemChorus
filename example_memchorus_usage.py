#!/usr/bin/env python3
"""
MemChorus v1.0 usable demo.

Demonstrates value for our Hermes system:
- Stores/retrieves project structure rules and task facts.
- Uses both voices.
- Can be called from agents to improve recall.

Run: python example_memchorus_usage.py
"""
import sys
sys.path.insert(0, 'src')
from memchorus.orchestrator import MemoryOrchestrator

def main():
    orch = MemoryOrchestrator()
    print("MemChorus Orchestrator ready")
    print("Sources:", list(orch.memory_sources.keys()))

    # Store something useful for our system (project organization rules)
    rules = {
        "project": "MemChorus",
        "code_location": "~/.hermes/workspace/Code/MemChorus/",
        "docs_location": "~/.hermes/workspace/Bubo_Wisdom/Projects/MemChorus/",
        "status": "level-set complete - importable and functional",
        "note": "Use this to recall structure without bleed"
    }
    orch.save("project_structure_rules", rules)

    # Store a sample task fact
    orch.save("current_task_example", {
        "task": "make memchorus usable for hermes improvement",
        "assignee": "cthugha",
        "status": "in progress after level-set"
    })

    retrieved = orch.retrieve("project_structure_rules")
    print("\nRetrieved project structure rules:")
    print(retrieved)

    search_results = orch.search("MemChorus")
    print("\nSearch results for 'MemChorus':", len(search_results))

    print("\n*** MemChorus v1.0 is now usable for improving our system ***")

if __name__ == "__main__":
    main()
