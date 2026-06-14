import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json


@pytest.mark.asyncio
async def test_notify_success():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import notify
        result = await notify({"type": "test", "content": "hello"})
        assert result is True


@pytest.mark.asyncio
async def test_notify_failure_queues():
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session") as mock_session:

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value = mock_client

        session_mock = MagicMock()
        mock_session.return_value.__enter__ = MagicMock(return_value=session_mock)
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        from backend.integrations.hermes import notify
        result = await notify({"type": "test"})
        assert result is False
        session_mock.add.assert_called_once()
        session_mock.commit.assert_called_once()


@pytest.mark.asyncio
async def test_deliver_pending_success():
    # deliver_pending now does DB I/O in threads via two helpers:
    #   _load_pending() -> list[dict]   (read, off the event loop)
    #   _apply_pending_results(delivered_ids, failed_ids)  (write, off the loop)
    # A 200 response must route the row's id into delivered_ids.
    pending = [{
        "id": 1,
        "payload_json": '{"type": "notify", "content": "test"}',
        "delivery_type": "notify",
    }]

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.hermes._load_pending", return_value=pending), \
         patch("backend.integrations.hermes._apply_pending_results") as mock_apply:

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import deliver_pending
        await deliver_pending()

        mock_apply.assert_called_once_with([1], [])


@pytest.mark.asyncio
async def test_deliver_pending_failure_increments():
    # A non-2xx response must route the row's id into failed_ids (attempts++).
    pending = [{
        "id": 7,
        "payload_json": '{"type": "notify"}',
        "delivery_type": "notify",
    }]

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.hermes._load_pending", return_value=pending), \
         patch("backend.integrations.hermes._apply_pending_results") as mock_apply:

        mock_resp = MagicMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import deliver_pending
        await deliver_pending()

        mock_apply.assert_called_once_with([], [7])


@pytest.mark.asyncio
async def test_deliver_pending_empty_is_noop():
    # No pending rows => no HTTP client, no write phase.
    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.integrations.hermes._load_pending", return_value=[]), \
         patch("backend.integrations.hermes._apply_pending_results") as mock_apply:

        from backend.integrations.hermes import deliver_pending
        await deliver_pending()

        mock_client_cls.assert_not_called()
        mock_apply.assert_not_called()
