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
        assert call_kwargs.get("system") == "You are an expert"


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
