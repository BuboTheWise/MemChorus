"""
Locator storage — compact "go-read-it" pointers stored alongside memory bodies.

Issue #140: full content blobs (docs, pages, tool output) injected on recall
bloat the prompt window.  This module extracts a compact *locator* from a
memory payload at save time and stores it as an additional field on the
record, ALONGSIDE the existing body (never replacing it — the minimal safe
variant required by the card).  On recall, the formatter injects the compact
locator (``gist + topics + path_or_url``, capped at ~150 chars) instead of
the raw body when the body is long.  The full content stays retrievable on
demand via ``orchestrator.retrieve(key)`` (or the source's own read path).

Locator schema (flat dict, all keys optional; the card requires at least
source + one of path_or_url/title/gist per entry that goes through this path):

    source       str   "vault" | "file" | "web" | "tool"
    path_or_url  str   absolute/relative path or URL the content came from
    title        str   extracted title (HTML <title>, first markdown heading,
                       or a filename fallback)
    topics       list  up to 6 topic tags (headings, or keywords from the body)
    gist         str   one line: what the content covers
    key          str   memory key (set automatically, last-resort anchor)

Everything here is source-agnostic: the locator rides on the payload dict, so
whichever backend persists the memory stores the locator alongside the body.
Nothing here raises — every helper degrades to ``None`` / unchanged input
when nothing extractable is present, so ordinary saves (plain strings with
no provenance) are byte-identical to before.
"""

import re
from typing import Any, Dict, List, Optional

# Content longer than this (chars, after unwrapping) is a "blob" — injection
# will prefer the locator over the full body.  Kept in the same ball-park as
# hooks._MAX_CONTENT_CHARS (300) so locator-first only kicks in for content
# the formatter would otherwise start truncating anyway.
LOCATOR_INJECT_THRESHOLD = 240

# Maximum total length of the injected locator line (gist + topics +
# path_or_url pointer), per the #140 acceptance criterion (~150 chars).
LOCATOR_LINE_MAX_CHARS = 150

# Caps that keep individual locator fields compact.
GEST_MAX_CHARS = 120
TOPICS_MAX = 6
TOPIC_MAX_CHARS = 24

# Topic tags that merely *describe* the payload rather than summarizing
# content — dropped from topic lists ("This is a Python module" ≈ noise).
_STOPWORD_TOPICS = {
    "summary", "overview", "introduction", "conclusion", "references",
    "notes", "todo", "tldr", "tl;dr", "changelog", "appendix",
}

# Heading lines: markdown '#' levels 1–4 (a '#' is *the* heading marker; an
# accidental '#tag' in prose is harmless noise, never a false topic).
_MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,4})\s+(.{2,80})\s*$")

# Keyword fallback for prose without structure: long, wordy, starts upper.
_KEYWORD_RE = re.compile(r"^[A-Z][a-z0-9][a-zA-Z0-9 +_-]{4,24}$")

# HTML document shapes (case-insensitive search, not anchored: content may
# be an excerpt that includes the <head>).
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HTML_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_HTML_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)

_TAG_RE = re.compile(r"<[^>]+>")

_GEST_STOPWORDS = {
    "this", "that", "these", "those", "there", "here", "with", "from",
    "into", "out", "about", "than", "then", "them", "they", "their",
    "were", "been", "being", "have", "has", "had", "will", "would",
    "should", "could", "can", "may", "might", "must", "do", "not",
    "no", "and", "or", "but", "the", "a", "an", "is", "are", "was",
    "it", "its", "for", "to", "of", "in", "on", "as", "by", "at",
    "be", "we", "our", "you", "your", "he", "she", "his", "her",
    "if", "so", "do", "does", "didn", "don", "isn", "aren",
}


def _norm_scalar(v: Any) -> Optional[str]:
    """Normalise a locator field value to a clean string, or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        s = str(int(v)) if float(v).is_integer() else str(v)
    else:
        s = str(v).strip()
    return s or None


def _body_text(value: Any) -> str:
    """Best-effort plain-text body of a payload (never raises)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for field in ("text", "content", "body", "_content"):
            inner = value.get(field)
            if isinstance(inner, str) and inner:
                return inner
        for field in ("content", "payload"):
            inner = value.get(field)
            if isinstance(inner, dict):
                sub = inner.get("text") or inner.get("content") or inner.get("body")
                if isinstance(sub, str) and sub:
                    return sub
    if isinstance(value, (list, tuple)):
        return " ".join(_body_text(v) for v in value)
    return ""


# ---------------------------------------------------------------------------
# Field extraction (title / topics / gist)
# ---------------------------------------------------------------------------

def _first_md_heading(body: str) -> Optional[str]:
    """First markdown heading (levels 1–4), stripped of trailing #/whitespace."""
    for line in body.splitlines():
        m = _MD_HEADING_RE.match(line)
        if m:
            head = m.group(2).strip().rstrip("#").strip()
            if 2 <= len(head) <= 80:
                return head
    return None


def _strip_tags(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment)
    return re.sub(r"\s+", " ", text).strip()


def _first_html_heading(body: str) -> Optional[str]:
    for regex in (_HTML_H1_RE, _HTML_H2_RE):
        m = regex.search(body)
        if m:
            text = _strip_tags(m.group(1))
            if 2 <= len(text) <= 80:
                return text
    return None


def extract_title(value: Any, body: str = "") -> Optional[str]:
    """Extract a short title from a payload (structured field → doc headings
    → filename fallback).  Never raises; None when nothing usable."""
    if isinstance(value, dict):
        t = _norm_scalar(value.get("title"))
        if t:
            return t[:80]
        loc = value.get("locator")
        if isinstance(loc, dict):
            t = _norm_scalar(loc.get("title"))
            if t:
                return t[:80]

    if body:
        md_head = _first_md_heading(body)
        if md_head:
            return md_head
        html_title = _HTML_TITLE_RE.search(body)
        if html_title:
            text = _strip_tags(html_title.group(1))
            if 2 <= len(text) <= 80:
                return text
        if _first_html_heading(body):
            return _first_html_heading(body)

    # Filename-ish fallback when the body is a path or the key looks like a path.
    path_like = ""
    if isinstance(body, str):
        if re.fullmatch(r"[\w./\\ -]+", body.strip()) and "/" in body:
            path_like = body.strip()
    if path_like:
        leaf = path_like.replace("\\", "/").rstrip("/").split("/")[-1]
        leaf = _strip_tags(leaf)
        if 2 <= len(leaf) <= 80 and not leaf.startswith("."):
            return leaf
    return None


def _clean_topic(token: str) -> Optional[str]:
    t = re.sub(r"\s+", " ", token.strip().strip("#*_` \t")).strip()
    t = re.sub(r"^[-–—]\s*", "", t)
    if not (2 <= len(t) <= TOPIC_MAX_CHARS):
        return None
    if not re.search(r"[A-Za-z0-9]", t):
        return None
    if t.lower() in _STOPWORD_TOPICS:
        return None
    return t


def _keywords_from_body(body: str, limit: int) -> List[str]:
    """Keyword fallback topics for prose without structured headings:
    long wordy tokens that start with an uppercase letter.  Bounded, de-duped,
    order-preserving (frequency is a secondary sort key)."""
    counts: Dict[str, int] = {}
    first_seen: Dict[str, int] = {}
    order = 0
    for m in _KEYWORD_RE.finditer(body):
        word = m.group(0).strip()
        if word.lower() in _GEST_STOPWORDS:
            continue
        if word not in counts:
            counts[word] = 0
            first_seen[word] = order
            order += 1
        counts[word] += 1
    ranked = sorted(counts, key=lambda w: (-counts[w], first_seen[w]))
    return ranked[:limit]


def extract_topics(value: Any, body: str = "", limit: int = TOPICS_MAX,
                   title: Optional[str] = None) -> List[str]:
    """Extract up to *limit* compact topic tags:
    explicit structured topics → document headings → prose keywords.
    The document title is excluded (it is not a topic)."""
    if isinstance(value, dict):
        for field in ("topics", "tags", "labels"):
            raw = value.get(field)
            if isinstance(raw, (list, tuple, set)):
                topics: List[str] = []
                for t in raw:
                    c = _clean_topic(str(t))
                    if c and c not in topics and c.lower() != (title or "").lower():
                        topics.append(c)
                    if len(topics) >= limit:
                        break
                if topics:
                    return topics
            elif isinstance(raw, str):
                parts = [p for p in re.split(r"[,;]\s*", raw) if p.strip()]
                topics = []
                for p in parts:
                    c = _clean_topic(p)
                    if c and c not in topics and c.lower() != (title or "").lower():
                        topics.append(c)
                    if len(topics) >= limit:
                        break
                if topics:
                    return topics

    if body:
        headings: List[str] = []
        for line in body.splitlines():
            m = _MD_HEADING_RE.match(line)
            if m:
                c = _clean_topic(m.group(2))
                if c and c not in headings and c.lower() != (title or "").lower():
                    headings.append(c)
                if len(headings) >= limit:
                    break
        if headings:
            return headings

        html_headings: List[str] = []
        for regex in (_HTML_H1_RE, _HTML_H2_RE):
            for m in regex.finditer(body):
                c = _clean_topic(_strip_tags(m.group(1)))
                if c and c not in html_headings and c.lower() != (title or "").lower():
                    html_headings.append(c)
                if len(html_headings) >= limit:
                    break
            if html_headings:
                return html_headings

    return _keywords_from_body(body, limit)


def _make_gist_from_headings(headings: List[str]) -> str:
    """Cover-line built from up to 3 headings: 'Covers A, B and C'."""
    items = [h for h in (headings or []) if h][:3]
    if not items:
        return ""
    if len(items) == 1:
        line = f"Covers {items[0]}."
    elif len(items) == 2:
        line = f"Covers {items[0]} and {items[1]}."
    else:
        line = f"Covers {items[0]}, {items[1]} and {items[2]}."
    return line[:GEST_MAX_CHARS]


def extract_gist(value: Any, body: str = "") -> Optional[str]:
    """One line: what the content covers.  Never raises; None when
    nothing usable can be distilled.

    Preference order:
      1. caller-supplied explicit gist;
      2. HTML document title (a page's <title> IS its one-line descriptor);
      3. first prose line (real sentence, not a label/heading/slug);
      4. cover line built from headings ("Covers A, B and C");
      5. the document title / first non-empty line as last resort.
    """
    if isinstance(value, dict):
        g = _norm_scalar(value.get("gist"))
        if g:
            return g[:GEST_MAX_CHARS]
        loc = value.get("locator")
        if isinstance(loc, dict):
            g = _norm_scalar(loc.get("gist"))
            if g:
                return g[:GEST_MAX_CHARS]

    if not body:
        return None
    plain = body.strip()

    # 2) HTML document title — a page's own one-line descriptor.
    if re.search(r"<\s*(?:html|head|body|title)\b", plain, re.IGNORECASE):
        m = _HTML_TITLE_RE.search(plain)
        if m:
            text = _strip_tags(m.group(1))
            if 2 <= len(text) <= 120:
                return text

    # 3) First genuine prose line (sentence-like, with lowercase body words).
    for line in plain.splitlines()[:25]:
        s = line.strip().lstrip("#*- \t")
        if not s or len(s) < 12:
            continue
        if re.search(r"<[a-zA-Z/!]", s):  # tag soup — strip tags, re-check
            s = _strip_tags(s)
            if len(s) < 12:
                continue
        if s.lstrip("# \t").lower().startswith(("note:", "todo:")):
            continue
        if not re.search(r"[a-z]{3,}", s):  # pure slug / label / all-caps
            continue
        return s[:GEST_MAX_CHARS]

    # 4) Headings-based cover line.
    headings: List[str] = []
    for line in plain.splitlines()[:60]:
        hm = _MD_HEADING_RE.match(line)
        if hm:
            c = _clean_topic(hm.group(2))
            if c:
                headings.append(c)
            if len(headings) >= 4:
                break
    if headings:
        # Skip the first heading *is* the title — cover the rest.
        head_items = headings[1:] if len(headings) > 1 else headings
        line = _make_gist_from_headings(head_items)
        if line:
            return line

    # 5) Last resort: the extracted title or first non-empty line.
    title = extract_title(value, body=body)
    if title:
        return title
    for line in plain.splitlines():
        s = line.strip()
        if s:
            return s[:GEST_MAX_CHARS]
    return None


# ---------------------------------------------------------------------------
# Explicit / structured locator sources
# ---------------------------------------------------------------------------

# Structured field names on the payload dict, mapped to canonical locator keys.
_STRUCTURED_FIELD_MAP = (
    ("url", "path_or_url"),
    ("uri", "path_or_url"),
    ("path_or_url", "path_or_url"),
    ("path", "path_or_url"),
    ("file_path", "path_or_url"),
    ("file", "path_or_url"),
    ("source_url", "path_or_url"),
    ("source_path", "path_or_url"),
)

# Body-text URL (scheme required — avoids eating "dir/file.py"; tags/brackets
# are not URL content).
_URL_RE = re.compile(r"\b((?:https?|git)://[^\s`'\"|,\)\]<]+)")
# Body-text file paths: absolute, relative-with-dir, or a bare known-extension name.
_PATH_BODY_RE = (
    r"\.{1,2}/[\w./-]*"
    r"|(?:/[\w.-]+)+/[\w./-]*"
    r"|[\w][\w.-]*/[\w./-]*"
    r"|\b[\w][\w-]*\.(?:py|md|txt|json|ya?ml|toml|cfg|ini|env|js|ts|java|kt|go|rs|c|cc|cpp|h|hpp|sh|sql|xml|html|css)\b"
)
# The (?<!:) guard stops paths that live inside a URL (…/file.py?x=1 is the URL's job).
_PATH_RE = re.compile(r"(?<!:)\s(" + _PATH_BODY_RE + r")(?!:)")


def _infer_source(value: Any, body: str, source_name: str = "") -> str:
    """Classify the entry kind: vault | file | web | tool (card's enum)."""
    if isinstance(value, dict):
        explicit = value.get("source")
        if isinstance(explicit, str) and explicit.strip().lower() in ("vault", "file", "web", "tool"):
            return explicit.strip().lower()
        loc = value.get("locator")
        if isinstance(loc, dict):
            explicit = loc.get("source")
            if isinstance(explicit, str) and explicit.strip().lower() in ("vault", "file", "web", "tool"):
                return explicit.strip().lower()

    if body and re.search(r"<\s*(?:html|head|title|meta|link|div|p|article|!doctype)\b", body, re.IGNORECASE):
        return "web"
    if body and _URL_RE.search(body):
        return "web"
    if body and _PATH_RE.search(body):
        return "file"
    if body and _MD_HEADING_RE:
        # markdown-ish structure with headings → treat as a document (vault/file)
        for line in body.splitlines()[:20]:
            if _MD_HEADING_RE.match(line):
                return "vault"
    if source_name:
        sn = source_name.lower()
        if "mempalace" in sn or "vault" in sn:
            return "vault"
        if "web" in sn or "url" in sn or "fetch" in sn:
            return "web"
        if "tool" in sn or "command" in sn:
            return "tool"
    return "file"


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract_locator(value: Any, key: str = "", source_name: str = "") -> Optional[Dict[str, Any]]:
    """Build a card-schema locator from a memory payload.

    Priority order:
      1. explicit ``value["locator"]`` (dict) or ``value["locator"]`` string —
         kept/normalised, merged with derived fields;
      2. structured provenance fields (url / path / file_path / ...);
      3. body-text scanning (URLs, file paths);
      4. title/topics/gist extraction from the body (the #140 core).

    Returns a locator dict only when at least one of
    source / path_or_url / title / topics / gist is usable — a bare key
    anchor alone doesn't reduce bloat on recall, so it's not sufficient.
    """
    body = _body_text(value)

    source_kind = _infer_source(value, body, source_name=source_name)

    path_or_url: Optional[str] = None
    if isinstance(value, dict):
        for src_key, dst in _STRUCTURED_FIELD_MAP:
            if dst != "path_or_url":
                continue
            nv = _norm_scalar(value.get(src_key))
            if nv and not path_or_url:
                path_or_url = nv
        loc0 = value.get("locator")
        if isinstance(loc0, dict):
            for k_candidate in ("path_or_url", "url", "path", "file_path"):
                nv = _norm_scalar(loc0.get(k_candidate))
                if nv and not path_or_url:
                    path_or_url = nv

    if body:
        um = _URL_RE.search(body)
        if um and not path_or_url:
            path_or_url = um.group(1)
        if not path_or_url:
            pm = _PATH_RE.search(body)
            if pm:
                p = pm.group(1).rstrip("/")
                if p and len(p) <= 300:
                    path_or_url = p

    title = extract_title(value, body=body)
    topics = extract_topics(value, body=body, title=title)
    gist = extract_gist(value, body=body)

    loc: Dict[str, Any] = {"source": source_kind}
    if path_or_url:
        loc["path_or_url"] = path_or_url
    if title:
        loc["title"] = title
    if topics:
        loc["topics"] = topics
    if gist:
        loc["gist"] = gist
    if key:
        loc["key"] = str(key).strip()

    has_content = any(k in loc for k in ("path_or_url", "title", "topics", "gist"))
    if not has_content:
        return None
    return loc


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _body_len(value: Any) -> int:
    try:
        return len(_body_text(value) or "")
    except Exception:
        return 0


def attach_locator(value: Any, key: str = "", source_name: str = "") -> Any:
    """Return *value* with a ``locator`` field stored alongside the body.

    Dict payloads get the locator merged in (a caller-provided ``locator``
    wins field-by-field, missing fields are back-filled from extraction).
    Non-dict payloads are wrapped as ``{"content": value, "locator": {...}}``
    — the same shape ``hooks._unwrap_content_field`` already understands —
    so the body stays fully recoverable and body-recall paths are unchanged.

    A locator is only attached when it is actually injectable on recall:
    the body needs to be at least ``LOCATOR_INJECT_THRESHOLD`` chars, OR the
    payload carries an explicit locator (the caller asserted its validity).
    This keeps the storage layer from inventing bloat on already-tiny bodies
    — the locator exists to cut bloat on recall, not to add it to storage.

    When nothing extractable exists the input is returned unchanged so
    ordinary saves (plain strings / dicts without provenance) stay
    byte-identical to before.
    """
    locator = extract_locator(value, key=key, source_name=source_name)
    if locator is None:
        return value
    body_len = _body_len(value)
    has_explicit = isinstance(value, dict) and bool(value.get("locator"))
    if body_len < LOCATOR_INJECT_THRESHOLD and not has_explicit:
        # Too small to ever be injected at recall — don't store a locator.
        return value
    try:
        if isinstance(value, dict):
            result = dict(value)
            if isinstance(value.get("locator"), dict):
                merged = dict(locator)
                for k, v in value["locator"].items():  # keep caller's explicit fields
                    merged.setdefault(str(k), v)
                result["locator"] = merged
            else:
                result["locator"] = locator
            return result
        return {"content": value, "locator": locator}
    except Exception:
        return value


def has_locator(item: Any) -> Optional[Dict[str, Any]]:
    """Locate a locator on a recall result (item dict or its payload) for the formatter.

    Checks: the item itself (search results may carry it at top level), then
    the item's ``content`` payload (what save() attached).  Returns the
    locator dict, or None.
    """
    if isinstance(item, dict):
        loc = item.get("locator")
        if isinstance(loc, dict) and loc:
            return loc
        if isinstance(loc, str) and loc.strip():
            return {"raw": loc.strip()}
        content = item.get("content")
        if isinstance(content, dict):
            cloc = content.get("locator")
            if isinstance(cloc, dict) and cloc:
                return cloc
            if isinstance(cloc, str) and cloc.strip():
                return {"raw": cloc.strip()}
    return None


# ---------------------------------------------------------------------------
# Recall formatting
# ---------------------------------------------------------------------------

def format_locator(locator: Dict[str, Any], source_name: Optional[str] = None,
                   key: Optional[str] = None) -> str:
    """Render a locator as a compact "go-read-it" line for recall injection.

    Layout: ``source=<kind> · <title> · <topics> → <path_or_url> — read it: retrieve(key='...')``
    (segments omitted when missing), hard-capped at LOCATOR_LINE_MAX_CHARS by
    trimming topics first, then the path/URL pointer.

    Never raises — degrades to ``key=<key>`` when the locator is empty.
    """
    if not isinstance(locator, dict) or not locator:
        identifier = key or (locator if isinstance(locator, str) else None) or "<unknown>"
        return f"key={identifier}"

    raw = locator.get("raw")
    if raw:
        return f"locator: {raw}"

    identifier = locator.get("key") or key
    source_kind = locator.get("source") or (source_name or "").lower()
    title = locator.get("title")
    topics = locator.get("topics") or []
    if isinstance(topics, str):
        topics = [t for t in re.split(r"[,;]\s*", topics) if t.strip()]
    path_or_url = locator.get("path_or_url")

    def _render(topics_list: List[str], pointer: Optional[str]) -> str:
        parts: List[str] = []
        if source_kind:
            parts.append(f"source={source_kind}")
        if title:
            parts.append(str(title))
        if topics_list:
            parts.append("topic: " + ", ".join(topics_list))
        if pointer:
            parts.append(f"→ {pointer}")
        line = " · ".join(parts) if parts else (str(identifier) if identifier else "locator")
        if identifier:
            line = f"{line} — read it: retrieve(key='{identifier}')"
        return line

    line = _render(list(topics), path_or_url)
    if len(line) <= LOCATOR_LINE_MAX_CHARS:
        return line

    # Trim topics first (fewest, then shortest), then drop the pointer.
    trimmed_topics = list(topics)
    while len(line) > LOCATOR_LINE_MAX_CHARS and trimmed_topics:
        trimmed_topics = trimmed_topics[:-1]
        line = _render(trimmed_topics, path_or_url)
    if len(line) > LOCATOR_LINE_MAX_CHARS:
        line = _render(trimmed_topics, None)
    if len(line) > LOCATOR_LINE_MAX_CHARS:
        line = line[:LOCATOR_LINE_MAX_CHARS].rstrip()
    return line


def should_inject_locator(content_text: str, locator: Optional[Dict[str, Any]]) -> bool:
    """True when recall should prefer the locator over the full body.

    Requires a usable locator and a body longer than LOCATOR_INJECT_THRESHOLD.
    Short bodies are left as-is — the locator exists to cut bloat, not to
    replace already-tiny context.
    """
    if not locator:
        return False
    return len(content_text or "") > LOCATOR_INJECT_THRESHOLD
