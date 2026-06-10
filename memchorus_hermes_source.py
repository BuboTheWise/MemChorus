#!/usr/bin/env python3
"""
Implementation of HermesDefaultMemorySource for MemChorus
This file provides a complete implementation that integrates with Hermes local memory files.
"""

import logging
import os
import json
from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod
import datetime

# Logger setup
logger = logging.getLogger(__name__)

class MemorySource(ABC):
    """
    Abstract base class for memory sources that MemChorus can orchestrate.
    """
    
    def __init__(self, name: str):
        self.name = name
    
    @abstractmethod
    def fetch(self, query: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Fetch memory items matching the query or all items if no query.
        
        Args:
            query: Search query for relevant memories
            limit: Maximum number of items to return
            
        Returns:
            List of memory items with their metadata
        """
        pass
    
    @abstractmethod
    def save(self, item: Dict[str, Any]) -> bool:
        """
        Save a memory item to this source.
        
        Args:
            item: Memory item to save
            
        Returns:
            True if successful, False otherwise
        """
        pass
    
    @abstractmethod
    def list_sources(self) -> List[Dict[str, Any]]:
        """
        List available sources in this memory system.
        
        Returns:
            List of source information
        """
        pass
    
    @abstractmethod
    def get_source_info(self) -> Dict[str, Any]:
        """
        Get detailed information about this source.
        
        Returns:
            Dictionary with source configuration and metadata
        """
        pass


class HermesDefaultMemorySource(MemorySource):
    """
    Memory source for Hermes default memory system.
    This interfaces with local file-based memory storage (MEMORY.md, USER.md).
    """

    def __init__(self):
        super().__init__("hermes_builtin")
        self._initialized = False
        self._memory_dir = None
        
    def initialize(self, config: Optional[Dict] = None) -> None:
        """
        Initialize the Hermes default memory source.
        
        Args:
            config: Configuration parameters for this source
        """
        # Determine the Hermes memory directory
        if config and 'memory_dir' in config:
            self._memory_dir = config['memory_dir']
        else:
            # Default to standard Hermes memory directory 
            self._memory_dir = "/home/bubo/.hermes/memories"
            
        # Ensure the directory exists
        if not os.path.exists(self._memory_dir):
            logger.warning(f"Memory directory {self._memory_dir} does not exist, creating it")
            try:
                os.makedirs(self._memory_dir, exist_ok=True)
            except Exception as e:
                logger.error(f"Failed to create memory directory: {e}")
                raise RuntimeError(f"Cannot initialize HermesDefaultMemorySource: {e}")
                
        # Check that required memory files exist and are readable
        memory_file = os.path.join(self._memory_dir, "MEMORY.md")
        user_file = os.path.join(self._memory_dir, "USER.md")
        
        if not os.path.exists(memory_file):
            # Create empty file if it doesn't exist
            try:
                with open(memory_file, 'w') as f:
                    f.write("")
                logger.info("Created default MEMORY.md file")
            except Exception as e:
                logger.error(f"Failed to create MEMORY.md: {e}")
        
        if not os.path.exists(user_file):
            # Create empty file if it doesn't exist
            try:
                with open(user_file, 'w') as f:
                    f.write("")
                logger.info("Created default USER.md file")
            except Exception as e:
                logger.error(f"Failed to create USER.md: {e}")
        
        logger.info(f"Initializing Hermes default memory source at {self._memory_dir}")
        self._initialized = True
        
    def fetch(self, query: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Fetch memories from Hermes default memory.
        
        Args:
            query: Search query
            limit: Maximum results
            
        Returns:
            List of matching memories
        """
        if not self._initialized:
            raise RuntimeError("HermesDefaultMemorySource not initialized")
            
        result = []
        
        try:
            # Read the main memory file (MEMORY.md)
            memory_file = os.path.join(self._memory_dir, "MEMORY.md")
            user_file = os.path.join(self._memory_dir, "USER.md")
            
            # Read both files
            memory_content = self._read_file(memory_file)
            user_content = self._read_file(user_file)
            
            # Parse the content into entries
            all_entries = []
            
            # Process MEMORY.md entries (main agent memories)
            entries = self._parse_memory_entries(memory_content)
            all_entries.extend(entries)
            
            # Process USER.md entries (user context and preferences) 
            entries = self._parse_memory_entries(user_content)
            all_entries.extend(entries)
            
            # Filter by query if provided
            if query:
                filtered_entries = []
                query_lower = query.lower()
                for entry in all_entries:
                    content = entry.get('content', '').lower() + ' ' + str(entry.get('tags', [])).lower()
                    if query_lower in content or query_lower in str(entry.get('date', '')).lower():
                        filtered_entries.append(entry)
                all_entries = filtered_entries
            
            # Sort by date (newest first) and limit results
            all_entries.sort(key=lambda x: x.get('date', '1970-01-01'), reverse=True)
            
            # Limit to requested number of results
            result = all_entries[:limit]
            
        except Exception as e:
            logger.error(f"Error fetching memories: {e}")
            # Return empty result on error, but still log it for debugging
            
        logger.debug(f"Fetched {len(result)} memories with query: {query}")
        return result
    
    def _read_file(self, filepath: str) -> str:
        """Read a memory file and return its content."""
        try:
            if not os.path.exists(filepath):
                return ""
                
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading file {filepath}: {e}")
            return ""
    
    def _parse_memory_entries(self, content: str) -> List[Dict[str, Any]]:
        """Parse memory content into structured entries."""
        entries = []
        
        if not content.strip():
            return entries
            
        # Split by lines and process
        lines = content.strip().split('\n')
        current_entry = None
        
        for i, line in enumerate(lines):
            if not line.strip():
                continue
                
            # Look for date format or numbered entries
            if line.startswith(('2026-', '2025-', '2024-')) or line.startswith(('-', '—', '•')):
                # New entry - save previous one if exists
                if current_entry:
                    entries.append(current_entry)
                
                # Start new entry
                current_entry = {
                    'content': '',
                    'date': '1970-01-01',  # Default
                    'source_file': 'unknown',
                    'line_number': i + 1,
                    'tags': []
                }
                
                # Try to extract date from the line
                if line.startswith('2026-') or line.startswith('2025-') or line.startswith('2024-'):
                    try:
                        date_part = line.split(':')[0]
                        current_entry['date'] = date_part
                    except Exception:
                        pass
                        
            elif current_entry is not None:
                if current_entry['content'].strip():
                    current_entry['content'] += ' ' + line.strip()
                else:
                    current_entry['content'] = line.strip()
        
        # Add the last entry if exists
        if current_entry:
            entries.append(current_entry)
            
        return entries
    
    def save(self, item: Dict[str, Any]) -> bool:
        """
        Save a memory item to Hermes default memory.
        
        Args:
            item: Memory item to save
            
        Returns:
            True on success
        """
        if not self._initialized:
            raise RuntimeError("HermesDefaultMemorySource not initialized")
            
        try:
            # Generate date for entry
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d")
            
            # Determine which file to write to (MEMORY.md for agent memories, USER.md for user context)
            destination_file = os.path.join(self._memory_dir, "MEMORY.md")
            
            if 'user_context' in item.get('tags', []):
                destination_file = os.path.join(self._memory_dir, "USER.md")
            
            # Format the entry
            content = item.get('content', '')
            formatted_entry = f"{timestamp}: {content}"
            
            # Add tags if present  
            if 'tags' in item and item['tags']:
                formatted_entry += f" [#{', #'.join(item['tags'])}]"
                
            # Write to the file (append)
            with open(destination_file, 'a', encoding='utf-8') as f:
                f.write(f"{formatted_entry}\n\n")
            
            logger.debug(f"Saved memory item to {destination_file}: {content[:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"Error saving memory item: {e}")
            return False
    
    def list_sources(self) -> List[Dict[str, Any]]:
        """
        List available sources in Hermes default system.
        
        Returns:
            List of source information
        """
        return [
            {
                "name": "hermes_builtin",
                "type": "builtin",
                "description": "Local file-based memory storage for Hermes agent",
                "configurable": True,
                "status": "available"
            }
        ]
    
    def get_source_info(self) -> Dict[str, Any]:
        """
        Get detailed information about the Hermes default memory source.
        
        Returns:
            Dictionary with source configuration
        """
        return {
            "name": "hermes_builtin",
            "type": "builtin",
            "description": "Hermes default local memory system",
            "version": "1.0.0",
            "status": "operational" if self._initialized else "uninitialized",
            "memory_directory": self._memory_dir,
            "last_updated": datetime.datetime.now().isoformat()
        }

# Test function to demonstrate usage
def test_hermes_memory_source():
    """Test the HermesDefaultMemorySource implementation."""
    # Initialize source
    source = HermesDefaultMemorySource()
    
    try:
        print("Initializing Hermes memory source...")
        source.initialize({
            'memory_dir': '/home/bubo/.hermes/memories'
        })
        print("Initialization successful")
        
        # Test fetching (will be empty initially)
        print("Fetching memories...")
        memories = source.fetch(limit=5)
        print(f"Found {len(memories)} memories")
        
        # Test saving
        print("Saving test memory...")
        success = source.save({
            'content': 'Test memory entry for MemChorus development',
            'tags': ['memchorus', 'development']
        })
        print(f"Save successful: {success}")
        
        # Fetch again to verify
        print("Fetching memories after save...")
        memories = source.fetch(limit=5)
        print(f"Found {len(memories)} memories")
        
    except Exception as e:
        print(f"Test failed with error: {e}")

if __name__ == "__main__":
    test_hermes_memory_source()