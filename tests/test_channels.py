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


@pytest.mark.asyncio
async def test_fetch_reads_rules_and_last_recording_nonfatally():
    """Rules/last-recording come from /dvr/rules and /api/v1/all (created_at in
    ms); a failure there leaves the fields None instead of failing fetch()."""
    import httpx
    from backend.integrations import channels_dvr

    def handler(fail_extras):
        def h(req):
            p = req.url.path
            if p == "/dvr":
                return httpx.Response(200, json={"disk": {"total": 2 * 1024**3, "free": 1024**3}})
            if p == "/api/v1/jobs":
                return httpx.Response(200, json=[])
            if fail_extras:
                return httpx.Response(500)
            if p == "/dvr/rules":
                return httpx.Response(200, json=[{"Paused": False}, {"Paused": True}, {"Paused": False}])
            if p == "/api/v1/all":
                assert req.url.params["limit"] == "1"
                return httpx.Response(200, json=[{"title": "X", "created_at": 1790540700000,
                                                  "completed": True, "corrupted": False}])
            return httpx.Response(404)
        return h

    real = httpx.AsyncClient
    for fail in (False, True):
        with patch("backend.integrations.channels_dvr.httpx.AsyncClient",
                   lambda **kw: real(transport=httpx.MockTransport(handler(fail)))):
            d = await channels_dvr.fetch.__wrapped__()  # bypass the TTL cache
        if fail:
            assert d.rules_total is None and d.last_recording is None
            assert d.storage_total_gb == 2.0
        else:
            assert (d.rules_enabled, d.rules_total) == (2, 3)
            assert d.last_recording["title"] == "X" and d.last_recording["completed"]
            assert d.last_recording["added"].startswith("2026-09-27T")
