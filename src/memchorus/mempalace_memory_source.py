"""
MemPalace Memory Source Adapter

This implementation provides integration with the MemPalace knowledge graph and diary system.
It serves as the default primary voice for MemChorus.
"""

import os
import json
from typing import List, Dict, Any, Optional
from memchorus.memory_source import MemorySource


class MemPalaceMemorySource(MemorySource):
    """
    Memory source implementation for MemPalace knowledge graph system.
    
    This integrates with the MemPalace MCP server to provide access to the 
    persistent knowledge graph and diary system.
    """

    def __init__(self, name: str = "mempalace", config: Optional[Dict[str, Any]] = None):
        """
        Initialize the MemPalace memory source.
        
        Args:
            name (str): Unique identifier for this memory source
            config (Dict[str, Any], optional): Configuration parameters for this source
        """
        super().__init__(name, config)
        self._name = name  # Store as private attribute to avoid access issues
        self.config = config or {}
        self._mcp_server_url = self.config.get('mcp_server_url', 'http://localhost:8000')
        self._initialize_mcp_client()
    
    def _initialize_mcp_client(self):
        """Initialize the MCP client connection."""
        # Placeholder for actual MCP integration
        # This would establish a connection to the MemPalace MCP server in a real implementation
        pass
    
    def save(self, key: str, value: Any) -> bool:
        """
        Save a memory to MemPalace.
        
        Args:
            key (str): Unique identifier for the memory
            value (Any): The memory content to store
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # In a real implementation, this would send data to the MemPalace MCP server
            # For now we maintain the fallback local storage approach from the original design
            mempalace_dir = os.path.expanduser('~/.hermes/mempalace_cache')
            os.makedirs(mempalace_dir, exist_ok=True)
            
            file_path = os.path.join(mempalace_dir, f"{key}.json")
            with open(file_path, 'w') as f:
                json.dump(value, f)
            return True
        except Exception:
            return False
    
    def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a memory from MemPalace.
        
        Args:
            key (str): Unique identifier for the memory
            
        Returns:
            Any: The memory content if found, None otherwise
        """
        try:
            # In a real implementation, this would query the MemPalace MCP server
            # For now we maintain the fallback local storage approach from the original design
            mempalace_dir = os.path.expanduser('~/.hermes/mempalace_cache')
            file_path = os.path.join(mempalace_dir, f"{key}.json")
            
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    return json.load(f)
            return None
        except Exception:
            return None
    
    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search for memories matching a query in MemPalace.
        
        Args:
            query (str): Search query string
            limit (int): Maximum number of results to return
            
        Returns:
            List[Dict[str, Any]]: List of matching memories with metadata
        """
        results = []
        try:
            # In a real implementation, this would query the MemPalace MCP search API
            # For now we simulate by searching in local directory
            mempalace_dir = os.path.expanduser('~/.hermes/mempalace_cache')
            
            if os.path.exists(mempalace_dir):
                for filename in os.listdir(mempalace_dir):
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
        Check if MemPalace memory source is available.
        
        Returns:
            bool: True if the source is available, False otherwise
        """
        try:
            # In a real implementation, this would check actual MCP server connectivity  
            # For now we assume it's available if the cache dir can be accessed (fallback method)
            mempalace_dir = os.path.expanduser('~/.hermes/mempalace_cache')
            return os.path.exists(mempalace_dir) and os.access(mempalace_dir, os.R_OK | os.W_OK)
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
            'type': 'mempalace',
            'available': self.is_available(),
            'mcp_server_url': self._mcp_server_url,
            'description': 'MemPalace knowledge graph system - primary voice'
        }
    
    # Add property to access name (needed for interface compliance)
    @property
    def name(self) -> str:
        """Get the name of this source."""
        return self._name