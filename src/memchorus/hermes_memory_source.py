"""
Hermes Default Memory Source

This implementation provides integration with the default Hermes memory system (local curated memory files).
It serves as the resilient core that must remain functional even if other voices are unavailable.
"""

import os
import json
from typing import List, Dict, Any, Optional
from memchorus.memory_source import MemorySource


class HermesDefaultMemorySource(MemorySource):
    """
    Memory source implementation for Hermes default memory system.
    
    This provides integration with the local curated memory files such as MEMORY.md,
    USER.md, and session context that form the resilient core of MemChorus.
    """

    def __init__(self, name: str = "hermes_default", config: Optional[Dict[str, Any]] = None):
        """
        Initialize the Hermes default memory source.
        
        Args:
            name (str): Unique identifier for this memory source
            config (Dict[str, Any], optional): Configuration parameters for this source
        """
        super().__init__(name, config)
        self._name = name  # Store as private attribute to avoid access issues
        self.config = config or {}
        self._initialize_memory_directory()
    
    def _initialize_memory_directory(self):
        """Initialize the memory storage directory."""
        self.memory_dir = self.config.get('memory_dir', os.path.expanduser('~/.hermes/memories'))
        os.makedirs(self.memory_dir, exist_ok=True)
    
    def save(self, key: str, value: Any) -> bool:
        """
        Save a memory to Hermes default memory storage.
        
        Args:
            key (str): Unique identifier for the memory
            value (Any): The memory content to store
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Convert value to JSON-serializable format
            if not isinstance(value, (str, int, float, bool, dict, list)):
                value = str(value)
                
            file_path = os.path.join(self.memory_dir, f"{key}.json")
            with open(file_path, 'w') as f:
                json.dump(value, f)
            return True
        except Exception:
            return False
    
    def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a memory from Hermes default memory storage.
        
        Args:
            key (str): Unique identifier for the memory
            
        Returns:
            Any: The memory content if found, None otherwise
        """
        try:
            file_path = os.path.join(self.memory_dir, f"{key}.json")
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    return json.load(f)
            return None
        except Exception:
            return None
    
    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search for memories matching a query.
        
        For the Hermes default memory system, this is a simplified search that 
        looks for files matching the query pattern in the memory directory.
        
        Args:
            query (str): Search query string
            limit (int): Maximum number of results to return
            
        Returns:
            List[Dict[str, Any]]: List of matching memories with metadata
        """
        results = []
        try:
            for filename in os.listdir(self.memory_dir):
                if filename.endswith('.json') and query.lower() in filename.lower():
                    key = filename[:-5]  # Remove .json extension
                    content = self.retrieve(key)
                    if content:
                        results.append({
                            'key': key,
                            'content': content,
                            'source': self._name
                        })
                    if len(results) >= limit:
                        break
        except Exception:
            pass
        return results
    
    def is_available(self) -> bool:
        """
        Check if Hermes default memory source is available.
        
        Returns:
            bool: True if the source is available, False otherwise
        """
        try:
            # Simple check - directory should exist and be writable
            return os.path.exists(self.memory_dir) and os.access(self.memory_dir, os.W_OK)
        except Exception:
            return False
    
    def get_source_info(self) -> Dict[str, Any]:
        """
        Get information about this memory source.
        
        Returns:
            Dict[str, Any]: Metadata about this source
        """
        return {
            'name': self._name,
            'type': 'hermes_default',
            'available': self.is_available(),
            'memory_dir': self.memory_dir,
            'description': 'Hermes default memory system - resilient core'
        }
    
    # Add property to access name (needed for interface compliance)
    @property
    def name(self) -> str:
        """Get the name of this source."""
        return self._name
