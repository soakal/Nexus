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
    from backend.database import PendingDelivery
    from datetime import datetime

    pending = PendingDelivery(
        id=1,
        payload_json='{"type": "notify", "content": "test"}',
        delivery_type="notify",
        attempts=0,
    )

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session") as mock_session:

        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        session_mock = MagicMock()
        session_mock.exec.return_value.all.return_value = [pending]
        mock_session.return_value.__enter__ = MagicMock(return_value=session_mock)
        mock_session.return_value.__exit__ = MagicMock(return_value=False)

        from backend.integrations.hermes import deliver_pending
        await deliver_pending()
        session_mock.delete.assert_called_once_with(pending)
