import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_trigger_recording_success():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock(status_code=200)
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": "123", "status": "scheduled"}
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.channels_dvr import trigger_recording
        result = await trigger_recording("PROG001")
        assert result["id"] == "123"


@pytest.mark.asyncio
async def test_trigger_recording_not_found():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock(status_code=404)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.channels_dvr import trigger_recording
        with pytest.raises(ValueError):
            await trigger_recording("BAD_ID")
