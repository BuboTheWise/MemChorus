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

    def _content_matches(self, content: Any, query_parts: List[str]) -> float:
        """Score how well *content* matches a set of query parts.

        Searches string values, dict keys/values, and list elements.
        Returns a relevance score (0.0 = no match).  Higher is better.

        Scoring weights:
            filename-only key match          3.0  (strongest signal)
            content value exact phrase       1.5
            each matched query part in text  1.0
            case-sensitive bonus            +0.5 per word
        """
        # Normalize the memory value to a single searchable string.
        if isinstance(content, str):
            pool = content
        elif isinstance(content, (dict, list)):
            import json as _json
            try:
                pool = _json.dumps(content)
            except Exception:
                pool = str(content)
        else:
            pool = str(content)

        pool_lower = pool.lower()
        matches = 0
        for part in query_parts:
            if part in pool_lower:
                matches += 1
            elif _re.search(rf'\b{_re.escape(part)}\b', pool_lower):
                matches += 0.5  # partial (whitespace-bounded) match counts less

        case_bonus = sum(1 for part in query_parts if part and len(part) > 2 and part in pool)
        return matches + 0.5 * case_bonus

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search memories by **actual file content** (not just filenames).

        Reads every ``.json`` file in ``memory_dir``, deserializes it, and checks
        whether any token from *query* appears inside the stored value -- strings,
        dict keys/values, or list elements.  Filename key matches still get a bonus
        so that exact-key queries remain fast.

        Returns up to *limit* results sorted by descending relevance score.
        """
        query_parts = [p for p in query.lower().split() if len(p) > 1]
        if not query_parts:
            return []

        results: List[Dict[str, Any]] = []
        try:
            filenames = os.listdir(self.memory_dir)
        except Exception:
            return []

        for filename in filenames:
            if not filename.endswith('.json'):
                continue

            key_name = filename[:-5]  # strip .json extension
            fpath = os.path.join(self.memory_dir, filename)

            # --- Fast path: does the KEY already look relevant? ---
            key_score = 0.0
            safe_key_norm = self._safe_key(key_name).lower()
            for part in query_parts:
                if part in safe_key_norm:
                    key_score += 3.0  # strong signal

            try:
                with open(fpath, 'r', errors='replace') as f:
                    raw = f.read()

                try:
                    content = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    # Treat non-JSON content as a plain string
                    content = raw[:4096]  # cap to avoid huge blobs

                score = self._content_matches(content, query_parts) + key_score
                if score <= 0:
                    continue  # no signal at all — skip

                # Modification time for relevance scoring
                try:
                    mtime = os.path.getmtime(fpath)
                    ts = datetime.datetime.fromtimestamp(
                        mtime, tz=datetime.timezone.utc).isoformat()
                except Exception:
                    ts = datetime.datetime.now(
                        tz=datetime.timezone.utc).isoformat()

                results.append({
                    'key': key_name,
                    'content': content,
                    'source': self._name,
                    'timestamp': ts,
                    'score': max(score, 0.1),
                })

            except Exception:
                continue  # skip unreadable files gracefully

        # Sort by score descending, then take top N
        results.sort(key=lambda r: r.get('score', 0.0), reverse=True)
        return results[:limit]

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

    def delete(self, key: str) -> bool:
        """Remove a memory identified by *key*.

        Tries the safe-key normalized path first (what ``save()`` writes to),
        then falls back to the raw key name for pre-normalized files.
        Returns ``True`` if at least one file was removed, ``False`` otherwise.
        """
        deleted = False
        try:
            # Primary: safe-key normalized path (what save() uses)
            safe_path = os.path.join(self.memory_dir, f"{self._safe_key(key)}.json")
            if os.path.exists(safe_path):
                os.remove(safe_path)
                deleted = True

            # Fallback: raw key name (pre-placed files may not be normalized)
            raw_path = os.path.join(self.memory_dir, f"{key}.json")
            if os.path.exists(raw_path) and not deleted:
                os.remove(raw_path)
                deleted = True
        except Exception:
            pass
        return deleted

    # Add property to access name (needed for interface compliance)
    @property
    def name(self) -> str:
        """Get the name of this source."""
        return self._name