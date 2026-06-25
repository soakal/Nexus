import asyncio
import contextvars
import functools
import logging

import anthropic

logger = logging.getLogger(__name__)

# Carries the durable task_id of the in-flight orchestrated task. The orchestrator
# sets it around a durable run_task so nested router calls (plan/debug) see it.
# NOTE: the default ThreadPoolExecutor does NOT copy the contextvars Context across
# the loop->thread hop, so the best-effort spend write does NOT read this var
# directly — _run / run_with_tools capture the task_id on the event loop and thread
# it into _record_spend via functools.partial. None for non-task callers
# (chat/briefing single-shot calls).
_current_task_id: contextvars.ContextVar = contextvars.ContextVar(
    "nexus_task_id", default=None
)


def set_task_context(task_id):
    """Bind the current task_id for spend attribution; returns a reset Token."""
    return _current_task_id.set(task_id)


def reset_task_context(token) -> None:
    """Restore the task-id contextvar to its prior value via the Token."""
    _current_task_id.reset(token)


class TaskAborted(Exception):
    """Raised inside the tool-use loop when a task must stop mid-flight.

    `.reason` is "stopped" (kill switch / autonomy disabled) or "cancelled"
    (cooperative cancel). The orchestrator catches it and finalizes the task.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"task aborted: {reason}")

OPUS_MODEL = "claude-opus-4-8"
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Process-lifetime metering outcome counters (reset on restart — that's fine;
# they are a live health signal, not a durable ledger). Incrementing a dict
# value under CPython's GIL is safe here without a lock.
_METER_COUNTS: dict[str, int] = {
    "recorded": 0,
    "skipped_no_usage": 0,
    "skipped_unparseable": 0,
    "failed": 0,
}


def metering_counters() -> dict:
    """Return a snapshot of the process-lifetime metering outcome counters."""
    return dict(_METER_COUNTS)


# Price per 1,000,000 tokens (USD), keyed on the model constants above.
# Verified 2026-06-16 against Anthropic's official pricing page
# (platform.claude.com/docs/.../about-claude/pricing): Opus 4.8 $5/$25,
# Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5 per MTok. The cache multipliers in
# _compute_cost (5m write 1.25x input, read 0.1x input) also match the official
# rates. NOTE: the hosted web_search server tool ($10/1k searches) is NOT metered.
_PRICE_PER_MTOK = {
    OPUS_MODEL: {"input": 5.0, "output": 25.0},
    SONNET_MODEL: {"input": 3.0, "output": 15.0},
    HAIKU_MODEL: {"input": 1.0, "output": 5.0},
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
    are priced as FRACTIONS of the placeholder input rate: cache_creation at
    1.25x input, cache_read at 0.1x input. These multipliers are applied to the
    # VERIFY placeholder input rate — refine when real prices are confirmed.
    """
    price = _PRICE_PER_MTOK.get(model)
    if price is None:
        logger.warning(f"No price entry for model {model!r}; recording cost 0.0")
        return 0.0
    cost = (
        input_tokens / 1e6 * price["input"]
        + cache_creation / 1e6 * (price["input"] * 1.25)
        + cache_read / 1e6 * (price["input"] * 0.1)
        + output_tokens / 1e6 * price["output"]
    )
    return float(cost)


def _record_spend(model: str, resp, label: str, task_id=None) -> None:
    """Best-effort: insert a SpendLog row from a Messages API response.

    Whole body is wrapped in try/except — a logging failure (or an absent/odd
    usage field) must NEVER crash the LLM response. If usage tokens can't be
    coerced to int (e.g. a MagicMock test response), we treat it as "no usage"
    and write NO row.

    `task_id` is captured by the CALLER on the event loop (where the
    `_current_task_id` contextvar is set) and threaded down via functools.partial.
    This is the fallback path: the contextvar does NOT survive the default
    ThreadPoolExecutor hop (verified by test), so we pass the value explicitly
    rather than reading the contextvar here (which runs in the worker thread).
    """
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:
            _METER_COUNTS["skipped_no_usage"] += 1
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
            # usage WAS present but a token field can't be coerced (e.g. a
            # MagicMock test response, or a future/odd usage shape). Distinct from
            # usage-None above (legit, silent): warn here, then write NO row.
            _METER_COUNTS["skipped_unparseable"] += 1
            logger.warning(
                f"could not meter LLM call model={model!r} label={label!r}; "
                "usage shape unrecognized"
            )
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
                task_id=task_id,
            ))
            session.commit()
        _METER_COUNTS["recorded"] += 1
    except Exception as e:  # best-effort — never break the response
        _METER_COUNTS["failed"] += 1
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


def _as_cached_system(system):
    """Normalize system to a cached content-block list.

    A string becomes a single text block with cache_control so the static prefix
    caches. A list is passed through unchanged (caller already owns the breakpoints).
    """
    if isinstance(system, str):
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return system


def _with_tools_cache(tools: list) -> list:
    # ponytail: shallow-copies only the last dict — never mutates the shared tool_specs registry
    if not tools:
        return tools
    out = list(tools)
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


def _create_sync(model: str, max_tokens: int, prompt: str, system: str, web_search: bool = False, label: str = "", task_id=None) -> str:
    """Blocking Anthropic call. Must be run in an executor, never on the loop.

    `task_id` is captured on the event loop by `_run` and passed in here (the
    contextvar does not cross the executor hop) so the spend row is attributed.
    """
    client = get_client()
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = _as_cached_system(system)
    if web_search:
        kwargs["tools"] = [_WEB_SEARCH_TOOL]
    resp = client.messages.create(**kwargs)
    text = _extract_text(resp)
    # Best-effort spend logging. This runs INSIDE the executor worker thread
    # (loop.run_in_executor), NOT on the event loop — so a synchronous
    # Session(engine) write here is correct and must NOT be wrapped in
    # asyncio.to_thread. Do not "fix" this into to_thread.
    try:
        _record_spend(model, resp, label, task_id)
    except Exception as e:  # never let metering break the response
        logger.warning(f"spend logging failed (non-fatal): {e}")
    return text


async def _budget_brake() -> None:
    """Universal daily budget brake: before EVERY billed call, check the daily cap.

    A BudgetExceeded propagates (callers degrade gracefully); any OTHER governor
    error is swallowed so a governor bug can never DOS the assistant. Used by
    `_run` (single-shot chat/briefing calls that carry no task context).
    """
    from backend.safety.governor import BudgetExceeded, check_budget
    try:
        await asyncio.to_thread(check_budget)
    except BudgetExceeded:
        raise
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"daily budget check failed (non-fatal), proceeding: {e}")


async def _loop_guard(task_id, task_start) -> None:
    """Per-round guard for `run_with_tools`. Order (documented): BUDGET -> KILL -> CANCEL.

    1) check_budget(task_id, task_start) — daily always + per-task if task_start
       given. BudgetExceeded propagates (durable task finalizes failed/budget).
    2) Kill switch: if SystemState.autonomy_enabled is OFF, raise
       TaskAborted("stopped").
    3) Cancel: if task_id is set and the Task row has cancel_requested, raise
       TaskAborted("cancelled").

    Only BudgetExceeded + TaskAborted escape. Any OTHER governor/DB error is
    logged and swallowed so the loop proceeds (mirrors `_budget_brake`).
    With task_id=None (chat/briefing single calls) this is inert: the per-task
    cap is skipped, autonomy is NOT consulted, and cancel is not checked — only
    the daily cap applies.
    """
    from backend.safety.governor import BudgetExceeded, check_budget, get_system_state

    # 1) Budget (BudgetExceeded must propagate).
    try:
        await asyncio.to_thread(check_budget, task_id, task_start)
    except BudgetExceeded:
        raise
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"loop budget check failed (non-fatal), proceeding: {e}")

    if task_id is None:
        return

    # 2) Kill switch.
    try:
        state = await asyncio.to_thread(get_system_state)
        autonomy_enabled = state["autonomy_enabled"]
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"loop kill-switch check failed (non-fatal), proceeding: {e}")
        autonomy_enabled = True
    if not autonomy_enabled:
        raise TaskAborted("stopped")

    # 3) Cooperative cancel.
    try:
        from backend.agents.orchestrator import _is_cancel_requested
        cancelled = await asyncio.to_thread(_is_cancel_requested, task_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"loop cancel check failed (non-fatal), proceeding: {e}")
        cancelled = False
    if cancelled:
        raise TaskAborted("cancelled")


async def _run(model: str, max_tokens: int, prompt: str, system: str, web_search: bool = False, label: str = "") -> str:
    """Run the blocking SDK call in the default thread-pool executor.

    The sync `anthropic.Anthropic` client wrapped in `run_in_executor` is more
    reliable here than `AsyncAnthropic`, which has been observed blocking the
    event loop during briefings.
    """
    await _budget_brake()

    # Capture the task_id contextvar HERE (on the event loop, where it is set);
    # it does not survive the run_in_executor hop, so we thread it down explicitly.
    task_id = _current_task_id.get()

    loop = asyncio.get_event_loop()
    func = functools.partial(_create_sync, model, max_tokens, prompt, system, web_search, label, task_id)
    return await loop.run_in_executor(None, func)


def _create_sync_raw(model: str, max_tokens: int, messages: list, system: str, tools: list, label: str, task_id=None):
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
        kwargs["system"] = _as_cached_system(system)
    if tools:
        kwargs["tools"] = tools
    resp = client.messages.create(**kwargs)
    # Best-effort spend logging — runs INSIDE the executor worker thread (NOT the
    # event loop), so the synchronous Session(engine) write inside _record_spend
    # is correct here and must NOT be wrapped in asyncio.to_thread. task_id is
    # captured by run_with_tools on the loop and threaded in (contextvar does not
    # cross the executor hop).
    try:
        _record_spend(model, resp, label, task_id)
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
    task_id=None,
    task_start=None,
) -> str:
    """Native tool-use loop over READ-ONLY tools.

    Drives a multi-round conversation: Claude may call any of the provided tools,
    we dispatch each call and feed the result back, until Claude returns a final
    text answer (stop_reason != "tool_use") or `max_rounds` is reached. When
    `web_search` is True, Anthropic's hosted web search tool is added alongside the
    local custom tools.

    Metering parity with `_run`: the per-round guard (`_loop_guard`) runs BEFORE
    every billed round and each create() records spend AFTER (inside
    `_create_sync_raw`). The guard enforces BUDGET -> KILL -> CANCEL: a
    `BudgetExceeded` propagates (durable task finalizes failed/budget_exceeded);
    a `TaskAborted` propagates (durable task finalizes 'stopped'). With
    `task_id=None` (chat/briefing) the guard is the daily-cap brake only — kill
    switch + cancel are not consulted.
    """
    messages: list = [{"role": "user", "content": prompt}]
    tools = _with_tools_cache(([_WEB_SEARCH_TOOL] if web_search else []) + list(tool_specs))

    # Prefer the explicit task_id param; fall back to the contextvar (set by the
    # orchestrator). Captured on the loop and threaded into each create() call
    # since the contextvar does not survive the run_in_executor hop.
    spend_task_id = task_id if task_id is not None else _current_task_id.get()

    loop = asyncio.get_event_loop()
    last_resp = None

    for _round in range(max_rounds):
        await _loop_guard(task_id, task_start)
        resp = await loop.run_in_executor(
            None,
            functools.partial(_create_sync_raw, model, max_tokens, messages, system, tools, label, spend_task_id),
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


def _create_streaming_sync(model: str, max_tokens: int, prompt: str, system: str, web_search: bool, loop, q) -> None:
    """Executor thread: streams from Anthropic and deposits events into an asyncio.Queue."""
    client = get_client()
    kwargs: dict = {
        "model": model, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = _as_cached_system(system)
    if web_search:
        kwargs["tools"] = [_WEB_SEARCH_TOOL]
    try:
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                loop.call_soon_threadsafe(q.put_nowait, ("token", text))
            loop.call_soon_threadsafe(q.put_nowait, ("done", stream.get_final_message()))
    except Exception as e:
        loop.call_soon_threadsafe(q.put_nowait, ("error", str(e)))


async def stream_sonnet(prompt: str, system: str = "", web_search: bool = False):
    """Async generator yielding text tokens streamed from Sonnet. Budget-gated."""
    await _budget_brake()
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    task_id = _current_task_id.get()
    fut = loop.run_in_executor(
        None,
        functools.partial(_create_streaming_sync, SONNET_MODEL, 8192, prompt, system, web_search, loop, q),
    )
    while True:
        kind, data = await q.get()
        if kind == "token":
            yield data
        elif kind == "done":
            await loop.run_in_executor(None, functools.partial(_record_spend, SONNET_MODEL, data, "stream_sonnet", task_id))
            break
        elif kind == "error":
            await fut
            raise RuntimeError(data)
    await fut


async def opus(prompt: str, system: str = "", web_search: bool = False, label: str = "") -> str:
    return await _run(OPUS_MODEL, 8192, prompt, system, web_search, label)


async def sonnet(prompt: str, system: str = "", web_search: bool = False, label: str = "") -> str:
    return await _run(SONNET_MODEL, 8192, prompt, system, web_search, label)


async def haiku(prompt: str, system: str = "", label: str = "") -> str:
    return await _run(HAIKU_MODEL, 4096, prompt, system, label=label)


async def run_model(
    model: str, prompt: str, system: str = "", web_search: bool = False,
    label: str = "", max_tokens: int = 8192,
) -> str:
    """Run an arbitrary model id through the metered _run path.

    Lets callers (e.g. the orchestrator's configurable planner/debug roles) pick
    the model at runtime from config instead of being hard-wired to opus/sonnet.
    Pricing/metering works for any model in _PRICE_PER_MTOK; unknown models meter
    as no-cost (no SpendLog row) but still run.
    """
    return await _run(model, max_tokens, prompt, system, web_search, label)
