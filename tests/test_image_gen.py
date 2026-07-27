from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.integrations import image_gen, telegram


def _mock_get_client(response):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get = AsyncMock(return_value=response)
    return mock_client


# ---------------------------------------------------------------------------
# generate_image
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_image_success():
    resp = MagicMock(status_code=200, content=b"\xff\xd8fake", headers={"content-type": "image/jpeg"})
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_get_client(resp)
        result = await image_gen.generate_image("a cat")
    assert result == b"\xff\xd8fake"


@pytest.mark.asyncio
async def test_generate_image_non_200_returns_none():
    resp = MagicMock(status_code=500, content=b"", headers={"content-type": "text/plain"})
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_get_client(resp)
        result = await image_gen.generate_image("a cat")
    assert result is None


@pytest.mark.asyncio
async def test_generate_image_non_image_content_type_returns_none():
    resp = MagicMock(status_code=200, content=b"<html>error</html>", headers={"content-type": "text/html"})
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_get_client(resp)
        result = await image_gen.generate_image("a cat")
    assert result is None


@pytest.mark.asyncio
async def test_generate_image_transport_error_returns_none():
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await image_gen.generate_image("a cat")
    assert result is None


@pytest.mark.asyncio
async def test_generate_image_url_encodes_prompt():
    resp = MagicMock(status_code=200, content=b"img", headers={"content-type": "image/jpeg"})
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = _mock_get_client(resp)
        await image_gen.generate_image("a cat & dog/thing")

    called_url = mock_client_cls.return_value.__aenter__.return_value.get.call_args.args[0]
    assert "%26" in called_url
    assert "%2F" in called_url
    assert " " not in called_url


# ---------------------------------------------------------------------------
# telegram.send_photo
# ---------------------------------------------------------------------------

def _mock_post_client(response):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(return_value=response)
    return mock_client


@pytest.mark.asyncio
async def test_send_photo_multipart_success():
    resp = MagicMock(status_code=200)
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.config.get_settings") as mock_settings:
        mock_settings.return_value.telegram_bot_token = "T"
        mock_settings.return_value.telegram_chat_id = "99"
        mock_client_cls.return_value = _mock_post_client(resp)
        result = await telegram.send_photo(b"imgbytes", caption="a cat")

    assert result is True
    call = mock_client_cls.return_value.__aenter__.return_value.post.call_args
    assert call.kwargs["data"]["chat_id"] == "99"
    assert call.kwargs["files"]["photo"][1] == b"imgbytes"


@pytest.mark.asyncio
async def test_send_photo_non_200_returns_false():
    resp = MagicMock(status_code=400, text="bad request")
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.config.get_settings") as mock_settings:
        mock_settings.return_value.telegram_bot_token = "T"
        mock_settings.return_value.telegram_chat_id = "99"
        mock_client_cls.return_value = _mock_post_client(resp)
        result = await telegram.send_photo(b"imgbytes")
    assert result is False


@pytest.mark.asyncio
async def test_send_photo_never_raises():
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.config.get_settings") as mock_settings:
        mock_settings.return_value.telegram_bot_token = "T"
        mock_settings.return_value.telegram_chat_id = "99"
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=RuntimeError("boom"))
        mock_client_cls.return_value = mock_client
        result = await telegram.send_photo(b"imgbytes")
    assert result is False
