"""Unit tests for backend/integrations/web_search.py.

The module was almost entirely uncovered. These tests exercise every function
with httpx fully mocked so no real network call is ever made:
  - _clean_html tag/entity stripping
  - _ddg_html_search organic-result scraping (hit, empty, error paths)
  - ddg_search instant-answer path + HTML fallback + error fallback
  - github_latest_release (releases hit, tags fallback, no info, error)
  - _parse_github_url extraction variants
  - search() routing (known repo, GitHub URL, plain DDG)
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations import web_search


def _mock_client(get_side=None, post_side=None):
    """Build a MagicMock that mimics `async with httpx.AsyncClient() as c`."""
    client = AsyncMock()
    ctx = client.__aenter__.return_value
    if get_side is not None:
        ctx.get = AsyncMock(side_effect=get_side)
    if post_side is not None:
        ctx.post = AsyncMock(side_effect=post_side)
    return client


def _resp(*, status_code=200, json_data=None, text=""):
    r = MagicMock(status_code=status_code)
    r.json.return_value = json_data
    r.text = text
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------- _clean_html

def test_clean_html_strips_tags_and_entities():
    raw = '<b>Tom &amp; Jerry&#x27;s</b> &quot;show&quot; &lt;x&gt;&nbsp;end'
    assert web_search._clean_html(raw) == 'Tom & Jerry\'s "show" <x> end'


def test_clean_html_trims_whitespace():
    assert web_search._clean_html("  <span>hi</span>  ") == "hi"


# ------------------------------------------------------------ _ddg_html_search

@pytest.mark.asyncio
async def test_ddg_html_search_parses_titles_and_snippets():
    html = (
        '<a class="result__a" href="#">First <b>Title</b></a>'
        '<a class="result__snippet" href="#">First snippet</a>'
        '<a class="result__a" href="#">Second Title</a>'
        '<a class="result__snippet" href="#">Second snippet</a>'
    )
    with patch("httpx.AsyncClient", return_value=_mock_client(post_side=[_resp(text=html)])):
        out = await web_search._ddg_html_search("q", max_results=5)
    assert "First Title: First snippet" in out
    assert "Second Title: Second snippet" in out


@pytest.mark.asyncio
async def test_ddg_html_search_respects_max_results():
    html = "".join(
        f'<a class="result__a" href="#">T{i}</a><a class="result__snippet" href="#">S{i}</a>'
        for i in range(5)
    )
    with patch("httpx.AsyncClient", return_value=_mock_client(post_side=[_resp(text=html)])):
        out = await web_search._ddg_html_search("q", max_results=2)
    assert len(out.splitlines()) == 2


@pytest.mark.asyncio
async def test_ddg_html_search_no_results_message():
    with patch("httpx.AsyncClient", return_value=_mock_client(post_side=[_resp(text="<html></html>")])):
        out = await web_search._ddg_html_search("q")
    assert out == "No web results found."


@pytest.mark.asyncio
async def test_ddg_html_search_error_returns_unavailable():
    with patch("httpx.AsyncClient", return_value=_mock_client(post_side=Exception("boom"))):
        out = await web_search._ddg_html_search("q")
    assert out.startswith("Web results unavailable:")
    assert "boom" in out


# ------------------------------------------------------------------ ddg_search

@pytest.mark.asyncio
async def test_ddg_search_instant_answer_abstract_and_related():
    data = {
        "AbstractText": "An abstract.",
        "Answer": "42",
        "RelatedTopics": [
            {"Text": "topic one"},
            {"Text": "topic two"},
            {"NoText": "ignored"},  # dict without Text is skipped
            "not-a-dict",           # non-dict is skipped
        ],
    }
    with patch("httpx.AsyncClient", return_value=_mock_client(get_side=[_resp(json_data=data)])):
        out = await web_search.ddg_search("q", max_results=5)
    assert "An abstract." in out
    assert "Answer: 42" in out
    assert "topic one" in out and "topic two" in out


@pytest.mark.asyncio
async def test_ddg_search_empty_instant_answer_falls_back_to_html():
    empty = _resp(json_data={})
    with patch("httpx.AsyncClient", return_value=_mock_client(get_side=[empty])), \
         patch.object(web_search, "_ddg_html_search", new=AsyncMock(return_value="HTML RESULTS")) as html:
        out = await web_search.ddg_search("q", max_results=3)
    assert out == "HTML RESULTS"
    html.assert_awaited_once_with("q", 3)


@pytest.mark.asyncio
async def test_ddg_search_error_falls_back_to_html():
    with patch("httpx.AsyncClient", return_value=_mock_client(get_side=Exception("down"))), \
         patch.object(web_search, "_ddg_html_search", new=AsyncMock(return_value="FALLBACK")):
        out = await web_search.ddg_search("q")
    assert out == "FALLBACK"


# --------------------------------------------------------- github_latest_release

@pytest.mark.asyncio
async def test_github_latest_release_from_releases_endpoint():
    rel = _resp(json_data={"tag_name": "v1.2.3", "published_at": "2024-01-05T00:00:00Z"})
    with patch("httpx.AsyncClient", return_value=_mock_client(get_side=[rel])):
        out = await web_search.github_latest_release("owner", "repo")
    assert "Latest release: v1.2.3" in out
    assert "2024-01-05" in out


@pytest.mark.asyncio
async def test_github_latest_release_falls_back_to_tags():
    releases_404 = _resp(status_code=404, json_data={})
    tags_ok = _resp(json_data=[{"name": "v9.9.9"}])
    with patch("httpx.AsyncClient", return_value=_mock_client(get_side=[releases_404, tags_ok])):
        out = await web_search.github_latest_release("owner", "repo")
    assert out == "Latest tag: v9.9.9"


@pytest.mark.asyncio
async def test_github_latest_release_no_info_when_both_fail():
    releases_404 = _resp(status_code=404)
    tags_empty = _resp(status_code=404)
    with patch("httpx.AsyncClient", return_value=_mock_client(get_side=[releases_404, tags_empty])):
        out = await web_search.github_latest_release("owner", "repo")
    assert out == "No release info found."


@pytest.mark.asyncio
async def test_github_latest_release_empty_tags_list_returns_no_info():
    releases_404 = _resp(status_code=404)
    tags_ok_empty = _resp(status_code=200, json_data=[])
    with patch("httpx.AsyncClient", return_value=_mock_client(get_side=[releases_404, tags_ok_empty])):
        out = await web_search.github_latest_release("owner", "repo")
    assert out == "No release info found."


@pytest.mark.asyncio
async def test_github_latest_release_error_returns_unavailable():
    with patch("httpx.AsyncClient", return_value=_mock_client(get_side=Exception("kaboom"))):
        out = await web_search.github_latest_release("owner", "repo")
    assert out.startswith("GitHub API unavailable:")
    assert "kaboom" in out


# ------------------------------------------------------------ _parse_github_url

def test_parse_github_url_full_url_strips_git_suffix():
    # Regression: .rstrip(".git") used to eat a trailing t/i/g/. from the repo
    # name (vault -> vaul); the suffix must be removed as a whole token only.
    owner, repo = web_search._parse_github_url("see https://github.com/hashicorp/vault.git for info")
    assert (owner, repo) == ("hashicorp", "vault")


def test_parse_github_url_url_without_git_suffix_keeps_name():
    owner, repo = web_search._parse_github_url("https://github.com/hashicorp/consul")
    assert (owner, repo) == ("hashicorp", "consul")


def test_parse_github_url_owner_slash_repo():
    owner, repo = web_search._parse_github_url("hashicorp/terraform")
    assert (owner, repo) == ("hashicorp", "terraform")


def test_parse_github_url_no_match():
    assert web_search._parse_github_url("just some words") == (None, None)


# ------------------------------------------------------------------- search()

@pytest.mark.asyncio
async def test_search_known_repo_release_combines_github_and_ddg():
    with patch.object(web_search, "github_latest_release", new=AsyncMock(return_value="REL")) as gh, \
         patch.object(web_search, "ddg_search", new=AsyncMock(return_value="DDG")) as ddg:
        out = await web_search.search("latest terraform release")
    gh.assert_awaited_once_with("hashicorp", "terraform")
    ddg.assert_awaited_once()
    assert out == "[GitHub API] REL\n[DuckDuckGo] DDG"


@pytest.mark.asyncio
async def test_search_github_url_uses_release_only():
    with patch.object(web_search, "github_latest_release", new=AsyncMock(return_value="REL")) as gh, \
         patch.object(web_search, "ddg_search", new=AsyncMock()) as ddg:
        out = await web_search.search("https://github.com/foo/bar")
    gh.assert_awaited_once_with("foo", "bar")
    ddg.assert_not_awaited()
    assert out == "REL"


@pytest.mark.asyncio
async def test_search_plain_query_uses_ddg():
    with patch.object(web_search, "ddg_search", new=AsyncMock(return_value="DDG")) as ddg:
        out = await web_search.search("what is the weather like today")
    ddg.assert_awaited_once_with("what is the weather like today")
    assert out == "DDG"
