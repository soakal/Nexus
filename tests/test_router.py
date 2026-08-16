import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_opus_call():
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="Opus response")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        result = await router.opus("Test prompt")
        assert result == "Opus response"
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["model"] == router.OPUS_MODEL


@pytest.mark.asyncio
async def test_sonnet_call():
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="Sonnet response")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        result = await router.sonnet("Test prompt")
        assert result == "Sonnet response"
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["model"] == router.SONNET_MODEL


@pytest.mark.asyncio
async def test_haiku_call():
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="Haiku response")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        result = await router.haiku("Test prompt")
        assert result == "Haiku response"
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["model"] == router.HAIKU_MODEL


@pytest.mark.asyncio
async def test_router_uses_system_prompt():
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="Response")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        await router.opus("prompt", system="You are an expert")
        call_kwargs = mock_client.messages.create.call_args[1]
        # system is now a cached content-block list; verify the text is preserved
        sent = call_kwargs.get("system")
        assert isinstance(sent, list) and sent[0]["text"] == "You are an expert"


@pytest.mark.asyncio
async def test_router_no_system_prompt_omits_key():
    """When system is empty, 'system' key must NOT be sent to the API."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="Response")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        await router.opus("prompt")
        call_kwargs = mock_client.messages.create.call_args[1]
        assert "system" not in call_kwargs


@pytest.mark.asyncio
async def test_opus_max_tokens():
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="ok")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        await router.opus("prompt")
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_haiku_max_tokens():
    """Haiku uses a lower max_tokens limit than Opus/Sonnet."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="ok")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        await router.haiku("prompt")
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_sonnet_max_tokens():
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="ok")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        await router.sonnet("prompt")
        call_kwargs = mock_client.messages.create.call_args[1]
        assert call_kwargs["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_router_messages_structure():
    """All callers must send a single user-role message."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="ok")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        from backend.agents import router
        await router.sonnet("hello world")
        call_kwargs = mock_client.messages.create.call_args[1]
        messages = call_kwargs["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello world"


def test_router_model_constants_are_strings():
    from backend.agents import router
    assert isinstance(router.OPUS_MODEL, str)
    assert isinstance(router.SONNET_MODEL, str)
    assert isinstance(router.HAIKU_MODEL, str)


def test_get_client_uses_api_key(monkeypatch):
    """get_client must pass the API key from settings to the Anthropic constructor."""
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value = MagicMock()
        from backend.agents.router import get_client
        client = get_client()
        call_kwargs = mock_anthropic.call_args[1]
        assert call_kwargs.get("api_key") == "sk-ant-test-key"


# ---------------------------------------------------------------------------
# Tier A5 — hosted web_search billed into SpendLog
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool


@pytest.fixture
def spend_eng(monkeypatch):
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr("backend.database.engine", eng)
    return eng


def _spend_rows(eng):
    from backend.database import SpendLog
    with Session(eng) as s:
        return s.exec(select(SpendLog)).all()


def _resp_with_usage(**usage_fields):
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        **usage_fields,
    )
    return SimpleNamespace(usage=usage)


def test_web_search_cost_metered(spend_eng):
    from backend.agents import router
    resp = _resp_with_usage(
        server_tool_use=SimpleNamespace(web_search_requests=3)
    )
    router._record_spend(router.SONNET_MODEL, resp, "test_ws")

    rows = _spend_rows(spend_eng)
    assert len(rows) == 1
    token_only = router._compute_cost(router.SONNET_MODEL, 1000, 500, 0, 0)
    assert rows[0].cost_usd == pytest.approx(token_only + 3 * 0.01)


def test_no_web_search_no_extra_cost(spend_eng):
    from backend.agents import router
    resp = _resp_with_usage()  # no server_tool_use at all
    router._record_spend(router.SONNET_MODEL, resp, "test_no_ws")

    rows = _spend_rows(spend_eng)
    assert len(rows) == 1
    token_only = router._compute_cost(router.SONNET_MODEL, 1000, 500, 0, 0)
    assert rows[0].cost_usd == pytest.approx(token_only)


def test_web_search_count_magicmock_ignored(spend_eng):
    """A MagicMock web_search_requests must not leak a bogus cost."""
    from backend.agents import router
    resp = _resp_with_usage(
        server_tool_use=SimpleNamespace(web_search_requests=MagicMock())
    )
    router._record_spend(router.SONNET_MODEL, resp, "test_mock_ws")

    rows = _spend_rows(spend_eng)
    assert len(rows) == 1
    token_only = router._compute_cost(router.SONNET_MODEL, 1000, 500, 0, 0)
    assert rows[0].cost_usd == pytest.approx(token_only)


def test_web_search_metering_never_raises(spend_eng):
    """server_tool_use whose attribute access explodes -> token-only row."""
    from backend.agents import router

    class _Explodes:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    resp = _resp_with_usage(server_tool_use=_Explodes())
    router._record_spend(router.SONNET_MODEL, resp, "test_boom_ws")

    rows = _spend_rows(spend_eng)
    assert len(rows) == 1
    token_only = router._compute_cost(router.SONNET_MODEL, 1000, 500, 0, 0)
    assert rows[0].cost_usd == pytest.approx(token_only)


# ---------------------------------------------------------------------------
# Tier B3 + B9 — tool-loop caching breakpoint + injection sentinels
# ---------------------------------------------------------------------------

def _tool_use_resp(tid="tu_1", name="fake_tool"):
    block = SimpleNamespace(type="tool_use", id=tid, name=name, input={})
    return SimpleNamespace(content=[block], stop_reason="tool_use", usage=None)


def _final_resp(text="final answer"):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block], stop_reason="end_turn", usage=None)


@pytest.mark.asyncio
async def test_tool_result_wrapped_and_cached(spend_eng):
    """The appended tool_result turn must sentinel-wrap content (B9) and put a
    cache breakpoint on the newest block (B3)."""
    from backend.agents import router

    captured_messages = []

    def _fake_create(model, max_tokens, messages, system, tools, label, task_id, trace_id=None, parent_span_id=None):
        captured_messages.append([dict(m) if isinstance(m, dict) else m for m in messages])
        return _tool_use_resp() if len(captured_messages) == 1 else _final_resp()

    async def _dispatch(_input):
        return "tool says hello"

    with patch.object(router, "_create_sync_raw", _fake_create):
        out = await router.run_with_tools(
            "m", 100, "prompt", "sys", [{"name": "fake_tool"}],
            {"fake_tool": _dispatch},
        )

    assert out == "final answer"
    # Second round's messages include the tool_result user turn
    tool_turn = captured_messages[1][-1]
    blocks = tool_turn["content"]
    assert blocks[-1]["content"] == "<tool_output>\ntool says hello\n</tool_output>"
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_control_breakpoint_moves_not_accumulates(spend_eng):
    """The moving cache_control breakpoint must actually MOVE across rounds,
    not accumulate one per round -- Anthropic allows at most 4 cache_control
    breakpoints per request (system + tools already use 2), so 3+ tool rounds
    each adding a fresh one without clearing the last hit a real 400
    invalid_request_error in production ("A maximum of 4 blocks with
    cache_control may be provided. Found 5.") on exactly this kind of
    multi-round investigation goal."""
    from backend.agents import router

    captured_messages = []

    def _fake_create(model, max_tokens, messages, system, tools, label, task_id, trace_id=None, parent_span_id=None):
        # Snapshot now -- `messages` is the SAME list object every call and
        # keeps growing after this call returns, so a bare reference would
        # make every captured "round" alias the final (fully-mutated) state.
        captured_messages.append([dict(m) if isinstance(m, dict) else m for m in messages])
        # 4 tool-use rounds before the final answer -- enough that the OLD
        # (broken) code would have accumulated 2 base + 4 = 6 breakpoints.
        return _tool_use_resp(tid=f"tu_{len(captured_messages)}") if len(captured_messages) <= 4 else _final_resp()

    async def _dispatch(_input):
        return "tool result"

    with patch.object(router, "_create_sync_raw", _fake_create):
        out = await router.run_with_tools(
            "m", 100, "prompt", "sys", [{"name": "fake_tool"}],
            {"fake_tool": _dispatch},
        )

    assert out == "final answer"
    # Inspect the LAST request sent (round 5, after 4 tool rounds) -- every
    # earlier tool_result's cache_control must have been cleared, leaving
    # exactly one (the newest).
    final_messages = captured_messages[-1]
    cache_control_count = sum(
        1
        for m in final_messages
        if m.get("role") == "user" and isinstance(m.get("content"), list)
        for block in m["content"]
        if isinstance(block, dict) and "cache_control" in block
    )
    assert cache_control_count == 1, (
        f"expected exactly 1 moving tool_result breakpoint, found {cache_control_count} "
        "-- this is the exact shape Anthropic's 4-breakpoint limit rejects"
    )
    # And it must be on the NEWEST tool_result turn, not a stale one.
    newest_tool_turn = final_messages[-1]
    assert "cache_control" in newest_tool_turn["content"][-1]


@pytest.mark.asyncio
async def test_system_prompt_gets_injection_rule(spend_eng):
    from backend.agents import router

    captured_systems = []

    def _fake_create(model, max_tokens, messages, system, tools, label, task_id, trace_id=None, parent_span_id=None):
        captured_systems.append(system)
        return _final_resp()

    with patch.object(router, "_create_sync_raw", _fake_create):
        await router.run_with_tools("m", 100, "p", "my system", [], {})
        await router.run_with_tools("m", 100, "p", "", [], {})

    assert captured_systems[0].startswith("my system")
    assert router.TOOL_OUTPUT_RULE in captured_systems[0]
    assert captured_systems[1] == router.TOOL_OUTPUT_RULE


# ---------------------------------------------------------------------------
# council w-observability — LLM-call span recording in the _create_sync path
# ---------------------------------------------------------------------------

def _trace_span_rows(eng):
    from backend.database import TraceSpan
    with Session(eng) as s:
        return s.exec(select(TraceSpan)).all()


@pytest.mark.asyncio
async def test_llm_call_span_recorded_when_trace_active(spend_eng):
    """With a trace bound via set_trace_context, a single-shot call (router.sonnet
    -> _create_sync) writes one TraceSpan row with tokens/cost and truncated
    input/output summaries."""
    from backend.agents import router

    resp = _resp_with_usage()
    resp.content = [SimpleNamespace(type="text", text="hi there")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        token = router.set_trace_context(42)
        try:
            out = await router.sonnet("x" * 2000, label="span_test")
        finally:
            router.reset_trace_context(token)

    assert out == "hi there"
    rows = _trace_span_rows(spend_eng)
    assert len(rows) == 1
    row = rows[0]
    assert row.trace_id == 42
    assert row.parent_span_id is None
    assert row.span_type == "llm_call"
    assert row.name == f"span_test ({router.SONNET_MODEL})"
    assert row.tokens_in == 1000
    assert row.tokens_out == 500
    # Pricing keys off the real model id via _record_trace_span's model= kwarg,
    # NOT off the (now label-prefixed) span name -- this is the regression guard.
    assert row.cost_usd == pytest.approx(router._compute_cost(router.SONNET_MODEL, 1000, 500, 0, 0))
    assert row.input_summary is not None and len(row.input_summary) == 1000
    assert row.output_summary == "hi there"


@pytest.mark.asyncio
async def test_llm_call_span_name_falls_back_to_model_when_unlabeled(spend_eng):
    """No label= passed -> span name degrades to the bare model id, matching
    pre-B2 behavior exactly (no dangling " ()" suffix)."""
    from backend.agents import router

    resp = _resp_with_usage()
    resp.content = [SimpleNamespace(type="text", text="hi there")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        token = router.set_trace_context(43)
        try:
            await router.sonnet("x")
        finally:
            router.reset_trace_context(token)

    rows = _trace_span_rows(spend_eng)
    assert len(rows) == 1
    assert rows[0].name == router.SONNET_MODEL
    assert rows[0].cost_usd == pytest.approx(router._compute_cost(router.SONNET_MODEL, 1000, 500, 0, 0))


@pytest.mark.asyncio
async def test_llm_call_span_not_recorded_when_no_trace(spend_eng):
    """Without a bound trace context, no TraceSpan row is written."""
    from backend.agents import router

    resp = _resp_with_usage()
    resp.content = [SimpleNamespace(type="text", text="hi")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        out = await router.sonnet("prompt")

    assert out == "hi"
    assert _trace_span_rows(spend_eng) == []


@pytest.mark.asyncio
async def test_llm_call_span_gets_parent_from_span_stack(spend_eng):
    """When a span stack is bound, the new span's parent_span_id is the
    innermost (last) entry on the stack."""
    from backend.agents import router

    resp = _resp_with_usage()
    resp.content = [SimpleNamespace(type="text", text="ok")]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        mock_anthropic.return_value = mock_client

        trace_token = router.set_trace_context(7)
        stack_token = router.set_span_stack_context((1, 2, 3))
        try:
            await router.sonnet("prompt")
        finally:
            router.reset_span_stack_context(stack_token)
            router.reset_trace_context(trace_token)

    rows = _trace_span_rows(spend_eng)
    assert len(rows) == 1
    assert rows[0].parent_span_id == 3


# ---------------------------------------------------------------------------
# Anthropic credit-exhaustion detection + paging
#
# 2026-08-15 the account ran out of credit; both the briefing and a
# goal-proposer tick failed with the same HTTP 400 and nobody was told it was
# a billing problem specifically -- these pin the fix: _run and run_with_tools
# both page distinctly, exactly once (debounced), on this specific failure,
# and never on any other kind of failure.
# ---------------------------------------------------------------------------

def _credit_exhausted_error(message="Your credit balance is too low to access the "
                                     "Anthropic API. Please go to Plans & Billing to "
                                     "upgrade or purchase credits."):
    """A real anthropic.BadRequestError shaped exactly like the live 2026-08-15
    failure -- constructed via the SDK's own class, not a fake/duck-typed stand-in."""
    import anthropic
    import httpx

    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    resp = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json=body,
    )
    return anthropic.BadRequestError(message=f"Error code: 400 - {body}", response=resp, body=body)


def test_is_credit_exhausted_matches_real_sdk_error():
    from backend.agents import router

    assert router._is_credit_exhausted(_credit_exhausted_error()) is True
    # The streaming path only has the stringified form, no .body -- must still match.
    assert router._is_credit_exhausted(RuntimeError(str(_credit_exhausted_error()))) is True


def test_is_credit_exhausted_does_not_match_other_400s():
    """Same status code, same generic error.type (invalid_request_error) -- e.g.
    the real cache_control-breakpoint-limit error this module already
    documents hitting -- must NOT be mistaken for credit exhaustion."""
    from backend.agents import router

    other = _credit_exhausted_error(
        message="A maximum of 4 blocks with cache_control may be provided. Found 5."
    )
    assert router._is_credit_exhausted(other) is False


def test_generic_llm_failure_does_not_page():
    from backend.agents import router

    assert router._is_credit_exhausted(RuntimeError("network timeout")) is False


@pytest.mark.asyncio
async def test_run_pages_once_on_credit_exhaustion(spend_eng):
    from backend.agents import router, watchdog

    watchdog.reset()
    err = _credit_exhausted_error()

    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify, \
         patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": 1, "surface": True, "reason": None}) as mock_flag:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = err
        mock_anthropic.return_value = mock_client

        with pytest.raises(Exception):
            await router.sonnet("prompt 1")
        with pytest.raises(Exception):
            await router.sonnet("prompt 2")

    assert mock_notify.await_count == 1
    assert mock_notify.await_args[1]["kind"] == "anthropic_credit_exhausted"
    mock_flag.assert_awaited_once()
    assert mock_flag.await_args[0][0:2] == ("router", "anthropic_credit")


@pytest.mark.asyncio
async def test_run_with_tools_pages_on_credit_exhaustion(spend_eng):
    from backend.agents import router, watchdog

    watchdog.reset()
    err = _credit_exhausted_error()

    def _fake_create(*args, **kwargs):
        raise err

    with patch.object(router, "_create_sync_raw", _fake_create), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify, \
         patch("backend.agents.outcomes.record_flag_ex", new_callable=AsyncMock,
               return_value={"id": 1, "surface": True, "reason": None}):
        with pytest.raises(Exception):
            await router.run_with_tools("m", 100, "prompt", "sys", [{"name": "fake_tool"}], {})

    mock_notify.assert_awaited_once()
    assert mock_notify.await_args[1]["kind"] == "anthropic_credit_exhausted"


@pytest.mark.asyncio
async def test_other_failures_never_page_as_credit_exhausted(spend_eng):
    from backend.agents import router, watchdog

    watchdog.reset()

    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as mock_notify:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("network timeout")
        mock_anthropic.return_value = mock_client

        with pytest.raises(RuntimeError, match="network timeout"):
            await router.sonnet("prompt")

    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_credit_alert_failure_never_swallows_the_original_error(spend_eng):
    """A poisoned alert path (notify_phone itself raises) must never mask the
    real BadRequestError -- same 'poisoned X doesn't break Y' contract used
    throughout this codebase (e.g. test_span_failure_never_breaks_chat)."""
    from backend.agents import router, watchdog

    watchdog.reset()
    err = _credit_exhausted_error()

    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("backend.events.notify_phone", new_callable=AsyncMock,
               side_effect=RuntimeError("telegram down")):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = err
        mock_anthropic.return_value = mock_client

        with pytest.raises(Exception) as excinfo:
            await router.sonnet("prompt")

    assert "credit balance is too low" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Trial A — Gemini Flash-Lite shadow on Haiku-tier calls
# ---------------------------------------------------------------------------

def test_shadow_active_requires_model_label_and_date(monkeypatch):
    from backend.agents import router
    from types import SimpleNamespace

    off = SimpleNamespace(shadow_model="", shadow_until="", shadow_labels="mail_junk_classify")
    assert router._shadow_active("mail_junk_classify", off) is False

    on = SimpleNamespace(shadow_model="google/gemini-2.5-flash-lite", shadow_until="", shadow_labels="mail_junk_classify")
    assert router._shadow_active("mail_junk_classify", on) is True
    assert router._shadow_active("some_other_label", on) is False

    expired = SimpleNamespace(shadow_model="google/gemini-2.5-flash-lite", shadow_until="2000-01-01", shadow_labels="mail_junk_classify")
    assert router._shadow_active("mail_junk_classify", expired) is False


@pytest.mark.asyncio
async def test_haiku_call_fires_shadow_when_active(spend_eng, monkeypatch, tmp_path):
    """A real Haiku call, with shadow active for its label, must fire a
    background shadow call -- verified via the recorded shadow: SpendLog row,
    not by awaiting the task directly (the whole point is it's not awaited)."""
    from backend.agents import router
    from backend.config import Settings

    monkeypatch.setattr(router, "_SHADOW_LOG", tmp_path / "shadow.jsonl")

    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="KEEP")]
    mock_resp.usage = None

    settings = Settings(shadow_model="google/gemini-2.5-flash-lite", shadow_until="", shadow_labels="mail_junk_classify")

    shadow_http_resp = MagicMock()
    shadow_http_resp.raise_for_status = MagicMock()
    shadow_http_resp.json.return_value = {
        "choices": [{"message": {"content": "KEEP"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }

    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("backend.config.get_settings", return_value=settings), \
         patch("httpx.AsyncClient") as mock_httpx_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        mock_httpx = AsyncMock()
        mock_httpx.post.return_value = shadow_http_resp
        mock_httpx_cls.return_value.__aenter__.return_value = mock_httpx

        from backend.agents import router as router_mod
        await router.haiku("is this junk mail?", label="mail_junk_classify")
        # Give the fire-and-forget task a chance to run.
        for t in list(router_mod._shadow_tasks):
            await t

    from sqlmodel import Session, select
    from backend.database import SpendLog
    with Session(spend_eng) as s:
        rows = s.exec(select(SpendLog).where(SpendLog.label == "shadow:mail_junk_classify")).all()
    assert len(rows) == 1
    assert rows[0].model == "google/gemini-2.5-flash-lite"


@pytest.mark.asyncio
async def test_haiku_call_no_shadow_when_inactive(spend_eng, monkeypatch):
    from backend.agents import router
    from backend.config import Settings

    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="KEEP")]
    mock_resp.usage = None

    settings = Settings(shadow_model="", shadow_until="", shadow_labels="mail_junk_classify")

    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("backend.config.get_settings", return_value=settings), \
         patch("httpx.AsyncClient") as mock_httpx_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        await router.haiku("is this junk mail?", label="mail_junk_classify")

    mock_httpx_cls.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_failure_never_touches_real_response(spend_eng, monkeypatch):
    """A poisoned shadow path (httpx raises) must never affect the real
    Haiku call's return value -- same 'poisoned X doesn't break Y' contract
    used throughout this module."""
    from backend.agents import router
    from backend.config import Settings

    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(type="text", text="KEEP")]
    mock_resp.usage = None

    settings = Settings(shadow_model="google/gemini-2.5-flash-lite", shadow_until="", shadow_labels="mail_junk_classify")

    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("backend.config.get_settings", return_value=settings), \
         patch("httpx.AsyncClient", side_effect=RuntimeError("network down")):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_anthropic.return_value = mock_client

        result = await router.haiku("is this junk mail?", label="mail_junk_classify")
        # Give the fire-and-forget task a chance to actually fail, so this
        # assertion proves isolation rather than just that the task never ran.
        for t in list(router._shadow_tasks):
            await t

    assert result == "KEEP"
