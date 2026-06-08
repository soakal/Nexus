import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_set_filtering_enabled():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.adguard import set_filtering
        await set_filtering(True)
        call_args = mock_client.__aenter__.return_value.post.call_args
        assert call_args[1]["json"]["protection_enabled"] is True


@pytest.mark.asyncio
async def test_timed_disable():
    with patch("backend.integrations.adguard.set_filtering", new_callable=AsyncMock) as mock_set:
        from backend.integrations.adguard import disable_for_minutes
        await disable_for_minutes(5)
        mock_set.assert_called_once_with(False)


@pytest.mark.asyncio
async def test_adguard_toggle_disable_then_enable():
    calls = []
    import backend.integrations.adguard as ag_module
    ag_module._reenable_task = None

    async def mock_set(enabled):
        calls.append(enabled)

    with patch("backend.integrations.adguard.set_filtering", side_effect=mock_set):
        await ag_module.disable_for_minutes(1)
        assert calls[0] is False
    # Cancel the pending reenable task cleanly
    if ag_module._reenable_task and not ag_module._reenable_task.done():
        ag_module._reenable_task.cancel()
    ag_module._reenable_task = None
