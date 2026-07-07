import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_unifi_health_check_ok():
    """health_check now exercises the same login POST fetch() depends on."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        from backend.integrations.unifi import health_check
        assert await health_check() is True


@pytest.mark.asyncio
async def test_unifi_health_check_bad_credentials():
    """A failed login (e.g. wrong/expired UNIFI_PASSWORD) must NOT report healthy —
    this is the bug fix: the old root-ping check couldn't detect this at all."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock(status_code=401)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client
        from backend.integrations.unifi import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_unifi_health_check_fail():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = mock_client
        from backend.integrations.unifi import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_unifi_fetch_clients_and_uplink():
    """fetch returns client count and uplink_status from API responses."""
    login_resp = MagicMock(status_code=200)
    login_resp.json.return_value = {}

    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {
        "data": [
            {"mac": "aa:bb:cc:dd:ee:01", "hostname": "laptop"},
            {"mac": "aa:bb:cc:dd:ee:02", "hostname": "phone"},
        ]
    }

    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {
        "data": [{"subsystem": "wan", "status": "ok"}]
    }

    with patch("httpx.AsyncClient") as mock_cls, \
         patch("backend.integrations.unifi.Session") as mock_session_cls, \
         patch("backend.database.engine"):

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp])
        mock_cls.return_value = mock_client

        # Set up DB session mock — no known devices
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        mock_session.exec.return_value.first.return_value = None
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    assert data.client_count == 2
    assert data.uplink_status == "ok"
    assert len(data.new_devices) == 2


@pytest.mark.asyncio
async def test_unifi_fetch_detects_new_devices():
    """Devices not in the KnownDevice table appear in new_devices."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {
        "data": [{"mac": "de:ad:be:ef:00:01", "hostname": "newdevice"}]
    }
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {"data": []}

    with patch("httpx.AsyncClient") as mock_cls, \
         patch("backend.integrations.unifi.Session") as mock_session_cls, \
         patch("backend.database.engine"):

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp])
        mock_cls.return_value = mock_client

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []  # no known devices
        mock_session.exec.return_value.first.return_value = None
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    assert len(data.new_devices) == 1
    assert data.new_devices[0]["mac"] == "de:ad:be:ef:00:01"


@pytest.mark.asyncio
async def test_unifi_fetch_login_failure_raises():
    """A non-200/201 login response should raise an exception."""
    login_resp = MagicMock(status_code=401)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.unifi import fetch
        with pytest.raises(Exception, match="UniFi login failed"):
            await fetch()


@pytest.mark.asyncio
async def test_unifi_fetch_clients_non200_raises():
    """A failed clients fetch must raise, not silently report 0 clients (the
    'Unraid lesson' — a zero-default here looks like a dead AP)."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=500)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=clients_resp)
        mock_cls.return_value = mock_client

        from backend.integrations.unifi import fetch
        with pytest.raises(Exception, match="UniFi clients fetch failed"):
            await fetch()


@pytest.mark.asyncio
async def test_unifi_fetch_health_non200_raises():
    """A failed health fetch must raise, not silently report uplink_status='ok'."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {"data": []}
    health_resp = MagicMock(status_code=500)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp])
        mock_cls.return_value = mock_client

        from backend.integrations.unifi import fetch
        with pytest.raises(Exception, match="UniFi health fetch failed"):
            await fetch()


@pytest.mark.asyncio
async def test_unifi_fetch_uplink_degraded():
    """wan status != 'ok' produces uplink_status='degraded'."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {"data": []}
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {
        "data": [{"subsystem": "wan", "status": "degraded"}]
    }

    with patch("httpx.AsyncClient") as mock_cls, \
         patch("backend.integrations.unifi.Session") as mock_session_cls, \
         patch("backend.database.engine"):

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp])
        mock_cls.return_value = mock_client

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    assert data.uplink_status == "degraded"


def test_unifi_data_defaults():
    from backend.integrations.unifi import UniFiData
    data = UniFiData()
    assert data.client_count == 0
    assert data.uplink_status == "unknown"
    assert data.bandwidth_mbps == 0.0
    assert data.alerts == []
    assert data.new_devices == []
