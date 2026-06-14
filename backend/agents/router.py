import asyncio
import functools
import logging

import anthropic

logger = logging.getLogger(__name__)

OPUS_MODEL = "claude-opus-4-8"
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"


def get_client() -> anthropic.Anthropic:
    from backend.config import get_settings
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)


# Anthropic's hosted web search tool — the same live search Claude.ai uses. When
# enabled, Claude decides when to search, runs it server-side, and returns the
# final answer (with citations) in one call. max_uses caps searches per turn.
_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


def _extract_text(resp) -> str:
    """Join all text blocks from a Messages API response.

    Opus 4.8 can prepend non-text blocks (e.g. thinking), and the web search tool
    interleaves text with server_tool_use / web_search_tool_result blocks — so we
    collect every text block rather than assuming content[0] is the answer.
    """
    parts = [
        block.text
        for block in resp.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", "")
    ]
    return "\n".join(parts).strip()


def _create_sync(model: str, max_tokens: int, prompt: str, system: str, web_search: bool = False) -> str:
    """Blocking Anthropic call. Must be run in an executor, never on the loop."""
    client = get_client()
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if web_search:
        kwargs["tools"] = [_WEB_SEARCH_TOOL]
    resp = client.messages.create(**kwargs)
    return _extract_text(resp)


async def _run(model: str, max_tokens: int, prompt: str, system: str, web_search: bool = False) -> str:
    """Run the blocking SDK call in the default thread-pool executor.

    The sync `anthropic.Anthropic` client wrapped in `run_in_executor` is more
    reliable here than `AsyncAnthropic`, which has been observed blocking the
    event loop during briefings.
    """
    loop = asyncio.get_event_loop()
    func = functools.partial(_create_sync, model, max_tokens, prompt, system, web_search)
    return await loop.run_in_executor(None, func)


async def opus(prompt: str, system: str = "", web_search: bool = False) -> str:
    return await _run(OPUS_MODEL, 8192, prompt, system, web_search)


async def sonnet(prompt: str, system: str = "", web_search: bool = False) -> str:
    return await _run(SONNET_MODEL, 8192, prompt, system, web_search)


async def haiku(prompt: str, system: str = "") -> str:
    return await _run(HAIKU_MODEL, 4096, prompt, system)
