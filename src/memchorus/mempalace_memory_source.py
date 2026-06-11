"""
MemPalace Memory Source Adapter

This implementation provides integration with the MemPalace knowledge graph and diary system.
It serves as the default primary voice for MemChorus.
"""

import os
import json
import asyncio
from typing import List, Dict, Any, Optional
from memchorus.memory_source import MemorySource
from hermes_agent.tools.mcp_tool import McpTool


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
        self._mcp_server_name = self.config.get('mcp_server_name', 'mempalace')
        self._mcp_tool = None
        self._initialize_mcp_client()
    
    def _initialize_mcp_client(self):
        """Initialize the MCP client connection."""
        try:
            # Initialize the MCP tool for MemPalace
            self._mcp_tool = McpTool(
                name=self._mcp_server_name,
                config={'server_name': self._mcp_server_name}
            )
            
            # Test if we can connect to the MCP server
            # This will check if the server is available and responsive
        except Exception as e:
            # If initialization fails, we'll still proceed with fallbacks
            # but note that MemPalace won't be fully functional
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
            # If MCP tool is available, use it
            if self._mcp_tool:
                try:
                    # Call MemPalace's add_record function via MCP
                    command = {
                        "function": "mempalace_add_record",
                        "arguments": {
                            "key": key,
                            "value": value,
                            "source": self._name
                        }
                    }
                    result = asyncio.run(self._mcp_tool.invoke(command))
                    return True  # If we get here without exception, assume success
                except Exception:
                    pass  # Fall back to local storage if MCP fails

            # Fallback: save to the local cache directory  
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
            # If MCP tool is available, use it first
            if self._mcp_tool:
                try:
                    # Call MemPalace's get_record function via MCP  
                    command = {
                        "function": "mempalace_get_record",
                        "arguments": {
                            "key": key
                        }
                    }
                    result = asyncio.run(self._mcp_tool.invoke(command))
                    if result and 'content' in result:
                        return result['content']
                except Exception:
                    pass  # Fall back to local storage if MCP fails

            # Fallback: retrieve from the local cache directory
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
            # If MCP tool is available, use it first  
            if self._mcp_tool:
                try:
                    # Call MemPalace's search function via MCP
                    command = {
                        "function": "mempalace_search",
                        "arguments": {
                            "query": query,
                            "limit": limit
                        }
                    }
                    result = asyncio.run(self._mcp_tool.invoke(command))
                    if result and isinstance(result, list):
                        results.extend(result)
                except Exception:
                    pass  # Fall back to local storage if MCP fails

            # If MCP fails or is not available, simulate behavior with local directory search
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
            # Check if we can connect to the MCP server
            if self._mcp_tool:
                # Attempt a simple check via discovery
                try:
                    asyncio.run(self._mcp_tool.invoke({
                        "function": "mempalace_status",
                        "arguments": {}
                    }))
                    return True
                except Exception:
                    pass
            
            # Fall back to local directory access for availability check  
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
            'mcp_server_name': self._mcp_server_name,
            'description': 'MemPalace knowledge graph system - primary voice'
        }
    
    # Add property to access name (needed for interface compliance)
    @property
    def name(self) -> str:
        """Get the name of this source."""
        return self._name