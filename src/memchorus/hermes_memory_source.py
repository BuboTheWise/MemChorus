"""
Hermes Default Memory Source

This implementation provides integration with the default Hermes memory system (local curated memory files).
It serves as the resilient core that must remain functional even if other voices are unavailable.
"""

import os
import re as _re
import json
import datetime
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

    @staticmethod
    def _safe_key(key: str) -> str:
        """Sanitize a memory key for use as a filename.

        Strips path separators and normalizes to alphanumerics plus hyphens.
        Prevents path traversal (../../etc/passwd) and ensures only flat files
        land inside the memory directory. Mirrors what MemPalace does in
        _key_to_room().
        """
        sanitized = key.lower().strip()
        sanitized = _re.sub(r'[^a-z0-9\-]', '-', sanitized)
        parts = [p for p in sanitized.split('-') if p]
        return '-'.join(parts)[:128]

    def _read_memory_file(self, file_path: str) -> List[Dict[str, Any]]:
        """
        Read memory entries from a file.
        
        Args:
            file_path (str): Path to the memory file
            
        Returns:
            List[Dict[str, Any]]: List of memory entries
        """
        try:
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    content = f.read()
                    
                # Parse the memory content (simple format support)
                entries = []
                for line in content.split('\n'):
                    line = line.strip()
                    if line:
                        # Simple format: date: content [tags]
                        if ':' in line:
                            parts = line.split(':', 1)
                            if len(parts) >= 2:
                                timestamp = parts[0].strip()
                                content_part = parts[1].strip()
                                entries.append({
                                    'timestamp': timestamp,
                                    'content': content_part
                                })
                return entries
            return []
        except Exception:
            return []
            
    def _write_memory_file(self, file_path: str, entries: List[Dict[str, Any]]) -> bool:
        """
        Write memory entries to a file.
        
        Args:
            file_path (str): Path to the memory file
            entries (List[Dict[str, Any]]): List of memory entries to write
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Format entries for simple storage
            formatted_entries = []
            for entry in entries:
                timestamp = entry.get('timestamp', datetime.datetime.now().strftime('%Y-%m-%d'))
                content = entry.get('content', '')
                formatted_entries.append(f"{timestamp}: {content}")
                
            with open(file_path, 'w') as f:
                f.write('\n'.join(formatted_entries))
            return True
        except Exception:
            return False

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
                
            file_path = os.path.join(self.memory_dir, f"{self._safe_key(key)}.json")
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
            # Try safe_key first (normalized for disk safety)
            file_path = os.path.join(self.memory_dir, f"{self._safe_key(key)}.json")
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    return json.load(f)
            # Fallback: try the raw key name (pre-placed files outside save() may not be normalized)
            file_path_raw = os.path.join(self.memory_dir, f"{key}.json")
            if os.path.exists(file_path_raw):
                with open(file_path_raw, 'r') as f:
                    return json.load(f)
            return None
        except Exception:
            return None

    def _content_to_search_text(self, content: Any) -> str:
        """Convert a memory value to searchable text.

        Handles all JSON types (dict, list, str, int, float, bool, None).
        Recursively walks dicts and lists so that nested values are also
        included in the searchable text body.

        Args:
            content: The JSON-deserialized value loaded from a .json file.

        Returns:
            str: Plain-text representation suitable for substring matching.
        """
        if isinstance(content, dict):
            parts = []
            for k, v in content.items():
                parts.append(str(k))
                parts.append(self._content_to_search_text(v))
            return ' '.join(parts)
        elif isinstance(content, list):
            return ' '.join(self._content_to_search_text(item) for item in content)
        else:
            return str(content)

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search for memories matching a query.

        Searches BOTH the filename (key) AND the JSON file content so that
        queries find results even when the key name doesn't contain the
        search terms but the stored value does.

        For each .json file in memory_dir the method:
          1. Checks if any query term appears in the filename (case-insensitive).
          2. If not, opens and reads the JSON content and checks all text
             extracted from that content against every query term.
          A match on either dimension counts as a hit.

        Args:
            query (str): Search query string
            limit (int): Maximum number of results to return

        Returns:
            List[Dict[str, Any]]: List of matching memories with metadata
        """
        results = []
        try:
            # Terms to match — split multi-word queries so each term can
            # independently trigger a hit (e.g. 'routing bug' checks both
            # 'routing' and 'bug').
            terms = query.lower().split()

            # Normalized variants ensure underscores in the lookup term still
            # match hyphen-normalized filenames / keys on disk.
            safe_q = self._safe_key(query).lower()
            safe_terms = [t for t in safe_q.split('-') if t]

            all_variants = terms + ([safe_q] if safe_q not in terms else []) + safe_terms
            all_variants = list(set(all_variants))

            filenames = sorted(os.listdir(self.memory_dir))
            for filename in filenames:
                if not filename.endswith('.json'):
                    continue

                key_name = filename[:-5]  # Remove .json extension
                file_lower = filename.lower()

                # Pass 1: fast — check if any variant appears in the filename
                hit = any(v in file_lower for v in all_variants)

                # Pass 2: content-aware search (only when filename didn't match)
                if not hit:
                    try:
                        file_path = os.path.join(self.memory_dir, filename)
                        with open(file_path, 'r') as f:
                            raw = json.load(f)
                        text = self._content_to_search_text(raw).lower()
                        hit = any(v in text for v in all_variants)
                    except Exception:
                        # Corrupt or unreadable file — skip gracefully
                        hit = False

                if hit:
                    content = self.retrieve(key_name)
                    if content is not None:
                        try:
                            mtime = os.path.getmtime(
                                os.path.join(self.memory_dir, filename)
                            )
                            ts = datetime.datetime.fromtimestamp(
                                mtime, tz=datetime.timezone.utc
                            ).isoformat()
                        except Exception:
                            ts = datetime.datetime.now(
                                tz=datetime.timezone.utc
                            ).isoformat()

                        results.append({
                            'key': key_name,
                            'content': content,
                            'source': self._name,
                            'timestamp': ts
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
            'description': 'Hermes default memory system - resilient core',
            'version': '1.0.1'
        }

    def proactive_check(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Proactively check for relevant memories that might inform decisions.
        
        This is a key method demonstrating the foundational role of Hermes default memory.
        It ensures the core source can effectively work without other voices.
        
        Args:
            context (Dict[str, Any], optional): Context about what's being decided
            
        Returns:
            Dict[str, Any]: Relevant memories or recommendations for action
        """
        # If no context, just return current status  
        if not context:
            return {
                'status': 'ready',
                'found_memories': 0,
                'source': self._name,
                'timestamp': datetime.datetime.now().isoformat()
            }
            
        # Search for memories related to the decision context
        query = ' '.join([str(v) for v in context.values() if v])
        findings = self.search(query, limit=3)
        recommendations = []
        
        # Look for past similar contexts or decisions 
        if query:
            # Simple logic based on what's available
            if len(findings) > 0:
                recommendations.append({
                    'type': 'context_retrieval',
                    'found': len(findings),
                    'memories': [{'key': f['key'], 'content': str(f['content'])} for f in findings]
                })
            
        return {
            'recommendations': recommendations,
            'source': self._name,
            'timestamp': datetime.datetime.now().isoformat(),
            'context_used': context
        }

    def proactive_save(self, key: str, value: Any, context: Optional[Dict[str, Any]] = None) -> bool:
        """
        Proactively save memory after an action or decision.
        
        This is a key method demonstrating the foundational role of Hermes default memory.
        It ensures that decisions and outcomes are reliably stored even without other voices.
        
        Args:
            key (str): Unique identifier for the memory
            value (Any): The memory content to store
            context (Dict[str, Any], optional): Context about what was decided or done
            
        Returns:
            bool: True if successful, False otherwise
        """
        # Save to default source (resilient core)  
        success = self.save(key, value)
        
        # Log the proactive action if needed for tracking 
        if success and context:
            # Create an action log in memory as a demonstration
            action_key = f"action_{key}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
            action_log = {
                'action': 'proactive_save',
                'memory_key': key,
                'context': context, 
                'timestamp': datetime.datetime.now().isoformat(),
                'source': self._name
            }
            # Save the action log in a separate file to track proactive behavior
            try:
                # Sanitize every part of the filename; action_key contains the
                # user-supplied key and must not escape memory_dir.
                safe_action_file = os.path.join(self.memory_dir, f"{self._safe_key(action_key)}.json")
                with open(safe_action_file, 'w') as f:
                    json.dump(action_log, f)
            except Exception:
                pass  # Quiet failure on additional logging - core save is what matters
                
        return success

    # Add property to access name (needed for interface compliance)
    @property
    def name(self) -> str:
        """Get the name of this source."""
        return self._name