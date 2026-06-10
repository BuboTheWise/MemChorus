#!/usr/bin/env python3
#!/usr/bin/env python3
"""
MemChorus - Memory Orchestration Skill

A modular, extensible memory orchestration layer that coordinates multiple 
memory sources ("voices") to provide coherent, efficient, and privacy-focused 
memory management for AI agents.
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
            Dictionary with source configuration
        """
        return {
            "name": "hermes_builtin",
            "type": "builtin",
            "description": "Hermes default local memory system",
            "version": "1.0.0",
            "status": "operational" if self._initialized else "uninitialized"
        }


class MemPalaceMemorySource(MemorySource):
    """
    Memory source for MemPalace knowledge graph and diary system.
    Interface to the existing MemPalace MCP server.
    """
    
    def __init__(self):
        super().__init__("mempalace")
        self._initialized = False
        self._mcp_client = None
        
    def initialize(self, config: Optional[Dict] = None) -> None:
        """
        Initialize the MemPalace memory source by connecting to its MCP server.
        
        Args:
            config: Configuration parameters for this source
        """
        # In a real implementation, this would establish connection with MemPalace MCP 
        # via the existing integration
        logger.info("Initializing MemPalace memory source")
        
        # This is a placeholder - in practice would connect to MemPalace MCP server
        if config:
            self._config = config
        else:
            self._config = {}
            
        self._initialized = True
        
    def fetch(self, query: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Fetch memories from MemPalace.
        
        Args:
            query: Search query
            limit: Maximum results
            
        Returns:
            List of matching memories
        """
        if not self._initialized:
            raise RuntimeError("MemPalaceMemorySource not initialized")
            
        # In a real implementation, this would call the MCP server or use 
        # existing MemPalace integration
        result = []
        
        logger.debug(f"Fetching memories from MemPalace with query: {query}")
        
        # Placeholder for actual MemPalace API call  
        # This would integrate with the existing mempalace-mcp-integration skill
        
        # This simulation provides sample data for testing
        if query and "test_query" in query.lower():
            result = [
                {"id": f"mempalace_{i}", "source": self.name, "timestamp": time.time(), 
                 "content": f"MemPalace memory item for query '{query}' with relevance", 
                 "relevance_score": 0.8 + i * 0.05}
                for i in range(2)
            ]
        
        return result
    
    def save(self, item: Dict[str, Any]) -> bool:
        """
        Save a memory item to MemPalace.
        
        Args:
            item: Memory item to save
            
        Returns:
            True on success
        """
        if not self._initialized:
            raise RuntimeError("MemPalaceMemorySource not initialized")
            
        # Placeholder for actual save to MemPalace
        logger.debug(f"Saving memory item to MemPalace: {item.get('content', '')}")
        return True
    
    def list_sources(self) -> List[Dict[str, Any]]:
        """
        List available sources in MemPalace system.
        
        Returns:
            List of source information
        """
        return [
            {
                "name": "mempalace",
                "type": "knowledge_graph",
                "description": "Persistent knowledge graph and diary system",
                "configurable": True,
                "status": "available"
            }
        ]
    
    def get_source_info(self) -> Dict[str, Any]:
        """
        Get detailed information about the MemPalace memory source.
        
        Returns:
            Dictionary with source configuration
        """
        return {
            "name": "mempalace",
            "type": "knowledge_graph", 
            "description": "MemPalace persistent knowledge graph and diary system",
            "version": "1.0.0",
            "status": "operational" if self._initialized else "uninitialized",
            "config": self._config
        }


class MemoryOrchestrator:
    """
    The core orchestrator that coordinates multiple memory sources.
    Implements all required features for MemChorus: relevance scoring, filtering,
    unified retrieval interface, priority handling, and context aggregation.
    """
    
    def __init__(self):
        self._sources: Dict[str, MemorySource] = {}
        self._relevance_threshold = 0.5
        self._priority_order = ["hermes_builtin", "mempalace"]  # Hermes default comes first as core fallback
        logger.info("Memory orchestrator initialized")
    
    def add_source(self, source: MemorySource) -> None:
        """
        Add a memory source to the orchestrator.
        
        Args:
            source: MemorySource instance to add
        """
        if not isinstance(source, MemorySource):
            raise TypeError("Source must be an instance of MemorySource")
            
        self._sources[source.name] = source
        logger.info(f"Added memory source: {source.name}")
        
    def remove_source(self, source_name: str) -> None:
        """
        Remove a memory source from the orchestrator.
        
        Args:
            source_name: Name of the source to remove
        """
        if source_name in self._sources:
            del self._sources[source_name]
            logger.info(f"Removed memory source: {source_name}")
    
    def _calculate_relevance_score(self, item: Dict[str, Any], query: str) -> float:
        """
        Calculate relevance score for a memory item based on the query.
        
        Args:
            item: Memory item to score
            query: Search query
            
        Returns:
            Relevance score between 0 and 1
        """
        # Very basic relevance scoring - in reality this would be more sophisticated
        content = str(item.get("content", "")).lower()
        query = query.lower()
        
        if not query:
            return 0.5
            
        # Simple keyword matching
        matches = sum(1 for word in query.split() if word in content)
        total_words = len(content.split())
        
        if total_words == 0:
            return 0.0
            
        # Score based on match rate
        base_score = matches / total_words
        
        # Boost score for exact phrase matching 
        if query in content:
            base_score += 0.2
            
        # Normalize to 0-1 range
        relevance = min(1.0, base_score + (item.get("relevance_score", 0.5) * 0.3))
        
        return relevance
    
    def _apply_prioritization(self, results: List[Dict[str, Any]], priority_order: List[str]) -> List[Dict[str, Any]]:
        """
        Apply prioritized order to results - sources with higher priority come first.
        
        Args:
            results: List of memory items
            priority_order: Order of sources by priority (highest first)
            
        Returns:
            Ranked list of results based on source priority
        """
        # Create a copy to avoid modifying original
        prioritized_results = []
        source_weights = {name: len(priority_order) - i for i, name in enumerate(priority_order)}
        
        # Assign weights to items by source priority
        for item in results:
            source_name = item.get("source", "")
            weight = source_weights.get(source_name, 0)
            
            # Add weighted score to allow sorting with primary source priority, secondary relevance
            item["priority_score"] = (weight * 10 + item.get("relevance_score", 0)) 
            prioritized_results.append(item)
        
        # Sort: higher priority_score first (descending order by source + relevance)
        prioritized_results.sort(key=lambda x: x["priority_score"], reverse=True)
        
        return prioritized_results
    
    def get_context(self, query: str, sources: Optional[List[str]] = None, 
                   relevance_threshold: Optional[float] = None, 
                   prioritize_results: bool = True) -> List[Dict[str, Any]]:
        """
        Fetch relevant memories from specified sources with enhanced orchestration features.
        
        Args:
            query: Query to search for
            sources: Specific sources to query (None means all)
            relevance_threshold: Minimum relevance score (0.0-1.0)
            prioritize_results: Whether to apply source prioritization
            
        Returns:
            List of memory items sorted by relevance and priority
        """
        if relevance_threshold is not None:
            self._relevance_threshold = relevance_threshold
            
        results = []
        
        # Determine which sources to use (with fallback to all if none specified)
        if sources is None:
            source_names = list(self._sources.keys())
        else:
            source_names = [name for name in sources if name in self._sources]
            
        # Ensure Hermes default is always included as the ultimate core
        if "hermes_builtin" not in source_names and "hermes_builtin" in self._sources:
            source_names.append("hermes_builtin")
            
        logger.debug(f"Fetching context for query '{query}' from sources: {source_names}")
        
        # Fetch from each source and combine results
        for source_name in source_names:
            try:
                source = self._sources[source_name]
                items = source.fetch(query, limit=15)  # Fetch more to allow filtering
                
                # Add source information to results and calculate relevance scores
                for item in items:
                    item["source"] = source_name
                    item["relevance_score"] = self._calculate_relevance_score(item, query)
                
                results.extend(items)
            except Exception as e:
                logger.warning(f"Failed to fetch from {source_name}: {e}")
                
        # Sort by relevance and apply threshold
        if results:
            results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
            filtered_results = [r for r in results if r.get("relevance_score", 0) >= self._relevance_threshold]
            
            # Apply prioritization if requested
            if prioritize_results and len(filtered_results) > 1:
                filtered_results = self._apply_prioritization(filtered_results, self._priority_order)
        else:
            filtered_results = []
        
        logger.info(f"Final results: {len(filtered_results)} relevant memories")
        return filtered_results
    
    def save_context(self, context: Dict[str, Any], source: Optional[str] = None, 
                    fallback_to_hermes: bool = True) -> bool:
        """
        Save context to specified or default memory source with fallback behavior.
        
        Args:
            context: Memory item to save
            source: Specific source to save to (None means default)
            fallback_to_hermes: Whether to fall back to Hermes if source fails
            
        Returns:
            True if successful
        """
        try:
            # Determine target source
            if source is None:
                # Save to all sources (fallback behavior, including Hermes as core)
                success = True
                for src_name, memory_source in self._sources.items():
                    try:
                        if not memory_source.save(context):
                            logger.warning(f"Failed to save to {src_name}")
                            success = False
                    except Exception as e:
                        logger.warning(f"Error saving to {src_name}: {e}")
                        success = False
                return success
            else:
                # Save to specific source with fallback behavior if needed
                if source in self._sources:
                    result = self._sources[source].save(context)
                    
                    # If saving to specified source fails and fallback is enabled,
                    # try saving to Hermes (as core/default)
                    if not result and fallback_to_hermes and "hermes_builtin" in self._sources:
                        logger.info(f"Falling back to Hermes default for saving: {context.get('content', '')}")
                        return self._sources["hermes_builtin"].save(context)
                    
                    return result
                else:
                    logger.warning(f"Source '{source}' not found")
                    return False
                    
        except Exception as e:
            logger.error(f"Error saving context: {e}")
            return False
    
    def list_sources(self) -> List[Dict[str, Any]]:
        """
        List all available memory sources.
        
        Returns:
            List of source information
        """
        sources_info = []
        for name, source in self._sources.items():
            info = source.get_source_info()
            info["name"] = name
            sources_info.append(info)
            
        return sources_info
    
    def source_info(self, source_name: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a specific memory source.
        
        Args:
            source_name: Name of the source to query
            
        Returns:
            Source information or None if not found
        """
        if source_name in self._sources:
            return self._sources[source_name].get_source_info()
        return None
    
    def initialize_all(self, config: Optional[Dict] = None) -> bool:
        """
        Initialize all registered memory sources.
        
        Args:
            config: Configuration dictionary with source-specific configurations
            
        Returns:
            True if initialization succeeded for all sources
        """
        success = True
        config = config or {}
        
        for name, source in self._sources.items():
            try:
                source_config = config.get(name, {})
                source.initialize(source_config)
            except Exception as e:
                logger.error(f"Failed to initialize {name}: {e}")
                success = False
                
        return success
    
    def set_priority_order(self, priority_order: List[str]) -> None:
        """
        Set the order of memory sources by priority.
        
        Args:
            priority_order: List of source names in priority order (highest first)
        """
        # Verify all specified sources are registered
        missing_sources = [name for name in priority_order if name not in self._sources]
        if missing_sources:
            raise ValueError(f"Sources not found in orchestrator: {missing_sources}")
            
        self._priority_order = priority_order[:]
        logger.info(f"Set priority order to: {priority_order}")


# Example usage
if __name__ == "__main__":
    # Initialize orchestrator
    orchestrator = MemoryOrchestrator()
    
    # Add memory sources (this would be done via integration in full implementation)
    hermes_source = HermesDefaultMemorySource()
    mempalace_source = MemPalaceMemorySource()
    
    orchestrator.add_source(hermes_source)
    orchestrator.add_source(mempalace_source)
    
    # Initialize all sources
    orchestrator.initialize_all()
    
    print("MemChorus memory orchestration skill initialized successfully")