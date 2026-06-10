"""
Memory Orchestrator

The MemoryOrchestrator is the core component that manages multiple memory sources
and provides intelligent context management for agents.
"""

from typing import List, Dict, Any, Optional
from memchorus.memory_source import MemorySource
from memchorus.hermes_memory_source import HermesDefaultMemorySource
from memchorus.mempalace_memory_source import MemPalaceMemorySource


class MemoryOrchestrator:
    """
    Core orchestrator for managing multiple memory sources in MemChorus.
    
    This orchestrator handles:
    - Registration and management of memory sources
    - Intelligent retrieval decisions based on relevance and efficiency  
    - Optimized storage placement decisions
    - Proactive memory checking before actions
    - Post-action memory saving behavior
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the MemoryOrchestrator.
        
        Args:
            config (Dict[str, Any], optional): Configuration parameters for the orchestrator
        """
        self.config = config or {}
        self.memory_sources: Dict[str, MemorySource] = {}
        self._default_source_name = self.config.get('default_source', 'hermes_default')
        self._initialize_default_sources()
    
    def _initialize_default_sources(self):
        """Initialize the default memory sources."""
        # Add Hermes default as the resilient core
        hermes_source = HermesDefaultMemorySource(
            name='hermes_default',
            config=self.config.get('hermes_default_config', {})
        )
        self.memory_sources['hermes_default'] = hermes_source
        
        # Add MemPalace as the primary voice  
        mempalace_source = MemPalaceMemorySource(
            name='mempalace',
            config=self.config.get('mempalace_config', {})
        )
        self.memory_sources['mempalace'] = mempalace_source
    
    def register_source(self, source: MemorySource) -> bool:
        """
        Register a new memory source with the orchestrator.
        
        Args:
            source (MemorySource): The memory source to register
            
        Returns:
            bool: True if successfully registered, False otherwise
        """
        try:
            self.memory_sources[source.name] = source
            return True
        except Exception:
            return False
    
    def unregister_source(self, source_name: str) -> bool:
        """
        Unregister a memory source from the orchestrator.
        
        Args:
            source_name (str): The name of the memory source to unregister
            
        Returns:
            bool: True if successfully unregistered, False otherwise
        """
        try:
            if source_name in self.memory_sources:
                del self.memory_sources[source_name]
                return True
            return False
        except Exception:
            return False
    
    def save(self, key: str, value: Any, source_name: Optional[str] = None) -> bool:
        """
        Save a memory to the appropriate source.
        
        The orchestrator decides where to save based on:
        - Explicit source specification
        - Source availability and efficiency
        - Memory characteristics
        
        Args:
            key (str): Unique identifier for the memory
            value (Any): The memory content to store
            source_name (str, optional): Specific source to save to
            
        Returns:
            bool: True if successful, False otherwise
        """
        # If specific source is provided, use it
        if source_name and source_name in self.memory_sources:
            return self.memory_sources[source_name].save(key, value)
        
        # Otherwise, save to the most appropriate source
        # For v1.0, we'll save to both sources (redundancy for resilience)
        success_count = 0
        
        # Save to Hermes default memory (resilient core)
        if self.memory_sources.get('hermes_default'):
            if self.memory_sources['hermes_default'].save(key, value):
                success_count += 1
        
        # Save to MemPalace (enhancement voice)
        if self.memory_sources.get('mempalace'):
            if self.memory_sources['mempalace'].save(key, value):
                success_count += 1
                
        return success_count > 0
    
    def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a memory from the most relevant source.
        
        The orchestrator selects the best source based on:
        - Source availability
        - Context relevance (for future enhancements)
        - Efficiency considerations
        
        Args:
            key (str): Unique identifier for the memory
            
        Returns:
            Any: The memory content if found, None otherwise
        """
        # Check sources in order of priority (most resilient first)
        priorities = ['hermes_default', 'mempalace']  # Hermes as core, then MemPalace
        
        for source_name in priorities:
            if source_name in self.memory_sources:
                source = self.memory_sources[source_name]
                if source.is_available():
                    result = source.retrieve(key)
                    if result is not None:
                        return result
        
        return None
    
    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search for memories matching a query across all sources.
        
        Args:
            query (str): Search query string
            limit (int): Maximum number of results to return
            
        Returns:
            List[Dict[str, Any]]: List of matching memories with source metadata
        """
        all_results = []
        
        # Search all available sources
        for source_name, source in self.memory_sources.items():
            if source.is_available():
                try:
                    results = source.search(query, limit)
                    all_results.extend(results)
                    # Adjust limit to preserve total result count
                    limit -= len(results)
                    if limit <= 0:
                        break
                except Exception:
                    continue
        
        # Sort and deduplicate results  
        return self._sort_and_deduplicate_results(all_results)
    
    def _sort_and_deduplicate_results(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Sort and remove duplicate results from search.
        
        Args:
            results (List[Dict[str, Any]]): Raw search results
            
        Returns:
            List[Dict[str, Any]]: Sorted and deduplicated results
        """
        # This would contain more sophisticated sorting and deduplication logic
        # For v1.0, we'll keep it simple
        seen_keys = set()
        unique_results = []
        
        for result in results:
            if result['key'] not in seen_keys:
                seen_keys.add(result['key'])
                unique_results.append(result)
                
        return unique_results
    
    def is_available(self) -> bool:
        """
        Check if the memory orchestrator and any sources are available.
        
        Returns:
            bool: True if at least one source is available, False otherwise
        """
        for source in self.memory_sources.values():
            if source.is_available():
                return True
        return False
    
    def get_orchestrator_info(self) -> Dict[str, Any]:
        """
        Get comprehensive information about the orchestrator and its sources.
        
        Returns:
            Dict[str, Any]: Metadata about the orchestrator and sources
        """
        info = {
            'orchestrator': {
                'name': 'memchorus_orchestrator',
                'version': '1.0.0',
                'default_source': self._default_source_name,
                'available_sources': len([s for s in self.memory_sources.values() if s.is_available()]),
                'total_sources': len(self.memory_sources)
            },
            'sources': {}
        }
        
        for name, source in self.memory_sources.items():
            info['sources'][name] = source.get_source_info()
            
        return info
