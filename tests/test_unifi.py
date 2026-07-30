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


def _alarm_resp(alerts=None, status_code=200):
    resp = MagicMock(status_code=status_code)
    resp.json.return_value = {"meta": {"rc": "ok"}, "data": alerts or []}
    return resp


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
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp, _alarm_resp()])
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
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp, _alarm_resp()])
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
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp, _alarm_resp()])
        mock_cls.return_value = mock_client

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    assert data.uplink_status == "degraded"


@pytest.mark.asyncio
async def test_unifi_fetch_bandwidth_from_wan_rate():
    """bandwidth_mbps is derived from the wan subsystem's tx_bytes-r/rx_bytes-r
    (bytes/sec) already present in the stat/health response, converted to Mbps."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {"data": []}
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {
        "data": [{"subsystem": "wan", "status": "ok", "tx_bytes-r": 125000, "rx_bytes-r": 125000}]
    }

    with patch("httpx.AsyncClient") as mock_cls, \
         patch("backend.integrations.unifi.Session") as mock_session_cls, \
         patch("backend.database.engine"):

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp, _alarm_resp()])
        mock_cls.return_value = mock_client

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    # (125000 + 125000) bytes/sec * 8 / 1_000_000 = 2.0 Mbps
    assert data.bandwidth_mbps == 2.0


@pytest.mark.asyncio
async def test_unifi_fetch_bandwidth_zero_when_no_wan_entry():
    """No wan subsystem entry in health data -- degrade to 0.0, don't raise."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {"data": []}
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {"data": []}

    with patch("httpx.AsyncClient") as mock_cls, \
         patch("backend.integrations.unifi.Session") as mock_session_cls, \
         patch("backend.database.engine"):

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp, _alarm_resp()])
        mock_cls.return_value = mock_client

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    assert data.bandwidth_mbps == 0.0


@pytest.mark.asyncio
async def test_unifi_fetch_bandwidth_null_rate_leaves_degrade_to_zero():
    """A wan entry present but with explicit null tx/rx values (not just
    missing keys) must not raise -- `.get(key, 0)` doesn't catch a `null`
    value the way `.get(key) or 0` does; None + None would otherwise blow up
    and get misreported as a full UniFi outage."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {"data": []}
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {
        "data": [{"subsystem": "wan", "status": "ok", "tx_bytes-r": None, "rx_bytes-r": None}]
    }

    with patch("httpx.AsyncClient") as mock_cls, \
         patch("backend.integrations.unifi.Session") as mock_session_cls, \
         patch("backend.database.engine"):

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[clients_resp, health_resp, _alarm_resp()])
        mock_cls.return_value = mock_client

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    assert data.bandwidth_mbps == 0.0


@pytest.mark.asyncio
async def test_unifi_fetch_alerts_filters_out_archived():
    """list/alarm returns ALL alarms by convention, not just active ones --
    fetch() must filter out anything with archived truthy so a resolved alarm
    doesn't show up as an active alert forever."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {"data": []}
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {"data": [{"subsystem": "wan", "status": "ok"}]}
    active = {"key": "EVT_WU_Disconnected", "msg": "AP disconnected", "archived": False}
    resolved = {"key": "EVT_WU_Connected", "msg": "AP reconnected", "archived": True}

    with patch("httpx.AsyncClient") as mock_cls, \
         patch("backend.integrations.unifi.Session") as mock_session_cls, \
         patch("backend.database.engine"):

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(
            side_effect=[clients_resp, health_resp, _alarm_resp([active, resolved])]
        )
        mock_cls.return_value = mock_client

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    assert data.alerts == [active]


@pytest.mark.asyncio
async def test_unifi_fetch_alerts_populated_from_alarm_endpoint():
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {"data": []}
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {"data": [{"subsystem": "wan", "status": "ok"}]}
    alarms = [{"key": "EVT_WU_Disconnected", "msg": "AP disconnected", "time": 1785318278000}]

    with patch("httpx.AsyncClient") as mock_cls, \
         patch("backend.integrations.unifi.Session") as mock_session_cls, \
         patch("backend.database.engine"):

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(
            side_effect=[clients_resp, health_resp, _alarm_resp(alarms)]
        )
        mock_cls.return_value = mock_client

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.exec.return_value.all.return_value = []
        mock_session_cls.return_value = mock_session

        from backend.integrations.unifi import fetch
        data = await fetch()

    assert data.alerts == alarms


@pytest.mark.asyncio
async def test_unifi_fetch_alarm_endpoint_non200_raises():
    """A failed alarms read must raise, not silently report 0 alerts (the
    'Unraid lesson' — a false all-clear is worse than a visible failure)."""
    login_resp = MagicMock(status_code=200)
    clients_resp = MagicMock(status_code=200)
    clients_resp.json.return_value = {"data": []}
    health_resp = MagicMock(status_code=200)
    health_resp.json.return_value = {"data": [{"subsystem": "wan", "status": "ok"}]}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=login_resp)
        mock_client.__aenter__.return_value.get = AsyncMock(
            side_effect=[clients_resp, health_resp, _alarm_resp(status_code=500)]
        )
        mock_cls.return_value = mock_client

        from backend.integrations.unifi import fetch
        with pytest.raises(Exception, match="UniFi alarms fetch failed"):
            await fetch()


def test_unifi_data_defaults():
    from backend.integrations.unifi import UniFiData
    data = UniFiData()
    assert data.client_count == 0
    assert data.uplink_status == "unknown"
    assert data.bandwidth_mbps == 0.0
    assert data.alerts == []
    assert data.new_devices == []


# ---------------------------------------------------------------------------
# Phase 7a — block/unblock (native, not via Hermes)
# ---------------------------------------------------------------------------

def test_normalize_mac_accepts_various_formats():
    from backend.integrations.unifi import _normalize_mac
    expected = "aa:bb:cc:dd:ee:ff"
    assert _normalize_mac("aa:bb:cc:dd:ee:ff") == expected
    assert _normalize_mac("AA:BB:CC:DD:EE:FF") == expected
    assert _normalize_mac("aa-bb-cc-dd-ee-ff") == expected
    assert _normalize_mac("aabb.ccdd.eeff") == expected
    assert _normalize_mac("aabbccddeeff") == expected


def test_normalize_mac_rejects_invalid():
    from backend.integrations.unifi import _normalize_mac
    for bad in ("", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:gg", "aa:bb:cc:dd:ee:ff:00",
                "'; DROP TABLE--", "not a mac at all"):
        with pytest.raises(ValueError):
            _normalize_mac(bad)


@pytest.mark.asyncio
async def test_login_extracts_csrf_from_header():
    from backend.integrations.unifi import _login
    login_resp = MagicMock(status_code=200)
    login_resp.headers = {"X-CSRF-Token": "tok-from-header"}
    login_resp.cookies = {}
    client = AsyncMock()
    client.post = AsyncMock(return_value=login_resp)
    headers = await _login(client)
    assert headers["X-CSRF-Token"] == "tok-from-header"


@pytest.mark.asyncio
async def test_login_extracts_csrf_from_cookie_fallback():
    import base64
    import json
    from backend.integrations.unifi import _login
    payload = base64.urlsafe_b64encode(json.dumps({"csrfToken": "tok-from-cookie"}).encode()).decode().rstrip("=")
    jwt_like = f"header.{payload}.sig"
    login_resp = MagicMock(status_code=200)
    login_resp.headers = {}
    login_resp.cookies = {"TOKEN": jwt_like}
    client = AsyncMock()
    client.post = AsyncMock(return_value=login_resp)
    headers = await _login(client)
    assert headers["X-CSRF-Token"] == "tok-from-cookie"


@pytest.mark.asyncio
async def test_login_no_csrf_available_omits_header():
    """Neither header nor a decodable cookie -- degrade to no CSRF header rather
    than crash; the stamgr POST will simply fail loudly downstream if UniFi
    actually requires it, matching every other 'raise, don't guess' pattern here."""
    from backend.integrations.unifi import _login
    login_resp = MagicMock(status_code=200)
    login_resp.headers = {}
    login_resp.cookies = {}
    client = AsyncMock()
    client.post = AsyncMock(return_value=login_resp)
    headers = await _login(client)
    assert "X-CSRF-Token" not in headers


@pytest.mark.asyncio
async def test_block_client_success():
    from backend.integrations.unifi import block_client, fetch
    login_resp = MagicMock(status_code=200)
    login_resp.headers = {"X-CSRF-Token": "tok"}
    login_resp.cookies = {}
    cmd_resp = MagicMock(status_code=200)
    cmd_resp.json.return_value = {"meta": {"rc": "ok"}, "data": []}

    with patch("httpx.AsyncClient") as mock_cls, patch.object(fetch, "invalidate") as mock_inval:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=[login_resp, cmd_resp])
        mock_cls.return_value = mock_client

        result = await block_client("aa:bb:cc:dd:ee:ff")

    assert result["meta"]["rc"] == "ok"
    mock_inval.assert_called_once()
    # Second POST (the actual stamgr call) must carry the normalized mac + cmd.
    second_call = mock_client.__aenter__.return_value.post.call_args_list[1]
    assert second_call.kwargs["json"] == {"cmd": "block-sta", "mac": "aa:bb:cc:dd:ee:ff"}
    assert second_call.kwargs["headers"]["X-CSRF-Token"] == "tok"


@pytest.mark.asyncio
async def test_unblock_client_success():
    from backend.integrations.unifi import unblock_client, fetch
    login_resp = MagicMock(status_code=200)
    login_resp.headers = {"X-CSRF-Token": "tok"}
    login_resp.cookies = {}
    cmd_resp = MagicMock(status_code=200)
    cmd_resp.json.return_value = {"meta": {"rc": "ok"}}

    with patch("httpx.AsyncClient") as mock_cls, patch.object(fetch, "invalidate"):
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=[login_resp, cmd_resp])
        mock_cls.return_value = mock_client

        result = await unblock_client("aabbccddeeff")

    assert result["meta"]["rc"] == "ok"
    second_call = mock_client.__aenter__.return_value.post.call_args_list[1]
    assert second_call.kwargs["json"]["cmd"] == "unblock-sta"


@pytest.mark.asyncio
async def test_block_client_http_failure_raises():
    from backend.integrations.unifi import block_client
    login_resp = MagicMock(status_code=200)
    login_resp.headers = {}
    login_resp.cookies = {}
    cmd_resp = MagicMock(status_code=500)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=[login_resp, cmd_resp])
        mock_cls.return_value = mock_client

        with pytest.raises(Exception, match="UniFi block-sta failed"):
            await block_client("aa:bb:cc:dd:ee:ff")


@pytest.mark.asyncio
async def test_block_client_rc_error_raises():
    """A 200 whose body reports rc != 'ok' must still raise -- HTTP 200 alone
    does not mean the command actually succeeded."""
    from backend.integrations.unifi import block_client
    login_resp = MagicMock(status_code=200)
    login_resp.headers = {}
    login_resp.cookies = {}
    cmd_resp = MagicMock(status_code=200)
    cmd_resp.json.return_value = {"meta": {"rc": "error", "msg": "api.err.NoSTAFound"}}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=[login_resp, cmd_resp])
        mock_cls.return_value = mock_client

        with pytest.raises(Exception, match="UniFi block-sta failed"):
            await block_client("aa:bb:cc:dd:ee:ff")


@pytest.mark.asyncio
async def test_block_client_invalid_mac_raises_before_any_request():
    from backend.integrations.unifi import block_client
    with patch("httpx.AsyncClient") as mock_cls:
        with pytest.raises(ValueError):
            await block_client("not-a-real-mac")
        mock_cls.assert_not_called()
