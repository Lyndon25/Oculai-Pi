"""Person-name extraction from search-result titles and snippets.

Shared helper used by the web-search data sources (Firecrawl, Baidu,
DuckDuckGo) to pull a plausible person's name out of a result title or
snippet before upserting a candidate.

This module centralizes the three regexes and the two heuristic functions
that were previously duplicated verbatim across ``sources/firecrawl.py``,
``sources/baidu.py`` and ``sources/duckduckgo.py``.

Regex note (F5 fix): the separator pattern uses ``\\s*`` (optional
whitespace) on both sides of the separator rather than ``\\s+``
(required whitespace). The previous ``\\s+`` form dropped recall for
no-space titles such as ``"zhangsan-个人主页"`` where the name and
separator are adjacent. The wider ``.{1,60}?`` capture range is retained.
"""

import re

# Name followed by a separator (e.g. "张三 - 个人主页", "John Doe | LinkedIn",
# "zhangsan-个人主页"). ``\s*`` restores no-space recall (F5).
_NAME_SEPARATOR_RE = re.compile(r"^(.{1,60}?)\s*[|·-]\s*")

# Chinese name at the very start of the title (2-4 hanzi).
_CHINESE_NAME_RE = re.compile(r"^[一-鿿]{2,4}")

# "作者：xxx" / "by xxx" / "writer: xxx" prefix inside a snippet.
_AUTHOR_PREFIX_RE = re.compile(r"(?:作者|by|writer)[:\s]*(.{2,30})", re.I)


def is_likely_person_name(text: str) -> bool:
    """Quick heuristic: does this look like a person name?

    Rejects empty/over-long strings, strings containing article/title
    markers (《》「」『』), strings with a colon followed by 3+ characters
    (likely a subtitle, not a name), strings without any alphabetic or
    CJK characters, and pure-digit strings.
    """
    if not text or len(text) < 2 or len(text) > 30:
        return False
    if any(c in text for c in "《》「」『』"):
        return False
    if re.search(r"[:：].{3,}", text):
        return False
    if not re.search(r"[a-zA-Z一-鿿]", text):
        return False
    if text.isdigit():
        return False
    return True


def extract_person_name_from_title(title: str, snippet: str) -> str | None:
    """Try to extract a person's name from a search result title/snippet.

    Returns the name if a reasonable person name is found, otherwise
    ``None`` so callers can fall back to ``"Unknown"``.
    """
    if not title:
        return None

    title = title.strip()

    # Pattern 1: name before separator (e.g., "张三 - 个人主页", "John Doe | LinkedIn")
    m = _NAME_SEPARATOR_RE.match(title)
    if m:
        candidate = m.group(1).strip()
        if is_likely_person_name(candidate):
            return candidate

    # Pattern 2: Chinese name at the very start (2-4 hanzi)
    m = _CHINESE_NAME_RE.match(title)
    if m:
        return m.group(0)

    # Pattern 3: "作者：xxx" or "by xxx" in snippet
    if snippet:
        m = _AUTHOR_PREFIX_RE.search(snippet)
        if m:
            candidate = m.group(1).strip()
            if is_likely_person_name(candidate):
                return candidate

    return None
