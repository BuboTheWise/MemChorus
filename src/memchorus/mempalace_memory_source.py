"""
MemPalace Memory Source Adapter

This implementation provides integration with the MemPalace knowledge graph and diary system.
It serves as the default primary voice for MemChorus.

For v1.0: MCP attempt + local simulation/fallback only.
"""
import os
import json
from typing import List, Dict, Any, Optional
from memchorus.memory_source import MemorySource

class MemPalaceMemorySource(MemorySource):
    def __init__(self, name: str = "mempalace", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self._name = name
        self.config = config or {}
        self._mcp_tool = None  # v1.0 uses simulation/fallback only
        self._initialize()

    def _initialize(self):
        # Placeholder for future real MCP (use shared wrapper at ~/.hermes/skills/mcp/scripts/...)
        pass

    def save(self, key: str, value: Any) -> bool:
        try:
            mempalace_dir = os.path.expanduser('~/.hermes/mempalace_cache')
            os.makedirs(mempalace_dir, exist_ok=True)
            file_path = os.path.join(mempalace_dir, f"{key}.json")
            with open(file_path, 'w') as f:
                json.dump(value, f)
            return True
        except Exception:
            return False

    def retrieve(self, key: str) -> Optional[Any]:
        try:
            mempalace_dir = os.path.expanduser('~/.hermes/mempalace_cache')
            file_path = os.path.join(mempalace_dir, f"{key}.json")
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    return json.load(f)
            return None
        except Exception:
            return None

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        results = []
        try:
            mempalace_dir = os.path.expanduser('~/.hermes/mempalace_cache')
            if os.path.exists(mempalace_dir):
                for filename in os.listdir(mempalace_dir):
                    if filename.endswith('.json') and query.lower() in filename.lower():
                        key = filename[:-5]
                        content = self.retrieve(key)
                        if content:
                            results.append({'key': key, 'content': content, 'source': self._name})
                        if len(results) >= limit:
                            break
        except Exception:
            pass
        return results

    def is_available(self) -> bool:
        try:
            mempalace_dir = os.path.expanduser('~/.hermes/mempalace_cache')
            return os.path.exists(mempalace_dir) and os.access(mempalace_dir, os.R_OK | os.W_OK)
        except Exception:
            return False

    def get_source_info(self) -> Dict[str, Any]:
        return {
            'name': self._name,
            'type': 'mempalace',
            'available': self.is_available(),
            'description': 'MemPalace (simulation/fallback for v1.0)',
            'version': '1.0'
        }

    @property
    def name(self) -> str:
        return self._name
