"""
Hermes Default Memory Source

This implementation provides integration with the default Hermes memory system (local curated memory files).
It serves as the resilient core that must remain functional even if other voices are unavailable.
"""

import difflib as _difflib
import json
import os
import re as _re
import datetime
from typing import List, Dict, Any, Optional
from memchorus.memory_source import MemorySource


class HermesDefaultMemorySource(MemorySource):
    """
    Memory source implementation for Hermes default memory system.

    This provides integration with the local curated memory files such as MEMORY.md,
    USER.md, and session context that form the resilient core of MemChorus.
    """

    # Minimum score threshold — results below this floor are filtered out before
    # being returned to the orchestrator so that low-confidence noise is suppressed.
    MIN_RECALL_SCORE = 1.5

    def __init__(self, name: str = "hermes_default", config: Optional[Dict[str, Any]] = None):
        """
        Initialize the Hermes default memory source.

        Args:
            name (str): Unique identifier for this memory source
            config (Dict[str, Any], optional): Configuration parameters for this source.
              Overrides:\n                min_recall_score – override MIN_RECALL_SCORE at runtime
        """
        super().__init__(name, config)
        self._name = name  # Store as private attribute to avoid access issues
        self.config = config or {}
        self._initialize_memory_directory()

    def _initialize_memory_directory(self):
        """Initialize the memory storage directory."""
        self.memory_dir = self.config.get('memory_dir', os.path.expanduser('~/.hermes/memories'))
        os.makedirs(self.memory_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Content matching / scoring helpers
    # ------------------------------------------------------------------

    def _content_matches(self, query: str, content_text: str) -> float:
        """Score how well *query* matches *content_text*.

        Algorithm:
          1. Split the query into individual terms (lowercased).
          2. For each term, count substring occurrences in the content text.
             Each distinct term found contributes to the score (substring
             matching is deliberately permissive — 'fix' does match 'suffix').
          3. Bonus: add +0.5 for every extra occurrence beyond the first per term
             (term frequency bonus, capped at +2 bonus to avoid runaway scoring).
          4. Self-match penalty: if the content is essentially identical to
             the query (SequenceMatcher ratio > 0.9), halve the final score
             so that query-echo artifacts don't dominate results.

        Args:
            query: The search query string.
            content_text: Plain-text representation of the memory content.

        Returns:
            float: Relevance score >= 0.  Zero means no match terms found.
        """
        q_lower = query.lower()
        c_lower = content_text.lower()
        terms = [t for t in q_lower.split() if len(t) > 1]
        if not terms:
            return 0.0

        score = 0.0
        for term in terms:
            count = c_lower.count(term)
            if count > 0:
                score += 2.0 + min(count - 1, 4) * 0.5

        # Self-match / query-echo penalty
        ratio = _difflib.SequenceMatcher(None, q_lower, c_lower).ratio()
        if ratio > 0.9:
            score *= 0.5

        return score

    def _effective_min_score(self) -> float:
        """Return the effective minimum recall score from config override."""
        return self.config.get('min_recall_score', self.MIN_RECALL_SCORE)

    @staticmethod
    def _safe_key(key: str) -> str:
        """Sanitize a memory key for use as a filename.

        Strips path separators and normalizes to alphanumerics plus hyphens.
        Prevents path traversal (../../etc/passwd) and ensures only flat files
        land inside the memory directory. Mirrors what MemPalace does in
        _key_to_room().
        """
        sanitized = key.lower().strip()
        sanitized = _re.sub(r'[^a-z0-9\ -\-]', '-', sanitized)
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
        """Scored search for memories matching a query.

        For each .json file in memory_dir the method:
          1. Scores the filename (key) against the query via _content_matches.
          2. Reads and scores the extracted content text against the query.
          3. Uses the maximum of the two as the file's score.

        Results scoring < MIN_RECALL_SCORE are dropped to suppress low-confidence
        noise. The remaining results are sorted by score descending and trimmed
        to *limit*.

        Args:
            query (str): Search query string
            limit (int): Maximum number of results to return

        Returns:
            List[Dict[str, Any]]: Ranked matching memories with metadata
        """
        candidates: List[Dict[str, Any]] = []
        min_score = self._effective_min_score()

        try:
            filenames = sorted(os.listdir(self.memory_dir))
            for filename in filenames:
                if not filename.endswith('.json'):
                    continue

                key_name = filename[:-5]  # Remove .json extension
                file_path = os.path.join(self.memory_dir, filename)

                # --- Score the key (filename) ---
                key_score = self._content_matches(query, key_name)

                # --- Read & score content ---
                raw = None
                content_text = ''
                try:
                    with open(file_path, 'r') as f:
                        raw = json.load(f)
                    content_text = self._content_to_search_text(raw).lower()
                except Exception:
                    # Corrupt or unreadable file — skip content scoring
                    pass

                content_score = self._content_matches(query, content_text)
                score = max(key_score, content_score)

                if score < min_score:
                    continue

                # Resolve timestamp & actual content for the result dict
                content_val = raw if raw is not None else self.retrieve(key_name)
                if content_val is None:
                    continue

                try:
                    mtime = os.path.getmtime(file_path)
                    ts = datetime.datetime.fromtimestamp(
                        mtime, tz=datetime.timezone.utc
                    ).isoformat()
                except Exception:
                    ts = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

                candidates.append({
                    'key': key_name,
                    'content': content_val,
                    'source': self._name,
                    'timestamp': ts,
                    '_score': score,       # internal — stripped before return
                })

            # Rank by score descending
            candidates.sort(key=lambda r: r['_score'], reverse=True)

        except Exception:
            pass

        # Trim to limit and strip the internal _score field
        results = []
        for c in candidates[:limit]:
            c.pop('_score', None)
            results.append(c)

        return results

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

                # Phase 3 fix — bounded rotation to prevent unbounded disk growth.
                # Keep at most self._proactive_action_max files per source (default 50).
                # Evict the oldest first by modification time.
                self._cleanup_action_logs()
            except Exception:
                pass  # Quiet failure on additional logging - core save is what matters

        return success

    @property
    def _proactive_action_max(self) -> int:
        """Configurable cap on proactive action log files per source (default 50)."""
        return self.config.get('proactive_action_max', 50)

    # ------------------------------------------------------------------
    # Bounded rotation for proactive action logs (§3 Phase fix)
    # ------------------------------------------------------------------

    def _cleanup_action_logs(self, force: bool = False) -> None:
        """Evict oldest action_*.json files when count exceeds the cap.

        Only scans files inside ``self.memory_dir`` that match the pattern
        ``action_*.json``.  The oldest N files (by filesystem modification
        time) are removed until the count drops to ``_proactive_action_max``.

        Args:
            force: If True, run cleanup even when currently under the cap
                   (useful for sweep/lifecycle callbacks). Normally called
                   after each new action log is written so the excess is
                   always just one over the cap.
        """
        try:
            import glob as _glob_mod
            # _safe_key() converts underscores to hyphens, so action logs
            # are written as "action-...timestamp.json".  Use a dash here
            # so the glob actually matches the real files.
            pattern = os.path.join(self.memory_dir, "action-*.json")
            files = _glob_mod.glob(pattern)

            if not files:
                return

            # Already under control? Skip (unless forced by lifecycle sweep).
            limit = self._proactive_action_max
            if len(files) <= limit and not force:
                return

            # Sort oldest-first by mtime so we evict the eldest entries
            files.sort(key=lambda p: os.path.getmtime(p))
            excess = len(files) - limit
            for fp in files[:excess]:
                try:
                    os.remove(fp)
                except OSError:
                    pass

        except Exception:
            # Cleanup failure should never block the primary save path
            pass

    # Add property to access name (needed for interface compliance)
    @property
    def name(self) -> str:
        """Get the name of this source."""
        return self._name
