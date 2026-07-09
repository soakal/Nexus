import pytest
from unittest.mock import patch, MagicMock


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

    def _fake_create(model, max_tokens, messages, system, tools, label, task_id):
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

    def _fake_create(model, max_tokens, messages, system, tools, label, task_id):
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

    def _fake_create(model, max_tokens, messages, system, tools, label, task_id):
        captured_systems.append(system)
        return _final_resp()

    with patch.object(router, "_create_sync_raw", _fake_create):
        await router.run_with_tools("m", 100, "p", "my system", [], {})
        await router.run_with_tools("m", 100, "p", "", [], {})

    assert captured_systems[0].startswith("my system")
    assert router.TOOL_OUTPUT_RULE in captured_systems[0]
    assert captured_systems[1] == router.TOOL_OUTPUT_RULE
