import logging
import re

import httpx

logger = logging.getLogger(__name__)

_DDG_URL = "https://api.duckduckgo.com/"
_DDG_HTML = "https://html.duckduckgo.com/html/"
_GH_RELEASES = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
_GH_TAGS = "https://api.github.com/repos/{owner}/{repo}/tags"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# DuckDuckGo HTML result snippets look like:
#   <a class="result__a" ...>Title</a> ... <a class="result__snippet" ...>Snippet</a>
_HTML_TITLE_RE = re.compile(r'class="result__a"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_HTML_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(s: str) -> str:
    s = _TAG_RE.sub("", s)
    return (
        s.replace("&amp;", "&")
        .replace("&#x27;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .strip()
    )


async def _ddg_html_search(query: str, max_results: int = 5) -> str:
    """Fallback: scrape DuckDuckGo's HTML endpoint for organic result snippets."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.post(
                _DDG_HTML,
                data={"q": query},
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            html = resp.text

        titles = [_clean_html(t) for t in _HTML_TITLE_RE.findall(html)]
        snippets = [_clean_html(s) for s in _HTML_SNIPPET_RE.findall(html)]

        parts = []
        for i in range(min(max_results, max(len(titles), len(snippets)))):
            title = titles[i] if i < len(titles) else ""
            snippet = snippets[i] if i < len(snippets) else ""
            line = f"- {title}: {snippet}".strip(" :-")
            if line:
                parts.append(line)
        return "\n".join(parts) if parts else "No web results found."
    except Exception as e:
        logger.warning(f"DDG HTML search failed: {e}")
        return f"Web results unavailable: {e}"


async def ddg_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGo search: Instant Answers API first, then HTML results fallback."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _DDG_URL,
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            )
            resp.raise_for_status()
            data = resp.json()

        parts = []
        if data.get("AbstractText"):
            parts.append(data["AbstractText"])
        if data.get("Answer"):
            parts.append(f"Answer: {data['Answer']}")
        for r in (data.get("RelatedTopics") or [])[:max_results]:
            if isinstance(r, dict) and r.get("Text"):
                parts.append(r["Text"])
        if parts:
            return "\n".join(parts)
    except Exception as e:
        logger.warning(f"DDG instant-answer search failed: {e}")

    # Instant Answers had nothing — fall back to HTML organic results.
    return await _ddg_html_search(query, max_results)


async def github_latest_release(owner: str, repo: str) -> str:
    """Fetch the latest release tag from a public GitHub repo."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _GH_RELEASES.format(owner=owner, repo=repo),
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return f"Latest release: {data.get('tag_name', 'unknown')} — published {data.get('published_at', '')[:10]}"
            # Fall back to tags
            resp2 = await client.get(
                _GH_TAGS.format(owner=owner, repo=repo),
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp2.status_code == 200:
                tags = resp2.json()
                if tags:
                    return f"Latest tag: {tags[0].get('name', 'unknown')}"
        return "No release info found."
    except Exception as e:
        logger.warning(f"GitHub release fetch failed: {e}")
        return f"GitHub API unavailable: {e}"


def _parse_github_url(text: str):
    """Extract owner/repo from a GitHub URL or 'owner/repo' string."""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s]+)", text)
    if m:
        return m.group(1), m.group(2).rstrip(".git")
    m2 = re.match(r"([a-zA-Z0-9_-]+)/([a-zA-Z0-9_.-]+)", text.strip())
    if m2:
        return m2.group(1), m2.group(2)
    return None, None


_KNOWN_REPOS = {
    "hashicorp vault": ("hashicorp", "vault"),
    "vault": ("hashicorp", "vault"),
    "terraform": ("hashicorp", "terraform"),
    "consul": ("hashicorp", "consul"),
    "nomad": ("hashicorp", "nomad"),
    "packer": ("hashicorp", "packer"),
}


async def search(query: str) -> str:
    """
    General-purpose search. Tries GitHub releases for known products first,
    then falls back to DuckDuckGo instant answers.
    """
    q_lower = query.lower()

    # Check known GitHub repos
    for keyword, (owner, repo) in _KNOWN_REPOS.items():
        if keyword in q_lower and ("release" in q_lower or "version" in q_lower or "latest" in q_lower):
            result = await github_latest_release(owner, repo)
            ddg = await ddg_search(query)
            return f"[GitHub API] {result}\n[DuckDuckGo] {ddg}"

    # Try to extract a GitHub URL from the query
    owner, repo = _parse_github_url(query)
    if owner and repo:
        return await github_latest_release(owner, repo)

    return await ddg_search(query)
