"""
Session History Memory Source

This implementation provides integration with the Hermes session history database
via FTS5 full-text search. It serves as a fallback recall source that queries past
conversations for relevant context -- mistakes, learnings, decisions, and patterns
encoded in session transcripts rather than standalone memory files.

This matters because valuable context exists in conversation history even when no
explicit mistake/learning artifact was saved by the hooks during that turn. Making
session history a first-class recall source means those "aha" moments where past
judgment surfaces at the right time happen reliably instead of coincidentally.
"""

import datetime
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional


from memchorus.memory_source import MemorySource


def _resolve_session_db_path() -> Optional[str]:
    """Resolve the Hermes session database path.

    Looks for the SQLite session DB at the standard Hermes location.
    Returns None if not found.
    """
    home = os.path.expanduser("~")
    candidates = [
        # Default profile — primary live session DB
        os.path.join(home, ".hermes", "state.db"),
        # Legacy / alternate layouts
        os.path.join(home, ".hermes", "sessions", "session_history.db"),
        os.path.join(home, ".hermes", "hermes-agent", "sessions", "session_history.db"),
        os.path.join(home, ".hermes", "hermes-agent", "sessions", "agent_sessions.db"),
    ]

    for path in candidates:
        if os.path.exists(path):
            return path

    return None


class SessionSearchMemorySource(MemorySource):
    """
    Memory source that queries Hermes session history via FTS5.

    Provides access to conversation transcripts, tool results, user decisions,
    and agent reasoning captured in past sessions. Scores results by keyword
    relevance against the current turn context.

    Falls back gracefully if the session DB is unavailable or lacks FTS5 tables.
    """

    # Minimum score threshold for session results -- slightly higher than
    # hermes_default to avoid flooding context with low-signal transcript fragments.
    MIN_RECALL_SCORE = 0.5

    def __init__(self, name: str = "session_history", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self._name = name
        self.config = config or {}
        self._db_path = self.config.get("session_db_path", _resolve_session_db_path())
        self._conn: Optional[sqlite3.Connection] = None
        self._fts_table: Optional[str] = None

    def _get_connection(self) -> Optional[sqlite3.Connection]:
        """Lazy-connect to the session DB."""
        if self._conn is None and self._db_path and os.path.exists(self._db_path):
            try:
                self._conn = sqlite3.connect(self._db_path)
                self._conn.row_factory = sqlite3.Row
                # Discover FTS5 table name
                self._discover_fts_table()
            except Exception:
                self._conn = None
        return self._conn

    def _discover_fts_table(self) -> None:
        """Find the FTS5 virtual tables backed by the messages table.

        Hermes state.db uses content-synced FTS5 tables:
          - messages_fts (standard tokenizer) indexes content, tool_name, tool_calls
          - messages_fts_trigram (trigram tokenizer) indexes the same columns
        Both map rowid back to messages.id via content_rowid='id'.
        """
        try:
            if self._conn is None:
                return
            cursor = self._conn.cursor()

            # Find ALL FTS5-backed virtual tables — Hermes uses specific names
            infos = cursor.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
            ).fetchall()
            fts_tables_standard = []  # standard tokenizer
            fts_tables_trigram = []   # trigram tokenizer

            for tbl_name, sql in infos:
                if not sql or "fts5" not in sql.lower():
                    continue
                # Messages table has the content column with actual text we want
                if "messages" not in str(tbl_name):
                    continue
                if "trigram" in tbl_name:
                    fts_tables_trigram.append(tbl_name)
                else:
                    fts_tables_standard.append(tbl_name)

            # Prefer standard tokenizer for term matching; fall back to trigram
            if fts_tables_standard:
                self._fts_table = fts_tables_standard[-1]
            elif fts_tables_trigram:
                self._fts_table = fts_tables_trigram[-1]
        except Exception:
            self._fts_table = None

    def _score_text(self, query: str, text: str) -> float:
        """Score how well *query* matches *text* using keyword overlap.

        Similar approach to HermesDefaultMemorySource._content_matches but
        tuned for conversation transcript matching where context windows are
        longer but individual terms still carry signal.
        """
        query_lower = query.lower()
        text_lower = text[:8000].lower()  # bound search space for performance
        terms = [t for t in query_lower.split() if len(t) > 1]

        if not text_lower or not terms:
            return 0.0

        score = 0.0
        for term in terms:
            count = text_lower.count(term)
            if count > 0:
                score += 1.5 + min(count, 3) * 0.25

        return score

    def _effective_min_score(self) -> float:
        """Return the effective minimum recall score from config override."""
        return self.config.get("min_recall_score", self.MIN_RECALL_SCORE)

    def _safe_key(self, key: str) -> str:
        """Sanitize a memory key for use as a cache filename prefix."""
        sanitized = key.lower().strip()
        import re
        sanitized = re.sub(r'[^a-z0-9\s\-]', '-', sanitized)
        parts = [p for p in sanitized.split('-') if p]
        return '-'.join(parts)[:128]

    def save(self, key: str, value: Any) -> bool:
        """Save a session-derived memory to the local cache.

        Session history is read-only from the DB perspective, so we cache
        scored results locally as .json files for faster subsequent retrieval
        without re-querying the session DB every recall cycle.
        """
        try:
            cache_dir = self.config.get("cache_dir", os.path.expanduser("~/.hermes/memories/session_cache"))
            os.makedirs(cache_dir, exist_ok=True)

            if not isinstance(value, (str, int, float, bool, dict, list)):
                value = str(value)

            file_path = os.path.join(cache_dir, f"{self._safe_key(key)}.json")
            with open(file_path, 'w') as f:
                json.dump(value, f)
            return True
        except Exception:
            return False

    def retrieve(self, key: str) -> Optional[Any]:
        """Retrieve a cached session-derived memory by key."""
        try:
            cache_dir = self.config.get("cache_dir", os.path.expanduser("~/.hermes/memories/session_cache"))
            file_path = os.path.join(cache_dir, f"{self._safe_key(key)}.json")
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    return json.load(f)
            file_path_raw = os.path.join(cache_dir, f"{key}.json")
            if os.path.exists(file_path_raw):
                with open(file_path_raw, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Query session history for relevant past conversations.

        Uses FTS5 on the Hermes state.db messages table to find messages
        matching the query, then JOINs back to sessions for metadata
        (title, timestamps). Results are scored by keyword overlap and
        ranked by relevance.
        """
        candidates: List[Dict[str, Any]] = []
        min_score = self._effective_min_score()

        conn = self._get_connection()
        if not conn or not self._fts_table:
            return candidates

        try:
            cursor = conn.cursor()

            # --- FTS5 query (primary path) -----------------------------------
            # FTS5 uses AND by default for space-separated terms; we want OR
            # semantics so any matching term counts. Build explicit OR for
            # each significant term (>2 chars).
            fts_terms = [t for t in query.split() if len(t) > 2]
            rowids: List[int] = []

            if fts_terms:
                # Try OR-joined terms first (single query, good recall)
                or_query = ' OR '.join(fts_terms)
                try:
                    rows = cursor.execute(
                        f'SELECT rowid FROM {self._fts_table} '
                        f'WHERE {self._fts_table} MATCH ?',
                        (or_query,),
                    ).fetchall()
                    rowids = [r[0] for r in rows[:200]]  # bound candidate set
                except sqlite3.OperationalError:
                    pass

                # If OR failed, try individual terms with dedup
                if not rowids:
                    seen_ids: set[int] = set()
                    for term in fts_terms:
                        try:
                            rows = cursor.execute(
                                f'SELECT rowid FROM {self._fts_table} '
                                f'WHERE {self._fts_table} MATCH ?',
                                (term,),
                            ).fetchall()
                            for r in rows[:50]:
                                rid = r[0]
                                if rid not in seen_ids:
                                    rowids.append(rid)
                                    seen_ids.add(rid)
                        except sqlite3.OperationalError:
                            pass

            # --- JOIN to real messages + sessions tables ----------------------
            if not rowids:
                return candidates  # no FTS hits, nothing to score

            placeholders = ','.join('?' * len(rowids))
            join_sql = f'''
                SELECT m.id, m.session_id, m.role, m.content, m.timestamp,
                       s.title, s.started_at
                FROM messages m
                LEFT JOIN sessions s ON m.session_id = s.id
                WHERE m.rowid IN ({placeholders})
                ORDER BY m.timestamp DESC
            '''
            msgs = cursor.execute(join_sql, rowids).fetchall()

            # --- Score and rank ----------------------------------------------
            for msg in msgs:
                msg_id, session_id, role, content, ts_epoch, title, started_at = msg

                if not content:
                    continue

                score = self._score_text(query, content)
                if score < min_score:
                    continue

                # Extract text from JSON-wrapped tool output when possible
                display_text = content
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and 'output' in parsed:
                        display_text = parsed['output']
                except (json.JSONDecodeError, TypeError):
                    pass

                # Build ISO timestamp from unix epoch
                ts_iso = None
                if ts_epoch:
                    try:
                        ts_iso = datetime.datetime.fromtimestamp(
                            ts_epoch, tz=datetime.timezone.utc
                        ).isoformat()
                    except (OSError, ValueError, OverflowError):
                        pass

                candidates.append({
                    'key': f'session-msg-{msg_id}',
                    'content': {
                        'session_id': session_id or '',
                        'message_id': msg_id,
                        'role': role or '',
                        'title': title or '',
                        # Store JSON-parsed output when available, else raw content
                        'text': display_text[:2000],
                        'raw_content_length': len(content),
                    },
                    'source': self._name,
                    'timestamp': ts_iso or datetime.datetime.now(
                        tz=datetime.timezone.utc
                    ).isoformat(),
                    '_score': score,
                })

            # Rank by score descending
            candidates.sort(key=lambda r: r['_score'], reverse=True)

        except Exception:
            pass  # degrade gracefully on any DB error

        # Trim to limit and strip internal _score before returning
        results = []
        for c in candidates[:limit]:
            c.pop('_score', None)
            results.append(c)

        return results

    def delete(self, key: str) -> bool:
        """Remove a cached session-derived memory."""
        try:
            cache_dir = self.config.get("cache_dir", os.path.expanduser("~/.hermes/memories/session_cache"))
            safe_path = os.path.join(cache_dir, f"{self._safe_key(key)}.json")
            raw_path = os.path.join(cache_dir, f"{key}.json")

            removed = False
            if os.path.exists(safe_path):
                os.remove(safe_path)
                removed = True
            if os.path.exists(raw_path) and not removed:
                os.remove(raw_path)
                removed = True
            return removed
        except Exception:
            pass
        return False

    def list_all_keys(self) -> List[str]:
        """Enumerate cached session-derived memory keys."""
        keys: List[str] = []
        try:
            cache_dir = self.config.get("cache_dir", os.path.expanduser("~/.hermes/memories/session_cache"))
            if os.path.exists(cache_dir):
                for filename in os.listdir(cache_dir):
                    if filename.endswith('.json'):
                        keys.append(filename[:-5])
        except Exception:
            pass
        return sorted(keys)

    @property
    def is_available(self) -> bool:
        """Check if session DB is reachable with FTS5 support."""
        try:
            conn = self._get_connection()
            return conn is not None and self._fts_table is not None
        except Exception:
            return False

    def get_source_info(self) -> Dict[str, Any]:
        """Get information about this session history memory source."""
        return {
            'name': self._name,
            'type': 'session_history',
            'available': self.is_available,
            'db_path': self._db_path,
            'fts_table': self._fts_table,
            'description': 'Hermes session history via FTS5 -- fallback recall for conversation transcripts',
            'version': '1.0.0'
        }

    def proactive_save(self, key: str, value: Any, context: Optional[Dict[str, Any]] = None) -> bool:
        """Proactively cache a session-derived insight."""
        return self.save(key, value)

    def proactive_check(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Check for relevant past session context matching the decision being made."""
        if not context:
            return {
                'status': 'ready',
                'found_memories': 0,
                'source': self._name,
                'timestamp': datetime.datetime.now().isoformat()
            }

        query = ' '.join([str(v) for v in context.values() if v])
        findings = self.search(query, limit=3)
        recommendations = []

        if query and findings:
            recommendations.append({
                'type': 'session_recall',
                'found': len(findings),
                'sessions': [{'key': f['key'], 'summary': str(f.get('content', ''))[:500]} for f in findings]
            })

        return {
            'recommendations': recommendations,
            'source': self._name,
            'timestamp': datetime.datetime.now().isoformat(),
            'context_used': context
        }

    @property
    def name(self) -> str:
        """Get the name of this source."""
        return self._name
