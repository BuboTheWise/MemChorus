"""
Abstract Memory Source Interface

This interface defines the contract for memory sources that MemChorus can integrate with.
All concrete implementations must implement these methods.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class MemorySource(ABC):
    """
    Abstract base class for all memory sources in MemChorus.
    
    A MemorySource represents a backend where memories can be stored and retrieved.
    Each source has a unique identifier and provides methods for:
    - Storing memories
    - Retrieving memories
    - Checking availability
    - Managing source configuration
    """
    
    @abstractmethod
    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the memory source.
        
        Args:
            name (str): Unique identifier for this memory source
            config (Dict[str, Any], optional): Configuration parameters for this source
        """
        pass
    
    @abstractmethod
    def save(self, key: str, value: Any) -> bool:
        """
        Save a memory to this source.
        
        Args:
            key (str): Unique identifier for the memory
            value (Any): The memory content to store
            
        Returns:
            bool: True if successful, False otherwise
        """
        pass
    
    @abstractmethod
    def retrieve(self, key: str) -> Optional[Any]:
        """
        Retrieve a memory from this source.
        
        Args:
            key (str): Unique identifier for the memory
            
        Returns:
            Any: The memory content if found, None otherwise
        """
        pass
    
    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search for memories matching a query.
        
        Args:
            query (str): Search query string
            limit (int): Maximum number of results to return
            
        Returns:
            List[Dict[str, Any]]: List of matching memories with metadata
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if this memory source is available and functioning.
        
        Returns:
            bool: True if the source is available, False otherwise
        """
        pass
    
    @abstractmethod
    def get_source_info(self) -> Dict[str, Any]:
        """
        Get information about this memory source.
        
        Returns:
            Dict[str, Any]: Metadata about this source
        """
        pass

    @abstractmethod
    def proactive_check(
        self, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Proactively discover memories relevant to the current decision/action.

        Spec §Triggered behavior mandates that hooks must be automatically invoked
        — not merely exist on paper. This method lets every registered source
        contribute context before an action fires, making proactive recall
        chorus-wide rather than being hardcoded to a single voice.

        Args:
            context (Dict[str, Any], optional): Context about the pending action

        Returns:
            Dict[str, Any]: Relevant memories or recommendations
        """
        pass

    @abstractmethod
    def proactive_save(
        self,
        key: str,
        value: Any,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Proactively persist a memory immediately after an action/decision completes.

        Pairs with ``proactive_check`` to close the triggered-behaviour loop:
        recall relevant context → perform action → capture outcome.

        Args:
            key (str): Unique identifier for the memory
            value (Any): The memory content to store
            context (Dict[str, Any], optional): Context about what was decided/done

        Returns:
            bool: True if successful, False otherwise
        """
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove a single memory identified by *key* from this source.

        Used by ``consolidate_key()`` and the EvictionEngine lifecycle sweep.
        Returns ``True`` if the entry was found and removed, ``False`` otherwise.

        Args:
            key (str): Unique identifier for the memory to delete

        Returns:
            bool: True if deleted, False if not found or error occurred
        """
        pass
