import asyncio
import functools
import logging

import anthropic

logger = logging.getLogger(__name__)

OPUS_MODEL = "claude-opus-4-8"
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"


# Price per 1,000,000 tokens (USD), keyed on the model constants above.
# VERIFY against current Anthropic pricing — these are placeholders.
_PRICE_PER_MTOK = {
    OPUS_MODEL: {"input": 15.0, "output": 75.0},
    SONNET_MODEL: {"input": 3.0, "output": 15.0},
    HAIKU_MODEL: {"input": 0.80, "output": 4.0},
}


def _compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> float:
    """Estimate USD cost of one billed call from token usage.

    Returns 0.0 for an unknown model (logged) rather than raising. Cache tokens
    are folded into the input rate — an approximation (cache-write and cache-read
    are actually priced differently); refine later when prices are verified.
    """
    price = _PRICE_PER_MTOK.get(model)
    if price is None:
        logger.warning(f"No price entry for model {model!r}; recording cost 0.0")
        return 0.0
    cost = (
        (input_tokens + cache_creation + cache_read) / 1e6 * price["input"]
        + output_tokens / 1e6 * price["output"]
    )
    return float(cost)


def _record_spend(model: str, resp, label: str) -> None:
    """Best-effort: insert a SpendLog row from a Messages API response.

    Whole body is wrapped in try/except — a logging failure (or an absent/odd
    usage field) must NEVER crash the LLM response. If usage tokens can't be
    coerced to int (e.g. a MagicMock test response), we treat it as "no usage"
    and write NO row.
    """
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return

        def _coerce(name):
            """Return a real int token count, or raise to signal 'no usage'.

            We require the raw attribute to be a genuine numeric type. A real
            Anthropic usage exposes plain ints; a MagicMock (used by the existing
            test_router.py suite) exposes auto-attributes that are technically
            int()-coercible (int(MagicMock()) == 1) but are NOT real usage — so
            we reject anything that isn't an int/float/str and treat the whole
            response as having no usage (writes NO row)."""
            raw = getattr(usage, name, 0)
            if raw is None:
                return 0
            if not isinstance(raw, (int, float, str)):
                raise TypeError(f"usage.{name} is not numeric: {type(raw)!r}")
            return int(raw or 0)

        try:
            input_tokens = _coerce("input_tokens")
            output_tokens = _coerce("output_tokens")
            cache_creation = _coerce("cache_creation_input_tokens")
            cache_read = _coerce("cache_read_input_tokens")
        except (TypeError, ValueError):
            # Non-numeric usage (e.g. MagicMock) -> treat as no usage, no row.
            return

        cost = _compute_cost(model, input_tokens, output_tokens, cache_creation, cache_read)

        from sqlmodel import Session

        from backend.database import SpendLog, engine

        with Session(engine) as session:
            session.add(SpendLog(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
                cost_usd=cost,
                label=label or "",
            ))
            session.commit()
    except Exception as e:  # best-effort — never break the response
        logger.warning(f"_record_spend failed (non-fatal): {e}")


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


def _create_sync(model: str, max_tokens: int, prompt: str, system: str, web_search: bool = False, label: str = "") -> str:
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
    text = _extract_text(resp)
    # Best-effort spend logging. This runs INSIDE the executor worker thread
    # (loop.run_in_executor), NOT on the event loop — so a synchronous
    # Session(engine) write here is correct and must NOT be wrapped in
    # asyncio.to_thread. Do not "fix" this into to_thread.
    try:
        _record_spend(model, resp, label)
    except Exception as e:  # never let metering break the response
        logger.warning(f"spend logging failed (non-fatal): {e}")
    return text


async def _budget_brake() -> None:
    """Universal daily budget brake: before EVERY billed call, check the daily cap.

    A BudgetExceeded propagates (callers degrade gracefully); any OTHER governor
    error is swallowed so a governor bug can never DOS the assistant. Shared by
    both `_run` (single-shot calls) and `run_with_tools` (the tool-use loop).
    """
    from backend.safety.governor import BudgetExceeded, check_budget
    try:
        await asyncio.to_thread(check_budget)
    except BudgetExceeded:
        raise
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"daily budget check failed (non-fatal), proceeding: {e}")


async def _run(model: str, max_tokens: int, prompt: str, system: str, web_search: bool = False, label: str = "") -> str:
    """Run the blocking SDK call in the default thread-pool executor.

    The sync `anthropic.Anthropic` client wrapped in `run_in_executor` is more
    reliable here than `AsyncAnthropic`, which has been observed blocking the
    event loop during briefings.
    """
    await _budget_brake()

    loop = asyncio.get_event_loop()
    func = functools.partial(_create_sync, model, max_tokens, prompt, system, web_search, label)
    return await loop.run_in_executor(None, func)


def _create_sync_raw(model: str, max_tokens: int, messages: list, system: str, tools: list, label: str):
    """Blocking Anthropic call for the tool-use loop. Returns the RAW response.

    Mirrors `_create_sync` but (1) takes a full `messages` list (not a single
    prompt) and a `tools` list, and (2) returns the raw Messages API response so
    the caller can inspect `stop_reason` / tool_use blocks. Spend is recorded
    in-thread (best-effort) exactly as in `_create_sync`. Must run in an executor,
    never on the event loop.
    """
    client = get_client()
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    resp = client.messages.create(**kwargs)
    # Best-effort spend logging — runs INSIDE the executor worker thread (NOT the
    # event loop), so the synchronous Session(engine) write inside _record_spend
    # is correct here and must NOT be wrapped in asyncio.to_thread.
    try:
        _record_spend(model, resp, label)
    except Exception as e:  # never let metering break the response
        logger.warning(f"spend logging failed (non-fatal): {e}")
    return resp


async def run_with_tools(
    model: str,
    max_tokens: int,
    prompt: str,
    system: str,
    tool_specs: list,
    dispatch: dict,
    *,
    web_search: bool = False,
    label: str = "",
    max_rounds: int = 5,
) -> str:
    """Native tool-use loop over READ-ONLY tools.

    Drives a multi-round conversation: Claude may call any of the provided tools,
    we dispatch each call and feed the result back, until Claude returns a final
    text answer (stop_reason != "tool_use") or `max_rounds` is reached. When
    `web_search` is True, Anthropic's hosted web search tool is added alongside the
    local custom tools.

    Metering parity with `_run`: the daily budget brake runs BEFORE every billed
    round (`_budget_brake`), and each create() records spend AFTER (inside
    `_create_sync_raw`). A `BudgetExceeded` from the brake propagates out so a
    durable task finalizes failed/budget_exceeded.
    """
    messages: list = [{"role": "user", "content": prompt}]
    tools = ([_WEB_SEARCH_TOOL] if web_search else []) + list(tool_specs)

    loop = asyncio.get_event_loop()
    last_resp = None

    for _round in range(max_rounds):
        await _budget_brake()
        resp = await loop.run_in_executor(
            None,
            functools.partial(_create_sync_raw, model, max_tokens, messages, system, tools, label),
        )
        last_resp = resp

        # Record the assistant turn verbatim (raw content blocks) so tool_result
        # turns reference valid tool_use ids on the next request.
        messages.append({"role": "assistant", "content": resp.content})

        tool_use_blocks = [
            b for b in resp.content if getattr(b, "type", None) == "tool_use"
        ]
        if getattr(resp, "stop_reason", None) != "tool_use" or not tool_use_blocks:
            return _extract_text(resp)

        tool_results = []
        for block in tool_use_blocks:
            name = getattr(block, "name", "")
            tid = getattr(block, "id", "")
            raw_input = getattr(block, "input", None)
            tinput = raw_input if isinstance(raw_input, dict) else {}
            fn = dispatch.get(name)
            if fn is None:
                result = "unknown tool: " + str(name)
            else:
                try:
                    result = await fn(tinput)
                except Exception as e:
                    result = f"{name} unavailable: {e}"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tid,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    # Ran out of rounds while Claude still wanted to call tools.
    text = _extract_text(last_resp) if last_resp is not None else ""
    return text if text else "(tool loop reached max rounds without a final answer)"


async def opus(prompt: str, system: str = "", web_search: bool = False, label: str = "") -> str:
    return await _run(OPUS_MODEL, 8192, prompt, system, web_search, label)


async def sonnet(prompt: str, system: str = "", web_search: bool = False, label: str = "") -> str:
    return await _run(SONNET_MODEL, 8192, prompt, system, web_search, label)


async def haiku(prompt: str, system: str = "", label: str = "") -> str:
    return await _run(HAIKU_MODEL, 4096, prompt, system, label=label)
