# ObsidianVaultMemorySource — Design Document

**Author:** Bubo (default profile)
**Date:** 2026-06-26
**Status:** Scoping study — no code implemented yet
**Task ID:** t_4df257f4

---

## 1. Feasibility Verdict

**Verdict: FEASIBLE — Recommend for v1.1 scope.**

Obsidian vault as a MemorySource is architecturally sound. The vault already exists at `~/.hermes/workspace/Bubo_Wisdom/` with 249 markdown notes (~2.5MB total content) across ~139 directories. It satisfies the spec requirement for human-readable, markdown-based persistence. The MemorySource contract maps cleanly onto Obsidian's filesystem-first architecture. Implementation complexity is LOW — it is structurally similar to HermesDefaultMemorySource but reads/writes `.md` files instead of `.json` files, and uses `rg` (ripgrep) for search rather than filename-only matching.

The primary value proposition: Obsidian contains conceptually richer knowledge than Hermes default memory (project specs, assessments, research notes, workflow documentation, daily reports). Adding it as a voice gives the chorus access to a substantially larger and more structured corpus without introducing new dependencies or runtime overhead beyond filesystem I/O.

---

## 2. Format Alignment Analysis

### Obsidian Storage Model
- Flat `.md` files on disk, organized in folders
- `[[wikilink]]` syntax for cross-references
- YAML frontmatter optional (tags, aliases, custom metadata)
- No built-in database or API — purely filesystem-driven
- Human-readable by definition (markdown is the native format)

### MemorySource Contract Mapping

| Method | Obsidian Implementation | Complexity |
|--------|------------------------|------------|
| `__init__(name, config)` | Accept vault_path + recursive_depth config | Trivial |
| `save(key, value)` | Write markdown note to vault (create parent dirs as needed) | Low — similar to hermes_default |
| `retrieve(key)` | Resolve key to filename, read `.md` content | Low — path resolution + file read |
| `search(query, limit)` | Ripgrep across vault for query term in filenames + content | Low — rg is fast on 250 files |
| `is_available()` | Check vault_path exists and is readable | Trivial |
| `get_source_info()` | Return vault path, note count, total size | Low — stat traversal |
| `proactive_check(context)` | Semantic-style scan of recent notes + tagged content | Medium — requires frontmatter/tag parsing |
| `proactive_save(key, value, context)` | Same as save, with enhanced frontmatter context | Low |

**Key insight:** The `save`/`retrieve` pair requires a key-to-filename mapping strategy. Two viable approaches:

1. **Filename-as-key (Recommended):** Key maps directly to `~/.hermes/workspace/Bubo_Wisdom/<key>.md`. Clean, predictable, follows the same pattern HermesDefaultMemorySource uses for JSON keys.
2. **Directory-aware routing:** Key can include path separators → `Projects/MemChorus/Design-Decisions.md`. More natural for Obsidian but requires a safe key parser that avoids arbitrary path traversal.

**Recommendation: Hybrid approach.** Accept simple keys (no slashes) and write to the vault root. Accept structured keys containing `/` and honor directory routing, clamped within the vault boundary to prevent path traversal. Sanitization mirrors `_safe_key()` from HermesDefaultMemorySource.

---

## 3. Query Capability Analysis

### Current Search Landscape

| Source | Search Mechanism | Speed (249 notes) | Quality of Results |
|--------|-----------------|-------------------|-------------------|
| **MemPalace** | Semantic search via MCP API (vector similarity). Subprocess spawn per call adds 1-3s latency. | ~2-5s roundtrip | High — true semantic recall across KG facts and diary entries |
| **HermesDefault** | Filename substring match + JSON content scan. In-process os.listdir loop. | <50ms | Low — only matches filenames, misses content inside JSON blobs |
| **Obsidian (proposed)** | Ripgrep regex search over 249 .md files (~2.5MB total) | <50ms for simple queries | Medium-High — full content search across all notes |

### What We Gain vs. MemPalace
- **Speed:** Obsidian search is an order of magnitude faster (sub-50ms vs 2-5s). No subprocess spawning, no network transport.
- **Coverage:** Obsidian has conceptual project knowledge MemPalace does not duplicate — specs, assessments, workflow docs, daily reports.
- **Transparency:** Results are plain markdown the agent can read directly without parsing binary protocols.

### What We Gain vs. MemPalace semantic quality
- **We lose semantic understanding.** Ripgrep gives keyword/regex matches, not vector similarity. A query for "optimization strategies" won't match a note titled "How to make memory faster" unless those exact words appear.
- **Mitigation:** frontmatter tags and YAML metadata can serve as a lightweight proxy for semantic categories. Tagging notes on save improves future recall precision without needing embeddings.

### Net Assessment
Obsidian fills a gap between fast-but-shallow keyword search and slow-but-deep semantic search. It is the practical middle ground that catches most useful hits quickly while MemPalace handles the long-tail semantic cases. This complements rather than duplicates existing capabilities.

---

## 4. Overlap vs. HermesDefaultMemorySource

### Where They Overlap
Both read/write local files on disk. Both are synchronous, no external dependencies, instant availability.

### Boundary Definition

| Dimension | HermesDefaultMemorySource | ObsidianVaultMemorySource |
|-----------|--------------------------|--------------------------|
| **Path** | `~/.hermes/memories/` (structured JSON) | `~/.hermes/workspace/Bubo_Wisdom/` (markdown notes) |
| **Format** | `.json` blobs — programmatic key-value pairs | `.md` files — human-readable documents |
| **Content type** | User preferences, ephemeral state, structured config data | Conceptual knowledge, project specs, research, reports |
| **Write pattern** | Programmatic save (key → JSON blob) | Document creation (key → markdown note with frontmatter) |
| **Search scope** | Filename-based in memory_dir | Full-content ripgrep across vault |
| **Role in chorus** | Resilient core — always available, always fast | Enhancement voice — richer content, faster than MemPalace |

### Cannibalization Risk: LOW
The two sources serve fundamentally different content domains. Hermes default is for structured data (preferences, thresholds, configuration fragments). Obsidian is for documents and conceptual knowledge. There is minimal reason to store the same memory in both places because one expects JSON structures and the other expects prose.

**Prevention mechanism:** The `_PROFILE_SOURCE_HINT` dict controls preferred destinations per MemoryProfile. Adding `"obsidian_vault"` as a preferred target for `LONG_LIVED_KNOWLEDGE`, `LARGE_DATA_BLOCK`, and `EPHEMERAL` profiles ensures Obsidian receives appropriate content while Hermes default handles structured data and user preferences.

**Proposed `_PROFILE_SOURCE_HINT` update:**
```python
_PROFILE_SOURCE_HINT[MemoryProfile.LONG_LIVED_KNOWLEDGE] = ["mempalace", "obsidian_vault"]
_PROFILE_SOURCE_HINT[MemoryProfile.EPHEMERAL] = ["hermes_default", "mempalace", "obsidian_vault"]
_PROFILE_SOURCE_HINT[MemoryProfile.LARGE_DATA_BLOCK] = ["hermes_default", "obsidian_vault"]  # better for large text docs than KG
```

### Third Voice Status
Obsidian becomes a meaningful third voice because:
1. It covers knowledge domains neither other source touches at scale (249 existing notes).
2. It offers faster search than MemPalace for keyword-aligned queries.
3. It provides richer content than Hermes default, which primarily holds key-value configurations.

---

## 5. Integration Surface

### Registration in `orchestrator.py`

Add to `_initialize_default_sources()` (side-by-side with existing sources):

```python
from memchorus.obsidian_vault_memory_source import ObsidianVaultMemorySource

def _initialize_default_sources(self):
    # ... existing hermes_default and mempalace initialization ...

    # Add Obsidian vault as a document voice
    obsidian_source = ObsidianVaultMemorySource(
        name='obsidian_vault',
        config=self.config.get('obsidian_config', {})
    )
    self.memory_sources['obsidian_vault'] = obsidian_source
```

### Configuration Keys

```yaml
obsidian_config:
  vault_path: "/home/bubo/.hermes/workspace/Bubo_Wisdom"  # or ~/Documents/Obsidian Vault (default fallback)
  recursive_search_depth: 10  # max directory depth for scanning
  file_glob: "*.md"           # pattern to match Obsidian notes
  rg_binary: "rg"             # ripgrep path (resolve via shutil.which if omitted)
  enable_frontmatter_tags: true  # parse YAML frontmatter for tag-based recall
  excluded_dirs: [".obsidian", "__pycache__", "Archive/_backup"]  # pruning list
```

### Path Resolution Strategy
1. Check `config.vault_path` — explicit override.
2. Use environment variable `OBSIDIAN_VAULT_PATH` if set.
3. Fall back to `~/.hermes/workspace/Bubo_Wisdom`.
4. Final fallback: `~/Documents/Obsidian Vault`.

### Fallback Behavior When Vault Doesn't Exist
- `is_available()` returns `False` — the orchestrator skips this source gracefully.
- No exception or crash in the orchestrator save/retrieve/search paths.
- The agent loses access to vault content but retains hermes_default + mempalace voices.
- **No data loss:** Obsidian is read/write, so missing vault only means no new notes land there — existing notes in other sources are unaffected.

---

## 6. Performance Characteristics

### Current Vault Profile (Ground Truth)
- **Note count:** 249 markdown files
- **Total content size:** ~2.5 MB
- **Average note size:** ~7.8 KB per file
- **Directory nesting depth:** ~139 directories

### Expected Performance (100+ notes, scaling to 500)

| Operation | Method | Estimated Time (249 notes) | Scaling (500 notes) |
|-----------|--------|--------------------------|-------------------|
| `is_available()` | `os.path.exists` + stat | <1ms | <1ms |
| `get_source_info()` | `find . -name "*.md" \| wc -l` | ~5-10ms | ~10-20ms |
| `retrieve(key)` | Single file read (~8KB avg) | <5ms | <5ms (O(1)) |
| `save(key, value)` | Write + mkdir as needed | <10ms | <10ms |
| `search(query, limit)` | Ripgrep across vault | <20ms for keyword match | ~40-60ms |
| `proactive_check(context)` | rg scan on context keywords + frontmatter parse | <50ms | ~80-100ms |

**Ripgrep performance note:** ripgrep is specifically optimized for single-machine filesystem search. On a 2.5MB corpus of text files, it comfortably processes the entire vault in under 10ms per query. Even at 500 notes (~5MB), sub-60ms remains realistic because rg uses SIMD-accelerated regex and parallel file reading.

**Overhead vs other sources:**
- Faster than MemPalace by ~100-200x (no subprocess spawn, no async roundtrip)
- Similar speed to HermesDefault for simple operations
- Slightly slower than hermes_default for search (hermes only scans filenames, Obsidian scans full content)

### Worst-Case Scenario
A vault with 2000+ notes and ~50MB total content would push ripgrep to ~200-500ms per search. This is still acceptable for MemChorus's real-time requirements but worth noting as an upper practical bound on vault size before an indexing layer becomes useful.

---

## 7. Interface Sketch (Method Signatures)

```python
class ObsidianVaultMemorySource(MemorySource):
    """Memory source adapter for an Obsidian markdown vault."""

    def __init__(
        self,
        name: str = "obsidian_vault",
        config: Optional[Dict[str, Any]] = None,
    ) -> None: ...

    def save(self, key: str, value: Any) -> bool: ...
    """Write markdown note. Key maps to filename; value is serialized to markdown content."""

    def retrieve(self, key: str) -> Optional[Any]: ...
    """Read markdown note by key (filename resolution). Returns parsed content dict or raw string."""

    def search(
        self, query: str, limit: int = 10
    ) -> List[Dict[str, Any]]: ...
    """Ripgrep search across vault .md files. Returns list of {key, content, source, timestamp, score} dicts."""

    def is_available(self) -> bool: ...
    """True if vault_path exists and is readable."""

    def get_source_info(self) -> Dict[str, Any]: ...
    """Returns {name, type, vault_path, note_count, total_size_bytes, available}."""

    def proactive_check(
        self, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]: ...
    """Scan recent/tagged notes relevant to context. Returns matching memories with scores."""

    def proactive_save(
        self, key: str, value: Any,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool: ...
    """Save memory, optionally enriching YAML frontmatter with context metadata (tags, timestamps)."""
```

### Private Helper Methods
```python
    def _resolve_vault_path(self) -> str: ...
    def _key_to_filename(self, key: str) -> str: ...
    def _filename_to_key(self, path: str) -> str: ...
    def _parse_frontmatter(self, content: str) -> Tuple[Dict[str, Any], str]: ...
    def _build_frontmatter(self, context: Dict[str, Any]) -> str: ...
    def _rg_search(self, query: str, limit: int) -> List[Dict[str, Any]]: ...
```

---

## 8. Test Targets

### Unit Tests (`tests/test_obsidian_vault_source.py`)

| Test | Description | Priority |
|------|-------------|----------|
| `test_init_default_config` | Constructor with no config falls back to Bubo_Wisdom path | P0 |
| `test_init_explicit_vault_path` | Constructor with explicit vault_path uses it | P0 |
| `test_is_available_true` | Returns True when vault exists | P0 |
| `test_is_available_false` | Returns False when path doesn't exist | P0 |
| `test_save_and_retrieve_roundtrip` | Save key/value, retrieve same key, assert equality | P0 |
| `test_save_large_content` | Save a 50KB note, verify file written correctly | P0 |
| `test_retrieve_missing_key_returns_none` | Non-existent key returns None | P0 |
| `test_search_by_keyword` | Search for term appearing in note content, verify match returned | P1 |
| `test_search_limit_respected` | Search with limit=3 returns at most 3 results | P1 |
| `test_search_no_match_empty_list` | Impossible query returns empty list | P1 |
| `test_key_sanitization_prevents_traversal` | Key with `../` is safely handled, no path escape | P0 |
| `test_frontmatter_parsing` | YAML frontmatter in note correctly parsed | P2 |
| `test_proactive_check_returns_context` | Context-aware search returns relevant notes | P1 |
| `test_save_with_structured_key_routing` | Key with `/` maps to correct subdirectory within vault | P1 |
| `test_excluded_dirs_not_searched` | `__pycache__`, `.obsidian` excluded from search | P1 |

### Integration Tests (`tests/test_obsidian_orchestrator_integration.py`)

| Test | Description | Priority |
|------|-------------|----------|
| `test_registration_in_orchestrator` | Source appears after orchestrator initialization | P0 |
| `test_orchestrator_search_includes_obsidian_results` | Multi-source search includes obsidian entries | P1 |
| `test_save_routes_to_obsidian_for_large_block_profile` | MemoryProfile.LARGE_DATA_BLOCK prefers obsidian | P1 |
| `test_graceful_degradation_when_vault_missing` | Orchestrator functions normally with obsidian unavailable | P0 |

### Performance Tests (`tests/test_obsidian_performance.py`)

| Test | Description | Acceptance Criteria |
|------|-------------|-------------------|
| `test_search_under_50ms` | 249-note vault search completes within budget | <50ms p95 |
| `test_retrieve_under_10ms` | Single note retrieval is fast | <10ms p95 |
| `test_save_under_20ms` | Write operation including mkdir | <20ms p95 |

---

## 9. Version Placement and Milestones

### Recommendation: **v1.1 Scope**

Justification:
- **Complexity:** Low to moderate — structurally nearly identical to HermesDefaultMemorySource with a search layer upgrade.
- **Risk:** Minimal — the source degrades gracefully when vault is missing, same as hermes_default when memory_dir doesn't exist.
- **Value-to-effort ratio:** High — 249 existing notes of conceptual knowledge become immediately searchable by the chorus without external dependencies or network calls.
- **Dependencies:** Zero — only ripgrep (standard on Linux, available via package manager) and Python stdlib for file I/O.

### Implementation Milestones

| Phase | Work Item | Estimated Effort |
|-------|-----------|-----------------|
| M1 | Implement `ObsidianVaultMemorySource` class with save/retrieve/is_available/get_source_info | 2-3 turns |
| M2 | Implement search via ripgrep + result formatting | 1-2 turns |
| M3 | Implement proactive_check/proactive_save with frontmatter support | 1-2 turns |
| M4 | Add to orchestrator._initialize_default_sources() + _PROFILE_SOURCE_HINT update | 1 turn |
| M5 | Write P0/P1 unit tests + integration tests + performance benchmarks | 2-3 turns |
| M6 | Commit, push, review cycle | Standard workflow |

**Total estimated: 9-12 turns for Cthugha implementation.**

---

## 10. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Vault path mismatch on other systems | Medium | Environment variable + fallback chain covers common layouts |
| Spaces in vault path | Low | File tools used instead of shell, handles spaces natively |
| Large vaults (>2000 notes) slow down search | Low | Configurable `recursive_search_depth` and excluded_dirs for pruning. Index layer can be added later if needed |
| ripgrep not installed | Low | Fallback to Python stdlib `glob + re` if rg binary not found (slower but functional) |
| Path traversal via malicious key | High | Key sanitization mirrors `_safe_key()` from hermes_default — clamped, no parent escapes |

---

## 11. Conclusion

Adding ObsidianVaultMemorySource is architecturally clean, technically feasible with modest effort, and adds genuine value as a third voice with fast keyword-based access to a rich corpus of conceptual knowledge. It fills the practical gap between HermesDefault's structured data storage and MemPalace's semantic recall, offering something neither provides alone: fast full-text search over human-readable documents. **Recommend proceeding with v1.1 implementation.**
